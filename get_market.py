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
import time
from datetime import datetime, timezone

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
YOUTUBE_API_KEYS_STR = os.getenv("YOUTUBE_API_KEYS", "")

if not all([PRIVATE_KEY, WALLET_ADDRESS, TELEGRAM_BOT_TOKEN]):
    raise ValueError("Missing required env variables (PRIVATE_KEY, WALLET_ADDRESS, TELEGRAM_BOT_TOKEN)")

if not YOUTUBE_API_KEYS_STR:
    raise ValueError("Missing YOUTUBE_API_KEYS (comma-separated list of keys)")

YOUTUBE_API_KEYS = [k.strip() for k in YOUTUBE_API_KEYS_STR.split(",") if k.strip()]
if len(YOUTUBE_API_KEYS) == 0:
    raise ValueError("No valid YouTube API keys provided")

# Constants
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYGON_RPC = "https://polygon-rpc.com/"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CHANNEL_ID = "UCX6OQ3DkcsbYNE6H8uQQuVA"  # MrBeast
CHECK_INTERVAL = 5  # YouTube updates are delayed; no need for sub-second polling

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
    signature_type=1,  # Critical fix — EIP-712 signing required for Polymarket
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
print("IMPORTANT: Ensure USDC & conditional token approvals are set!")

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

def get_mid_price(token_id: str):
    url = f"https://clob.polymarket.com/orderbook?token_id={token_id}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        bids = [float(b[0]) for b in data.get("bids", [])]
        asks = [float(a[0]) for a in data.get("asks", [])]
        if bids and asks:
            return (max(bids) + min(asks)) / 2
        elif bids:
            return max(bids)
        elif asks:
            return min(asks)
        return None
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
        print("Order response:", json.dumps(resp, indent=2))
        return resp
    except Exception as e:
        error_msg = str(e)
        print(f"Order placement failed: {error_msg}")
        return {"error": error_msg}

async def safe_send_message(bot, chat_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        print(f"Send message error: {e}")

async def get_subscriber_count(session, key_index):
    key = YOUTUBE_API_KEYS[key_index % len(YOUTUBE_API_KEYS)]
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {
        "part": "statistics",
        "id": CHANNEL_ID,
        "key": key
    }
    try:
        async with session.get(url, params=params, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("items"):
                    stats = data["items"][0].get("statistics", {})
                    if "subscriberCount" in stats:
                        return int(stats["subscriberCount"]), (key_index + 1) % len(YOUTUBE_API_KEYS)
            elif response.status == 403:
                print("YouTube API quota exceeded — rotating key")
            else:
                print(f"YouTube API error: {response.status}")
    except Exception as e:
        print(f"YouTube fetch exception: {e}")
    return None, (key_index + 1) % len(YOUTUBE_API_KEYS)

async def monitor_subscriber_increase(application: Application):
    print("MrBeast subscriber monitoring started")
    chat_id = application.bot_data['chat_id']
    market_question = application.bot_data.get('market_question', 'Selected market')

    await safe_send_message(application.bot, chat_id, f"🚀 Monitoring MrBeast subscribers!\nMarket: {market_question}\nWaiting for first increase...")

    async with aiohttp.ClientSession() as session:
        last_subs = None
        triggered = False
        key_index = application.bot_data.get('key_index', 0)
        initial_set = False

        while application.bot_data.get('monitoring', False):
            current_subs, key_index = await get_subscriber_count(session, key_index)
            application.bot_data['key_index'] = key_index

            if current_subs is None:
                await safe_send_message(application.bot, chat_id, "⚠️ Failed to fetch subscriber count — retrying...")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            delta_str = ""
            if last_subs is not None:
                delta = current_subs - last_subs
                delta_str = f" ({delta:+,})" if delta != 0 else " (no change)"

            await safe_send_message(application.bot, chat_id, f"Current: {current_subs:,} subscribers{delta_str}")

            if not initial_set:
                last_subs = current_subs
                initial_set = True
                await safe_send_message(application.bot, chat_id, f"Baseline set: {current_subs:,} subscribers\nWaiting for increase...")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            if current_subs > last_subs and not triggered:
                triggered = True
                delta = current_subs - last_subs
                total_start = time.time()
                results = []

                token_id_sell = application.bot_data.get('token_id_sell')
                sell_amount = application.bot_data.get('sell_amount', 0)
                from_outcome = application.bot_data.get('from_outcome')

                # SELL first
                if sell_amount > 0 and token_id_sell:
                    balance = get_balance(token_id_sell)
                    actual_sell = min(balance, sell_amount)
                    if actual_sell > 0.01:
                        sell_start = time.time()
                        sell_result = place_market_order(token_id_sell, actual_sell, SELL)
                        sell_dur = time.time() - sell_start
                        if sell_result.get("status") == "dry_run" or (sell_result and "error" not in sell_result):
                            prefix = "[DRY RUN] " if DRY_RUN else ""
                            results.append(f"{prefix}✅ SOLD {actual_sell:.4f} shares of {from_outcome} (took {sell_dur:.3f}s)")
                        else:
                            err = sell_result.get("error", "Unknown error") if sell_result else "No response"
                            results.append(f"❌ SELL FAILED: {err} (took {sell_dur:.3f}s)")
                    else:
                        results.append("⚠️ Insufficient balance to sell")

                # Then BUY
                token_id_buy = application.bot_data.get('token_id_buy')
                buy_usdc = application.bot_data.get('buy_usdc', 0)
                target_outcome = application.bot_data.get('target_outcome')

                if buy_usdc > 0 and token_id_buy:
                    buy_start = time.time()
                    buy_result = place_market_order(token_id_buy, buy_usdc, BUY)
                    buy_dur = time.time() - buy_start
                    if buy_result.get("status") == "dry_run" or (buy_result and "error" not in buy_result):
                        prefix = "[DRY RUN] " if DRY_RUN else ""
                        results.append(f"{prefix}✅ BOUGHT ${buy_usdc:.2f} of {target_outcome} (took {buy_dur:.3f}s)")
                    else:
                        err = buy_result.get("error", "Unknown error") if buy_result else "No response"
                        results.append(f"❌ BUY FAILED: {err} (took {buy_dur:.3f}s)")

                total_dur = time.time() - total_start
                trigger_msg = f"🚨 SUBSCRIBER INCREASE DETECTED! +{delta:,} subs\nTotal execution: {total_dur:.3f}s\n\n"
                trigger_msg += "\n".join(results) if results else "No actions performed."

                await safe_send_message(application.bot, chat_id, trigger_msg)
                await safe_send_message(application.bot, chat_id, "Monitoring stopped after trigger.")
                application.bot_data['monitoring'] = False
                break

            last_subs = current_subs
            await asyncio.sleep(CHECK_INTERVAL)

# Telegram handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("MrBeast Subscriber Increase Sniper Bot 🚀\n\nEnter Polymarket event slug:")
    return SLUG

async def get_slug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    slug = update.message.text.strip()
    context.user_data['slug'] = slug
    markets = fetch_active_markets(slug)
    if not markets:
        await update.message.reply_text("No active markets found for this slug.")
        return ConversationHandler.END
    text = f"Found {len(markets)} active market(s):\n\n"
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
        await update.message.reply_text("How many outcomes to trade on? (1 or 2)")
        return NUM_OUTCOMES
    except:
        await update.message.reply_text("Invalid market number.")
        return MARKET_IDX

async def get_num_outcomes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text not in ["1", "2"]:
        await update.message.reply_text("Please enter 1 or 2.")
        return NUM_OUTCOMES
    context.user_data['num_outcomes'] = int(text)
    await update.message.reply_text("Select FROM outcome (current position):")
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

        mid = get_mid_price(token_id)
        price_str = f"{mid:.4f}" if mid else "N/A"

        if context.user_data['num_outcomes'] == 1:
            await update.message.reply_text(
                f"Selected: {outcome} | Mid price: {price_str}\n\n"
                "Initial action — Buy new position or use existing to Sell on trigger? (b/s)"
            )
            return ACTION
        else:
            text = "Remaining outcomes:\n"
            for i, o in enumerate(outcomes):
                if i != idx:
                    text += f"{i}: {o}\n"
            await update.message.reply_text(text + "\nSelect TO outcome (target on trigger):")
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
            "Initial action on FROM — Buy new or use existing Sell on trigger? (b/s)"
        )
        return ACTION
    except:
        await update.message.reply_text("Invalid index.")
        return OUTCOME2_IDX

async def get_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = update.message.text.lower().strip()
    if action not in ['b', 's']:
        await update.message.reply_text("Please enter 'b' (buy) or 's' (sell existing).")
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
        est = usdc / mid if mid and mid > 0 else "N/A"
        await update.message.reply_text(f"Estimated ≈ {est:.2f} shares\n\nConfirm initial BUY? (y/n)")
        return CONFIRM_BUY
    except:
        await update.message.reply_text("Invalid amount.")
        return BUY_AMOUNT

async def confirm_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' not in update.message.text.lower():
        await update.message.reply_text("Initial buy cancelled.")
        return ConversationHandler.END

    token_id = context.user_data['token_id1']
    usdc = context.user_data['initial_usdc']
    result = place_market_order(token_id, usdc, BUY)
    bal = get_balance(token_id)
    context.user_data['balance'] = bal

    if result.get("status") == "dry_run" or (result and "error" not in result):
        prefix = "[DRY RUN] " if DRY_RUN else ""
        await update.message.reply_text(
            f"{prefix}✅ Initial BUY executed. New balance: {bal:.4f} shares\n\n"
            "Now set SELL amount on trigger:\n1=25% 2=50% 3=100% 4=custom\nChoice:"
        )
    else:
        err = result.get("error", "Unknown error")
        await update.message.reply_text(
            f"❌ Initial BUY FAILED: {err}\nUsing current balance {bal:.4f} anyway.\n\n"
            "Set SELL amount on trigger:\n1=25% 2=50% 3=100% 4=custom\nChoice:"
        )
    return SELL_CHOICE

async def get_sell_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ch = update.message.text.strip()
    bal = context.user_data['balance']
    if ch == '4':
        await update.message.reply_text("Enter custom shares to sell on trigger:")
        return CUSTOM_SELL
    elif ch in ['1', '2', '3']:
        percs = {'1': 0.25, '2': 0.50, '3': 1.00}
        sell_amount = bal * percs[ch]
        context.user_data['sell_amount'] = sell_amount
        mid = get_mid_price(context.user_data['token_id1'])
        est = sell_amount * mid if mid else "N/A"
        await update.message.reply_text(
            f"Will sell {sell_amount:.4f} shares on trigger (≈ ${est})\n\n"
            "Also auto-buy TO outcome on trigger? (y/n)"
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
        est = shares * mid if mid else "N/A"
        await update.message.reply_text(
            f"Will sell {shares:.4f} shares on trigger (≈ ${est})\n\n"
            "Also auto-buy TO outcome on trigger? (y/n)"
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

    msg = "On MrBeast subscriber increase:\n"
    if sell_amt > 0:
        mid_sell = get_mid_price(context.user_data['token_id1'])
        est_sell = sell_amt * mid_sell if mid_sell else "N/A"
        msg += f"• SELL {sell_amt:.4f} shares of {from_o} (≈ ${est_sell})\n"
    if buy_usdc > 0:
        target_token = context.user_data.get('token_id2') or context.user_data['token_id1']
        mid_buy = get_mid_price(target_token)
        est_shares = buy_usdc / mid_buy if mid_buy and mid_buy > 0 else "N/A"
        msg += f"• BUY ${buy_usdc:.2f} of {target_o} (≈ {est_shares} shares)\n"
    if sell_amt == 0 and buy_usdc == 0:
        msg += "• No actions configured\n"

    msg += "\nStart monitoring? (y/n)"
    return msg

async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' not in update.message.text.lower():
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    application = context.application
    application.bot_data['token_id_sell'] = context.user_data['token_id1']
    application.bot_data['sell_amount'] = context.user_data.get('sell_amount', 0)
    application.bot_data['token_id_buy'] = context.user_data.get('token_id2') or (
        context.user_data['token_id1'] if context.user_data.get('buy_usdc', 0) > 0 else None
    )
    application.bot_data['buy_usdc'] = context.user_data.get('buy_usdc', 0)
    application.bot_data['from_outcome'] = context.user_data['outcome1']
    application.bot_data['target_outcome'] = context.user_data.get('outcome2') or context.user_data['outcome1']
    application.bot_data['chat_id'] = update.effective_chat.id
    application.bot_data['market_question'] = context.user_data['market'].get('question', 'Unknown')
    application.bot_data['monitoring'] = True

    application.create_task(monitor_subscriber_increase(application))
    await update.message.reply_text("🚀 Monitoring started! Will execute instantly on subscriber increase.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

async def stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update.effective_application.bot_data['monitoring'] = False
    await update.message.reply_text("Monitoring stopped.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.application.bot_data.get('monitoring', False):
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

    print("MrBeast Subscriber Sniper Bot ready. Use /start")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    print("⚠️ REAL TRADING — KEEP DRY_RUN=true UNTIL FULLY TESTED!")
    main()
