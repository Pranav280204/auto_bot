import os
import json
import requests
from dotenv import load_dotenv
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta
import time
# Telegram imports
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

load_dotenv()

# Config
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
DRY_RUN = os.getenv("DRY_RUN", "False").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")

if not all([PRIVATE_KEY, WALLET_ADDRESS, TELEGRAM_BOT_TOKEN, TWITTER_API_KEY]):
    raise ValueError("Missing required env variables")

# Constants
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYGON_RPC = "https://polygon-rpc.com/"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

ERC1155_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
client = ClobClient(
    host=CLOB_API,
    key=PRIVATE_KEY,
    chain_id=137,
    signature_type=1,
    funder=WALLET_ADDRESS
)

try:
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    print("API credentials set successfully")
except Exception as e:
    print(f"API creds error: {e}")
    exit(1)

print(f"Connected to Polymarket with wallet: {WALLET_ADDRESS[:6]}...{WALLET_ADDRESS[-4:]}")
print(f"Dry run mode: {DRY_RUN}")

# Twitter monitoring
TARGET_ACCOUNT = "elonmusk"
CHECK_INTERVAL = 0.5  # Aggressive polling for maximum speed
LAST_CHECKED_TIME = datetime.now(timezone.utc)
SEEN_TWEET_IDS = set()

# Conversation states
SLUG, MARKET_IDX, NUM_OUTCOMES, OUTCOME1_IDX, OUTCOME2_IDX, ACTION, BUY_AMOUNT, CONFIRM_BUY, SELL_CHOICE, CUSTOM_SELL, AUTO_BUY_YN, AUTO_BUY_AMOUNT, START_MONITOR = range(13)

def fetch_active_markets(slug):
    url = f"{GAMMA_API}/events/slug/{slug}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        event = response.json()
        markets = event.get("markets", [])
        active = [m for m in markets if m.get("active", False) and not m.get("closed", True)]
        return active
    except Exception as e:
        print(f"Fetch markets error: {e}")
        return []

def get_mid_price(token_id):
    try:
        book = client.get_order_book(token_id)
        bids = [float(p) for p, _ in book.get("bids", [])]
        asks = [float(p) for p, _ in book.get("asks", [])]
        if bids and asks:
            return (max(bids) + min(asks)) / 2
        return max(bids) if bids else (min(asks) if asks else None)
    except Exception as e:
        print(f"Orderbook error: {e}")
        return None

def get_balance(token_id):
    try:
        contract = w3.eth.contract(address=w3.to_checksum_address(CONDITIONAL_TOKENS), abi=ERC1155_ABI)
        balance_wei = contract.functions.balanceOf(w3.to_checksum_address(WALLET_ADDRESS), int(token_id)).call()
        return balance_wei / 1_000_000
    except Exception as e:
        print(f"Balance fetch error: {e}")
        return 0.0

def place_market_order(token_id, amount, side):
    if DRY_RUN:
        print(f"[DRY RUN] Would place {side} amount {amount} on token {token_id}")
        return {"status": "dry_run"}
    try:
        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=side,
            order_type=OrderType.FOK
        )
        signed = client.create_market_order(args)
        resp = client.post_order(signed, OrderType.FOK)
        print("Order response:", resp)
        return resp
    except Exception as e:
        print(f"Order placement failed: {e}")
        return None

async def safe_send_message(bot, chat_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        print(f"Send message error: {e}")

async def check_for_new_tweets(session, application, initial=False):
    global LAST_CHECKED_TIME, SEEN_TWEET_IDS

    now = datetime.now(timezone.utc)
    overlap_back = timedelta(minutes=5) if initial else timedelta(seconds=10)
    since_time = now - overlap_back
    until_time = now + timedelta(seconds=5)

    since_str = since_time.strftime("%Y-%m-%d_%H:%M:%S_UTC")
    until_str = until_time.strftime("%Y-%m-%d_%H:%M:%S_UTC")

    query = f"from:{TARGET_ACCOUNT} since:{since_str} until:{until_str} include:nativeretweets"
    url = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    params = {"query": query, "queryType": "Latest"}
    headers = {"X-API-Key": TWITTER_API_KEY}

    all_tweets = []
    next_cursor = None
    detection_start = time.time()

    while True:
        if next_cursor:
            params["cursor"] = next_cursor
        async with session.get(url, headers=headers, params=params) as response:
            if response.status != 200:
                print(f"Twitter API Error: {response.status}")
                break
            data = await response.json()
            tweets = data.get("tweets", [])
            all_tweets.extend(tweets)
            if not data.get("has_next_page", False) or not data.get("next_cursor"):
                break
            next_cursor = data.get("next_cursor")

    if initial:
        for tweet in all_tweets:
            SEEN_TWEET_IDS.add(tweet.get("id"))
        LAST_CHECKED_TIME = now
        print(f"Initial population complete: {len(SEEN_TWEET_IDS)} recent tweets cached")
        return

    new_tweets = [t for t in all_tweets if t.get("id") not in SEEN_TWEET_IDS]
    if not new_tweets:
        return

    detection_end = time.time()
    print(f"DETECTION TOOK {detection_end - detection_start:.3f}s")

    chat_id = application.bot_data['chat_id']

    # Retrieve trigger config
    token_id_sell = application.bot_data.get('token_id_sell')
    sell_amount = application.bot_data.get('sell_amount', 0)
    token_id_buy = application.bot_data.get('token_id_buy')
    buy_usdc = application.bot_data.get('buy_usdc', 0)
    from_outcome = application.bot_data.get('from_outcome')
    target_outcome = application.bot_data.get('target_outcome')

    trigger_msg = "🚨 NEW ELON ACTIVITY DETECTED! Executing trigger actions...\n\n"
    results = []

    # Execute SELL first (priority for better fill in fast-moving markets)
    sell_result = None
    if sell_amount > 0 and token_id_sell:
        sell_start = time.time()
        sell_result = place_market_order(token_id_sell, sell_amount, SELL)
        sell_dur = time.time() - sell_start
        results.append(f"✅ SELL {sell_amount:.4f} shares of {from_outcome} (took {sell_dur:.3f}s)")
        print(f"SELL executed in {sell_dur:.3f}s")

    # Then BUY
    buy_result = None
    if buy_usdc > 0 and token_id_buy:
        buy_start = time.time()
        buy_result = place_market_order(token_id_buy, buy_usdc, BUY)
        buy_dur = time.time() - buy_start
        results.append(f"✅ BUY ${buy_usdc:.2f} USDC of {target_outcome} (took {buy_dur:.3f}s)")
        print(f"BUY executed in {buy_dur:.3f}s")

    if results:
        trigger_msg += "\n".join(results)
    else:
        trigger_msg += "No actions configured."

    await safe_send_message(application.bot, chat_id, trigger_msg)

    # Full tweet details
    message = f"{'═' * 60}\nDETECTED {len(new_tweets)} NEW TWEET(S)\n{'═' * 60}\n\n"
    for tweet in new_tweets:
        created_str = tweet.get('createdAt', '??')
        try:
            tweet_time = datetime.strptime(created_str, "%a %b %d %H:%M:%S %z %Y")
            latency = (now - tweet_time).total_seconds()
            latency_str = f"{latency:.2f}s ago"
            time_str = tweet_time.strftime('%Y-%m-%d %H:%M:%S UTC')
        except:
            latency_str = "??"
            time_str = "??"
        message += f"[{time_str}] {latency_str}\n"
        text = tweet.get('text', '').strip()
        message += f"{text[:500]}{'...' if len(text) > 500 else ''}\n"
        message += "─" * 70 + "\n"

    await safe_send_message(application.bot, chat_id, message)

    total_cycle = time.time() - detection_start
    print(f"FULL CYCLE (detect → orders): {total_cycle:.3f}s")
    await safe_send_message(application.bot, chat_id, f"Full cycle completed in {total_cycle:.2f}s")

    # Update seen + last checked time
    tweet_times = []
    for tweet in new_tweets:
        tid = tweet.get("id")
        if tid:
            SEEN_TWEET_IDS.add(tid)
        created_str = tweet.get('createdAt')
        if created_str:
            try:
                tt = datetime.strptime(created_str, "%a %b %d %H:%M:%S %z %Y")
                tweet_times.append(tt)
            except:
                pass

    if tweet_times:
        LAST_CHECKED_TIME = max(tweet_times) + timedelta(seconds=1)

async def monitor_elon_activity(application: Application):
    print("Monitoring started")
    async with aiohttp.ClientSession() as session:
        # Initial population to avoid triggering on past tweets
        await check_for_new_tweets(session, application, initial=True)
        while application.bot_data.get('monitoring', False):
            loop_start = time.time()
            await check_for_new_tweets(session, application)
            duration = time.time() - loop_start
            sleep_time = max(0, CHECK_INTERVAL - duration)
            await asyncio.sleep(sleep_time)
    print("Monitoring stopped")

# Telegram handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Enter Polymarket event slug:")
    return SLUG

async def get_slug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    slug = update.message.text.strip()
    context.user_data['slug'] = slug
    markets = fetch_active_markets(slug)
    if not markets:
        await update.message.reply_text("No active markets found.")
        return ConversationHandler.END
    text = f"Found {len(markets)} active market(s):\n"
    for i, m in enumerate(markets):
        text += f"{i}: {m.get('question', 'Unknown')}\n"
    await update.message.reply_text(text + "\nSelect market number:")
    return MARKET_IDX

async def get_market_idx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        idx = int(update.message.text.strip())
        markets = fetch_active_markets(context.user_data['slug'])
        market = markets[idx]
        context.user_data['market'] = market
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        token_ids = market.get("clobTokenIds", [])
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
        context.user_data['outcomes'] = outcomes
        context.user_data['token_ids'] = token_ids
        await update.message.reply_text("How many outcome ranges do you want to trade on? (1 or 2)")
        return NUM_OUTCOMES
    except:
        await update.message.reply_text("Invalid number.")
        return MARKET_IDX

async def get_num_outcomes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text not in ["1", "2"]:
        await update.message.reply_text("Please enter 1 or 2")
        return NUM_OUTCOMES
    context.user_data['num_outcomes'] = int(text)
    await update.message.reply_text("Select FIRST outcome (current / from range):")
    return OUTCOME1_IDX

async def get_outcome1_idx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        idx = int(update.message.text.strip())
        outcomes = context.user_data['outcomes']
        token_ids = context.user_data['token_ids']
        outcome = outcomes[idx]
        token_id = token_ids[idx]
        context.user_data['outcome1'] = outcome
        context.user_data['token_id1'] = token_id

        if context.user_data['num_outcomes'] == 1:
            mid = get_mid_price(token_id)
            price_str = f"{mid:.4f}" if mid else "N/A"
            await update.message.reply_text(
                f"Selected: {outcome} | Mid: {price_str}\n\n"
                "Initial action on this outcome — Buy new position or use existing for Sell? (b/s)"
            )
            return ACTION
        else:
            text = "Remaining outcomes:\n"
            for i, o in enumerate(outcomes):
                if i != idx:
                    text += f"{i}: {o}\n"
            await update.message.reply_text(text + "\nSelect SECOND outcome (target / to range):")
            return OUTCOME2_IDX
    except:
        await update.message.reply_text("Invalid index.")
        return OUTCOME1_IDX

async def get_outcome2_idx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        idx = int(update.message.text.strip())
        outcomes = context.user_data['outcomes']
        token_ids = context.user_data['token_ids']
        outcome = outcomes[idx]
        token_id = token_ids[idx]
        context.user_data['outcome2'] = outcome
        context.user_data['token_id2'] = token_id

        mid1 = get_mid_price(context.user_data['token_id1'])
        mid2 = get_mid_price(token_id)
        await update.message.reply_text(
            f"From: {context.user_data['outcome1']} (mid {mid1:.4f if mid1 else 'N/A'})\n"
            f"To: {outcome} (mid {mid2:.4f if mid2 else 'N/A'})\n\n"
            "Initial action on FROM outcome — Buy or use existing Sell? (b/s)"
        )
        return ACTION
    except:
        await update.message.reply_text("Invalid index.")
        return OUTCOME2_IDX

async def get_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = update.message.text.lower().strip()
    if action not in ['b', 's']:
        await update.message.reply_text("Please enter 'b' or 's'")
        return ACTION
    context.user_data['action'] = action
    token_id1 = context.user_data['token_id1']

    if action == 'b':
        await update.message.reply_text("Enter initial USDC amount to BUY on FROM outcome:")
        return BUY_AMOUNT
    else:
        bal = get_balance(token_id1)
        if bal < 0.01:
            await update.message.reply_text("Insufficient balance on FROM outcome.")
            return ConversationHandler.END
        context.user_data['balance'] = bal
        await update.message.reply_text(
            f"Balance on FROM: {bal:.4f} shares\n\n"
            "Sell amount on trigger:\n1 = 25%\n2 = 50%\n3 = 100%\n4 = custom\nChoice:"
        )
        return SELL_CHOICE

async def get_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        usdc = float(update.message.text.strip())
        if usdc <= 0:
            raise ValueError
        context.user_data['initial_usdc'] = usdc
        token_id = context.user_data['token_id1']
        mid = get_mid_price(token_id)
        est = usdc / mid if mid and mid > 0 else "?"
        await update.message.reply_text(f"Estimated ≈ {est} shares\n\nConfirm initial BUY? (y/n)")
        return CONFIRM_BUY
    except:
        await update.message.reply_text("Invalid amount.")
        return BUY_AMOUNT

async def confirm_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' not in update.message.text.lower():
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    token_id = context.user_data['token_id1']
    usdc = context.user_data['initial_usdc']
    place_market_order(token_id, usdc, BUY)
    bal = get_balance(token_id)
    context.user_data['balance'] = bal

    await update.message.reply_text(
        f"Initial BUY completed. New balance: {bal:.4f} shares\n\n"
        "Now set SELL amount on trigger for FROM outcome:\n"
        "1 = 25%\n2 = 50%\n3 = 100%\n4 = custom\nChoice:"
    )
    return SELL_CHOICE

async def get_sell_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ch = update.message.text.strip()
    bal = context.user_data['balance']
    if ch == '4':
        await update.message.reply_text("Enter custom shares to sell on trigger:")
        return CUSTOM_SELL
    elif ch in ['1', '2', '3']:
        percs = {'1': 0.25, '2': 0.5, '3': 1.0}
        sell_amount = bal * percs[ch]
        context.user_data['sell_amount'] = sell_amount
        mid = get_mid_price(context.user_data['token_id1'])
        est = sell_amount * mid if mid else "?"
        await update.message.reply_text(
            f"Will SELL {sell_amount:.4f} shares on trigger (≈ ${est} USDC)\n\n"
            "Also perform AUTO BUY on trigger? (y/n)"
        )
        return AUTO_BUY_YN
    else:
        await update.message.reply_text("Invalid choice.")
        return SELL_CHOICE

async def get_custom_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        shares = float(update.message.text.strip())
        bal = context.user_data['balance']
        if shares <= 0 or shares > bal:
            raise ValueError
        context.user_data['sell_amount'] = shares
        mid = get_mid_price(context.user_data['token_id1'])
        est = shares * mid if mid else "?"
        await update.message.reply_text(
            f"Will SELL {shares:.4f} shares on trigger (≈ ${est} USDC)\n\n"
            "Also perform AUTO BUY on trigger? (y/n)"
        )
        return AUTO_BUY_YN
    except:
        await update.message.reply_text("Invalid amount.")
        return CUSTOM_SELL

async def get_auto_buy_yn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' in update.message.text.lower():
        target = context.user_data.get('outcome2') or context.user_data['outcome1']
        await update.message.reply_text(f"Enter USDC amount to BUY on trigger (to {target}):")
        return AUTO_BUY_AMOUNT
    else:
        context.user_data['buy_usdc'] = 0
        msg = await build_confirm_message(context)
        await update.message.reply_text(msg)
        return START_MONITOR

async def get_auto_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
        context.user_data['buy_usdc'] = amount
        msg = await build_confirm_message(context)
        await update.message.reply_text(msg)
        return START_MONITOR
    except:
        await update.message.reply_text("Invalid amount.")
        return AUTO_BUY_AMOUNT

async def build_confirm_message(context: ContextTypes.DEFAULT_TYPE) -> str:
    from_o = context.user_data['outcome1']
    target_o = context.user_data.get('outcome2') or from_o
    sell_amt = context.user_data.get('sell_amount', 0)
    buy_usdc = context.user_data.get('buy_usdc', 0)

    msg = "On new Elon tweet:\n"
    if sell_amt > 0:
        mid_sell = get_mid_price(context.user_data['token_id1'])
        est_sell = sell_amt * mid_sell if mid_sell else "?"
        msg += f"• SELL {sell_amt:.4f} shares of {from_o} (≈ ${est_sell})\n"
    if buy_usdc > 0:
        target_token = context.user_data.get('token_id2') or context.user_data['token_id1']
        mid_buy = get_mid_price(target_token)
        est_shares = buy_usdc / mid_buy if mid_buy and mid_buy > 0 else "?"
        msg += f"• BUY ${buy_usdc:.2f} USDC of {target_o} (≈ {est_shares} shares)\n"
    if sell_amt == 0 and buy_usdc == 0:
        msg += "• No actions configured\n"

    msg += "\nStart monitoring? (y/n)"
    return msg

async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' not in update.message.text.lower():
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    context.bot_data['token_id_sell'] = context.user_data['token_id1']
    context.bot_data['sell_amount'] = context.user_data.get('sell_amount', 0)
    context.bot_data['token_id_buy'] = context.user_data.get('token_id2') or (
        context.user_data['token_id1'] if context.user_data.get('buy_usdc', 0) > 0 else None
    )
    context.bot_data['buy_usdc'] = context.user_data.get('buy_usdc', 0)
    context.bot_data['from_outcome'] = context.user_data['outcome1']
    context.bot_data['target_outcome'] = context.user_data.get('outcome2') or context.user_data['outcome1']
    context.bot_data['chat_id'] = update.effective_chat.id
    context.bot_data['monitoring'] = True

    context.application.create_task(monitor_elon_activity(context.application))
    await update.message.reply_text("🚀 Monitoring started! Will act instantly on new Elon activity.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

async def stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.bot_data['monitoring'] = False
    await update.message.reply_text("Monitoring stopped.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.bot_data.get('monitoring', False):
        await update.message.reply_text("Monitoring active.")
    else:
        await update.message.reply_text("Not monitoring.")

def main():
    custom_request = HTTPXRequest(
        connection_pool_size=20,
        read_timeout=30,
        connect_timeout=30,
        pool_timeout=30,
    )
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).request(custom_request).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SLUG: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_slug)],
            MARKET_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_market_idx)],
            NUM_OUTCOMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_num_outcomes)],
            OUTCOME1_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_outcome1_idx)],
            OUTCOME2_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_outcome2_idx)],
            ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_action)],
            BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_buy_amount)],
            CONFIRM_BUY: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_buy)],
            SELL_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sell_choice)],
            CUSTOM_SELL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_sell)],
            AUTO_BUY_YN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_auto_buy_yn)],
            AUTO_BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_auto_buy_amount)],
            START_MONITOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_monitor)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("stop", stop_monitor))
    application.add_handler(CommandHandler("status", status))

    print("Bot running. Use /start")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    print("⚠️ REAL TRADING — KEEP DRY_RUN=true UNTIL FULLY TESTED!")
    main()
