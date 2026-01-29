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
from telegram.request import HTTPXRequest

load_dotenv()

# Config
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
DRY_RUN = os.getenv("DRY_RUN", "False").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
YOUTUBE_API_KEYS = [k.strip() for k in os.getenv("YOUTUBE_API_KEYS", "").split(",") if k.strip()]
CHANNEL_ID = "UCX6OQ3DkcsbYNE6H8uQQuVA"  # MrBeast

if not all([PRIVATE_KEY, WALLET_ADDRESS, TELEGRAM_BOT_TOKEN, YOUTUBE_API_KEYS]):
    raise ValueError("Missing required env variables (add YOUTUBE_API_KEYS=key1,key2,...)")

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

# Global monitoring vars
LAST_SUB_COUNT = None
LAST_NOTIFY_TIME = 0
key_idx = 0

def get_next_youtube_key():
    global key_idx
    key = YOUTUBE_API_KEYS[key_idx % len(YOUTUBE_API_KEYS)]
    key_idx += 1
    return key

# YouTube subscriber fetch
async def get_subscriber_count(session):
    key = get_next_youtube_key()
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {"part": "statistics", "id": CHANNEL_ID, "key": key}
    try:
        async with session.get(url, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("items", [])
                if items and "statistics" in items[0] and "subscriberCount" in items[0]["statistics"]:
                    return int(items[0]["statistics"]["subscriberCount"])
    except Exception as e:
        print(f"YouTube API error: {e}")
    return None

# Utility functions (same as original)
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

# Trigger action executor (sell first, then buy)
async def execute_trigger(application):
    chat_id = application.bot_data['chat_id']
    token_id_sell = application.bot_data.get('token_id_sell')
    sell_amount = application.bot_data.get('sell_amount', 0)
    token_id_buy = application.bot_data.get('token_id_buy')
    buy_usdc = application.bot_data.get('buy_usdc', 0)
    from_outcome = application.bot_data.get('from_outcome')
    target_outcome = "YES"

    results = []
    # SELL first (priority)
    if sell_amount > 0 and token_id_sell:
        bal = get_balance(token_id_sell)
        actual_sell = min(sell_amount, bal)
        if actual_sell > 0:
            start = time.time()
            place_market_order(token_id_sell, actual_sell, SELL)
            dur = time.time() - start
            results.append(f"✅ SOLD {actual_sell:.4f} shares of {from_outcome} (took {dur:.3f}s)")

    # BUY second
    if buy_usdc > 0 and token_id_buy:
        start = time.time()
        place_market_order(token_id_buy, buy_usdc, BUY)
        dur = time.time() - start
        results.append(f"✅ BOUGHT ${buy_usdc:.2f} USDC of {target_outcome} (took {dur:.3f}s)")

    if results:
        msg = "🚨 SUB COUNT CHANGED — TRADES EXECUTED!\n" + "\n".join(results)
        await safe_send_message(application.bot, chat_id, msg)

# Monitoring loop
async def monitor_sub_count(application: Application):
    global LAST_SUB_COUNT, LAST_NOTIFY_TIME
    async with aiohttp.ClientSession() as session:
        # Initial fetch
        sub = await get_subscriber_count(session)
        if sub is not None:
            LAST_SUB_COUNT = sub
            print(f"Initial sub count: {sub:,}")
        else:
            print("Initial sub fetch failed")

        while application.bot_data.get('monitoring', False):
            loop_start = time.time()
            current_sub = await get_subscriber_count(session)
            if current_sub is None:
                await asyncio.sleep(2)
                continue

            changed = current_sub != LAST_SUB_COUNT
            if changed and LAST_SUB_COUNT is not None:
                print(f"CHANGE DETECTED: {LAST_SUB_COUNT:,} → {current_sub:,}")
                await execute_trigger(application)
                LAST_SUB_COUNT = current_sub

            # Notify every 10s
            now = time.time()
            if now - LAST_NOTIFY_TIME >= 10:
                msg = f"MrBeast subscribers: {current_sub:,} (updated {datetime.now().strftime('%H:%M:%S')})"
                await safe_send_message(application.bot, application.bot_data['chat_id'], msg)
                LAST_NOTIFY_TIME = now

            duration = time.time() - loop_start
            await asyncio.sleep(max(0, 2 - duration))

    print("Monitoring stopped")

# Telegram conversation states
SLUG, MARKET_IDX, MODE_CHOICE, OUTCOME_IDX, AMOUNT_BUY, SELL_PERCENT, CUSTOM_SELL, TRIGGER_OUTCOME, SELL_CHOICE_TRIGGER, CUSTOM_SELL_TRIGGER, AUTO_BUY_YN, AUTO_BUY_AMOUNT, START_MONITOR = range(13)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Enter Polymarket event slug (e.g., mrbeast-subscriber-count-xxx):")
    return SLUG

async def get_slug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    slug = update.message.text.strip()
    context.user_data['slug'] = slug
    markets = fetch_active_markets(slug)
    if not markets:
        await update.message.reply_text("No active markets found for this slug.")
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
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        token_ids = market.get("clobTokenIds", [])
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
        if len(outcomes) != 2 or len(token_ids) != 2:
            await update.message.reply_text("This bot supports binary (YES/NO) markets only.")
            return ConversationHandler.END
        context.user_data['outcomes'] = outcomes  # 0: YES, 1: NO
        context.user_data['token_ids'] = token_ids
        text = "Choose mode:\n1. Normal buy\n2. Normal sell\n3. Trigger action"
        await update.message.reply_text(text)
        return MODE_CHOICE
    except:
        await update.message.reply_text("Invalid number.")
        return MARKET_IDX

# Mode choice
async def get_mode_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ch = update.message.text.strip()
    if ch == '1':
        context.user_data['mode'] = 'buy'
        text = "Select outcome to BUY:\n0: YES\n1: NO\nEnter number:"
        await update.message.reply_text(text)
        return OUTCOME_IDX
    elif ch == '2':
        context.user_data['mode'] = 'sell'
        text = "Select outcome to SELL:\n0: YES\n1: NO\nEnter number:"
        await update.message.reply_text(text)
        return OUTCOME_IDX
    elif ch == '3':
        text = "Select outcome to SELL on trigger (usually 1 = NO):\n0: YES\n1: NO\nEnter number:"
        await update.message.reply_text(text)
        return TRIGGER_OUTCOME
    else:
        await update.message.reply_text("Choose 1, 2, or 3.")
        return MODE_CHOICE

# Normal buy/sell outcome select
async def get_outcome_normal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        idx = int(update.message.text.strip())
        if idx not in [0, 1]:
            raise ValueError
        outcome = context.user_data['outcomes'][idx]
        token_id = context.user_data['token_ids'][idx]
        context.user_data['selected_token'] = token_id
        context.user_data['selected_outcome'] = outcome
        mode = context.user_data['mode']
        if mode == 'buy':
            await update.message.reply_text(f"Buy {outcome}\nEnter USDC amount:")
            return AMOUNT_BUY
        else:  # sell
            bal = get_balance(token_id)
            if bal < 0.01:
                await update.message.reply_text("Insufficient balance.")
                return ConversationHandler.END
            context.user_data['balance'] = bal
            text = f"Balance: {bal:.4f} shares of {outcome}\nSell % on trigger:\n1=25% 2=50% 3=100% 4=custom\nChoice:"
            await update.message.reply_text(text)
            return SELL_PERCENT
    except:
        await update.message.reply_text("Invalid (0 or 1).")
        return OUTCOME_IDX

async def get_amount_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        usdc = float(update.message.text.strip())
        if usdc <= 0:
            raise ValueError
        token_id = context.user_data['selected_token']
        place_market_order(token_id, usdc, BUY)
        await update.message.reply_text("Normal BUY placed.")
        return ConversationHandler.END
    except:
        await update.message.reply_text("Invalid amount.")
        return AMOUNT_BUY

async def get_sell_percent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ch = update.message.text.strip()
    bal = context.user_data['balance']
    token_id = context.user_data['selected_token']
    outcome = context.user_data['selected_outcome']
    if ch == '4':
        await update.message.reply_text("Enter custom shares to sell:")
        return CUSTOM_SELL
    elif ch in ['1','2','3']:
        percs = {'1':0.25, '2':0.5, '3':1.0}
        shares = bal * percs[ch]
        place_market_order(token_id, shares, SELL)
        await update.message.reply_text(f"Normal SELL of {shares:.4f} shares ({outcome}) placed.")
        return ConversationHandler.END
    else:
        await update.message.reply_text("Invalid choice.")
        return SELL_PERCENT

async def get_custom_sell_normal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        shares = float(update.message.text.strip())
        bal = context.user_data['balance']
        if shares <= 0 or shares > bal:
            raise ValueError
        token_id = context.user_data['selected_token']
        place_market_order(token_id, shares, SELL)
        await update.message.reply_text(f"Normal SELL of {shares:.4f} shares placed.")
        return ConversationHandler.END
    except:
        await update.message.reply_text("Invalid amount.")
        return CUSTOM_SELL

# Trigger setup
async def get_trigger_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        idx = int(update.message.text.strip())
        if idx not in [0, 1]:
            raise ValueError
        token_id_sell = context.user_data['token_ids'][idx]
        outcome = context.user_data['outcomes'][idx]
        context.user_data['token_id_sell'] = token_id_sell
        context.user_data['from_outcome'] = outcome
        bal = get_balance(token_id_sell)
        if bal < 0.01:
            await update.message.reply_text("No shares to sell on trigger.")
            return ConversationHandler.END
        context.user_data['balance'] = bal
        text = f"SELL from {outcome} (bal {bal:.4f})\n% on sub change:\n1=25% 2=50% 3=100% 4=custom\nChoice:"
        await update.message.reply_text(text)
        return SELL_CHOICE_TRIGGER
    except:
        await update.message.reply_text("Invalid (0 or 1).")
        return TRIGGER_OUTCOME

async def get_sell_choice_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ch = update.message.text.strip()
    bal = context.user_data['balance']
    if ch == '4':
        await update.message.reply_text("Enter custom shares to sell on trigger:")
        return CUSTOM_SELL_TRIGGER
    elif ch in ['1','2','3']:
        percs = {'1':0.25, '2':0.5, '3':1.0}
        sell_amt = bal * percs[ch]
        context.user_data['sell_amount'] = sell_amt
        mid = get_mid_price(context.user_data['token_id_sell'])
        est = sell_amt * mid if mid else "?"
        await update.message.reply_text(f"SELL {sell_amt:.4f} shares on trigger (≈${est})\nAuto BUY YES after sell? (y/n)")
        return AUTO_BUY_YN
    else:
        await update.message.reply_text("Invalid.")
        return SELL_CHOICE_TRIGGER

async def get_custom_sell_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        shares = float(update.message.text.strip())
        bal = context.user_data['balance']
        if shares <= 0 or shares > bal:
            raise ValueError
        context.user_data['sell_amount'] = shares
        mid = get_mid_price(context.user_data['token_id_sell'])
        est = shares * mid if mid else "?"
        await update.message.reply_text(f"SELL {shares:.4f} shares on trigger (≈${est})\nAuto BUY YES after sell? (y/n)")
        return AUTO_BUY_YN
    except:
        await update.message.reply_text("Invalid.")
        return CUSTOM_SELL_TRIGGER

async def get_auto_buy_yn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' in update.message.text.lower():
        await update.message.reply_text("Enter USDC amount to BUY YES on trigger:")
        return AUTO_BUY_AMOUNT
    else:
        context.user_data['buy_usdc'] = 0
        msg = await build_confirm_message(context)
        await update.message.reply_text(msg)
        return START_MONITOR

async def get_auto_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        usdc = float(update.message.text.strip())
        if usdc <= 0:
            raise ValueError
        context.user_data['buy_usdc'] = usdc
        msg = await build_confirm_message(context)
        await update.message.reply_text(msg)
        return START_MONITOR
    except:
        await update.message.reply_text("Invalid amount.")
        return AUTO_BUY_AMOUNT

async def build_confirm_message(context: ContextTypes.DEFAULT_TYPE) -> str:
    from_o = context.user_data['from_outcome']
    sell_amt = context.user_data.get('sell_amount', 0)
    buy_usdc = context.user_data.get('buy_usdc', 0)
    msg = "On MrBeast sub count change:\n"
    if sell_amt > 0:
        mid = get_mid_price(context.user_data['token_id_sell'])
        est = sell_amt * mid if mid else "?"
        msg += f"• SELL {sell_amt:.4f} shares of {from_o} (≈${est})\n"
    if buy_usdc > 0:
        mid_buy = get_mid_price(context.user_data['token_ids'][0])
        est_shares = buy_usdc / mid_buy if mid_buy and mid_buy > 0 else "?"
        msg += f"• BUY ${buy_usdc:.2f} USDC of YES (≈{est_shares} shares)\n"
    if sell_amt == 0 and buy_usdc == 0:
        msg += "• No actions\n"
    msg += "\nStart monitoring? (y/n)"
    return msg

async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' not in update.message.text.lower():
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END
    context.bot_data['token_id_sell'] = context.user_data['token_id_sell']
    context.bot_data['sell_amount'] = context.user_data.get('sell_amount', 0)
    context.bot_data['token_id_buy'] = context.user_data['token_ids'][0]  # YES
    context.bot_data['buy_usdc'] = context.user_data.get('buy_usdc', 0)
    context.bot_data['from_outcome'] = context.user_data['from_outcome']
    context.bot_data['chat_id'] = update.effective_chat.id
    context.bot_data['monitoring'] = True
    context.application.create_task(monitor_sub_count(context.application))
    await update.message.reply_text("🚀 Monitoring MrBeast subs started! Trading on count changes.")
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
            MODE_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_mode_choice)],
            OUTCOME_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_outcome_normal)],
            AMOUNT_BUY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_amount_buy)],
            SELL_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sell_percent)],
            CUSTOM_SELL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_sell_normal)],
            TRIGGER_OUTCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trigger_outcome)],
            SELL_CHOICE_TRIGGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sell_choice_trigger)],
            CUSTOM_SELL_TRIGGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_sell_trigger)],
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
    print("⚠️ REAL TRADING — KEEP DRY_RUN=true UNTIL TESTED!")
    main()
