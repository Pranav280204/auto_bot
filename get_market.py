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
from datetime import datetime, timezone
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

# Load environment variables
load_dotenv()

# Polymarket configs
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
DRY_RUN = os.getenv("DRY_RUN", "False").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")

if not PRIVATE_KEY:
    raise ValueError("PRIVATE_KEY is missing in .env")
if not WALLET_ADDRESS:
    raise ValueError("WALLET_ADDRESS is missing in .env")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing in .env")
if not TWITTER_API_KEY:
    raise ValueError("TWITTER_API_KEY is missing in .env")

# Constants
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYGON_RPC = "https://polygon-rpc.com/"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Minimal ERC-1155 ABI
ERC1155_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Initialize Web3 and ClobClient
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
print("\n⚠️ REQUIRED APPROVALS (do once):")
print("• BUY: Approve USDC to exchange contracts")
print("• SELL: setApprovalForAll on Conditional Tokens for exchange contracts")
print("Exchange addrs: 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E & 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296\n")

# Twitter monitoring configs
TARGET_ACCOUNT = "elonmusk"
CHECK_INTERVAL = 1
PRINT_FULL_TEXT = True
LAST_CHECKED_TIME = datetime.now(timezone.utc)

# Conversation states
SLUG = 0
MARKET_IDX = 1
OUTCOME_IDX = 2
ACTION = 3
BUY_AMOUNT = 4
CONFIRM_BUY = 5
SELL_CHOICE = 6
CUSTOM_SELL = 7
START_MONITOR = 8

def fetch_active_markets(slug):
    url = f"{GAMMA_API}/events/slug/{slug}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        event = response.json()
        if not event or "markets" not in event:
            return []
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
        print(f"[DRY RUN] Would place {side} for {amount} on {token_id}")
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

async def check_for_new_tweets(session, application):
    global LAST_CHECKED_TIME
    until_time = datetime.now(timezone.utc)
    since_time = LAST_CHECKED_TIME
    since_str = since_time.strftime("%Y-%m-%d_%H:%M:%S_UTC")
    until_str = until_time.strftime("%Y-%m-%d_%H:%M:%S_UTC")

    query = f"from:{TARGET_ACCOUNT} since:{since_str} until:{until_str} include:nativeretweets"
    url = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    params = {"query": query, "queryType": "Latest"}
    headers = {"X-API-Key": TWITTER_API_KEY}

    all_tweets = []
    next_cursor = None

    while True:
        if next_cursor:
            params["cursor"] = next_cursor
        async with session.get(url, headers=headers, params=params) as response:
            if response.status == 200:
                data = await response.json()
                tweets = data.get("tweets", [])
                all_tweets.extend(tweets)
                if data.get("has_next_page", False) and data.get("next_cursor"):
                    next_cursor = data.get("next_cursor")
                else:
                    break
            else:
                print(f"Twitter API Error: {response.status}")
                break

    if all_tweets:
        chat_id = application.bot_data['chat_id']
        message = f"{'═' * 60}\nDETECTED {len(all_tweets)} NEW ACTIVITIES FROM @{TARGET_ACCOUNT}!\n{'═' * 60}\n\n"
        for tweet in all_tweets:
            created_str = tweet.get('createdAt', '??')
            try:
                tweet_time = datetime.strptime(created_str, "%a %b %d %H:%M:%S %z %Y")
                latency = (until_time - tweet_time).total_seconds()
                latency_str = f"{latency:.2f}s ago"
                time_str = tweet_time.strftime('%Y-%m-%d %H:%M:%S UTC')
            except:
                latency_str = "??"
                time_str = "??"
            message += f"[{time_str}] {latency_str}\n"
            text = tweet.get('text', '').strip()
            if PRINT_FULL_TEXT:
                message += f"{text}\n"
            else:
                preview = text[:90] + "…" if len(text) > 90 else text
                message += f"{preview}\n"
            message += "─" * 70 + "\n"

        await application.bot.send_message(chat_id=chat_id, text=message)

        token_id = application.bot_data['token_id']
        sell_amount = application.bot_data['sell_amount']
        place_market_order(token_id, sell_amount, SELL)
        await application.bot.send_message(chat_id=chat_id, text="SELL ORDER PLACED!")

    LAST_CHECKED_TIME = until_time

async def monitor_elon_activity(application: Application):
    print("Monitoring loop started")
    async with aiohttp.ClientSession() as session:
        while application.bot_data.get('monitoring', False):
            start_time = time.time()
            await check_for_new_tweets(session, application)
            duration = time.time() - start_time
            print(f"Check completed in {duration:.2f}s", end="\r", flush=True)
            sleep_time = max(0, CHECK_INTERVAL - duration)
            await asyncio.sleep(sleep_time)
    print("Monitoring loop ended")

# ────────────────────────────────────────────────
# Telegram Handlers
# ────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Enter Polymarket event slug (e.g. elon-musk-of-tweets-january-16-january-23):")
    return SLUG

async def get_slug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    slug = update.message.text.strip()
    context.user_data['slug'] = slug
    markets = fetch_active_markets(slug)
    if not markets:
        await update.message.reply_text("No active markets found. Check slug or try /start again.")
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
        if idx < 0 or idx >= len(markets):
            raise ValueError
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
        text = "Outcomes:\n"
        for i, outcome in enumerate(outcomes):
            text += f"{i}: {outcome}\n"
        await update.message.reply_text(text + "\nSelect outcome index:")
        return OUTCOME_IDX
    except:
        await update.message.reply_text("Invalid number. Try again:")
        return MARKET_IDX

async def get_outcome_idx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        idx = int(update.message.text.strip())
        outcomes = context.user_data['outcomes']
        if idx < 0 or idx >= len(outcomes):
            raise ValueError
        token_ids = context.user_data['token_ids']
        token_id = token_ids[idx]
        selected = outcomes[idx]
        context.user_data['token_id'] = token_id
        context.user_data['selected'] = selected
        mid = get_mid_price(token_id)
        price_str = f"{mid:.4f} USDC" if mid else "unavailable"
        await update.message.reply_text(f"Selected: {selected} | Mid price: {price_str}\n\nBuy or Sell on trigger? (b/s)")
        return ACTION
    except:
        await update.message.reply_text("Invalid index. Try again:")
        return OUTCOME_IDX

async def get_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = update.message.text.lower().strip()
    if action not in ['b', 's']:
        await update.message.reply_text("Please enter 'b' or 's':")
        return ACTION
    context.user_data['action'] = action
    token_id = context.user_data['token_id']
    if action == 'b':
        await update.message.reply_text("Enter USDC amount to spend for initial BUY:")
        return BUY_AMOUNT
    else:
        bal = get_balance(token_id)
        context.user_data['balance'] = bal
        if bal < 0.01:
            await update.message.reply_text("No shares to sell.")
            return ConversationHandler.END
        text = f"Balance: {bal:.4f} shares\n\nSell amount on trigger:\n1 = 25%\n2 = 50%\n3 = 100%\n4 = custom\nChoice:"
        await update.message.reply_text(text)
        return SELL_CHOICE

async def get_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        usdc = float(update.message.text.strip())
        if usdc <= 0:
            raise ValueError
        context.user_data['buy_amount'] = usdc
        token_id = context.user_data['token_id']
        mid = get_mid_price(token_id)
        est = usdc / mid if mid and mid > 0 else "?"
        est_str = f"{est:.2f} shares" if isinstance(est, float) else est
        await update.message.reply_text(f"≈ {est_str}\n\nConfirm BUY? (y/n)")
        return CONFIRM_BUY
    except:
        await update.message.reply_text("Invalid amount. Try again:")
        return BUY_AMOUNT

async def confirm_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' in update.message.text.lower():
        token_id = context.user_data['token_id']
        usdc = context.user_data['buy_amount']
        place_market_order(token_id, usdc, BUY)
        sell_amount = get_balance(token_id)
        context.user_data['sell_amount'] = sell_amount
        await update.message.reply_text(f"Will auto-sell all {sell_amount:.4f} shares on Elon activity.\n\nStart monitoring? (y/n)")
        return START_MONITOR
    else:
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

async def get_sell_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ch = update.message.text.strip()
    bal = context.user_data['balance']
    if ch == '4':
        await update.message.reply_text("Enter custom shares to sell:")
        return CUSTOM_SELL
    elif ch in ['1', '2', '3']:
        if ch == '1':
            sell_amount = bal * 0.25
        elif ch == '2':
            sell_amount = bal * 0.5
        elif ch == '3':
            sell_amount = bal
        context.user_data['sell_amount'] = sell_amount
        mid = get_mid_price(context.user_data['token_id'])
        est = sell_amount * mid if mid else "?"
        est_str = f"{est:.2f} USDC" if isinstance(est, float) else est
        await update.message.reply_text(f"Will sell {sell_amount:.4f} shares ≈ {est_str}\n\nStart monitoring? (y/n)")
        return START_MONITOR
    else:
        await update.message.reply_text("Invalid choice. Enter 1-4:")
        return SELL_CHOICE

async def get_custom_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        shares = float(update.message.text.strip())
        bal = context.user_data['balance']
        if shares <= 0 or shares > bal:
            raise ValueError
        context.user_data['sell_amount'] = shares
        mid = get_mid_price(context.user_data['token_id'])
        est = shares * mid if mid else "?"
        est_str = f"{est:.2f} USDC" if isinstance(est, float) else est
        await update.message.reply_text(f"Will sell {shares:.4f} shares ≈ {est_str}\n\nStart monitoring? (y/n)")
        return START_MONITOR
    except:
        await update.message.reply_text("Invalid amount. Try again:")
        return CUSTOM_SELL

async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' in update.message.text.lower():
        context.bot_data['token_id'] = context.user_data['token_id']
        context.bot_data['sell_amount'] = context.user_data['sell_amount']
        context.bot_data['chat_id'] = update.effective_chat.id
        context.bot_data['monitoring'] = True
        context.application.create_task(monitor_elon_activity(context.application))
        await update.message.reply_text("Monitoring started! I'll notify you here on Elon activity and sells.")
        return ConversationHandler.END
    else:
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Configuration cancelled.")
    return ConversationHandler.END

async def stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.bot_data['monitoring'] = False
    await update.message.reply_text("Monitoring stopped.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.bot_data.get('monitoring', False):
        await update.message.reply_text("Monitoring is active.")
    else:
        await update.message.reply_text("Not monitoring.")

# ────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SLUG: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_slug)],
            MARKET_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_market_idx)],
            OUTCOME_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_outcome_idx)],
            ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_action)],
            BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_buy_amount)],
            CONFIRM_BUY: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_buy)],
            SELL_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sell_choice)],
            CUSTOM_SELL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_sell)],
            START_MONITOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_monitor)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("stop", stop_monitor))
    application.add_handler(CommandHandler("status", status))

    print("Telegram bot started. Use /start in your chat.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    print("⚠️ REAL MONEY TRADING — keep DRY_RUN=true until tested!")
    main()