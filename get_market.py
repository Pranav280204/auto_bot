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
from datetime import datetime
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
YOUTUBE_API_KEYS_STR = os.getenv("YOUTUBE_API_KEYS", "")

if not all([PRIVATE_KEY, WALLET_ADDRESS, TELEGRAM_BOT_TOKEN]):
    raise ValueError("Missing required env variables (PRIVATE_KEY, WALLET_ADDRESS, TELEGRAM_BOT_TOKEN)")

if not YOUTUBE_API_KEYS_STR:
    raise ValueError("Missing YOUTUBE_API_KEYS (comma-separated list of 6 keys)")

YOUTUBE_API_KEYS = [k.strip() for k in YOUTUBE_API_KEYS_STR.split(",") if k.strip()]
if len(YOUTUBE_API_KEYS) == 0:
    raise ValueError("No valid YouTube API keys provided")

# Constants
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYGON_RPC = "https://polygon-rpc.com/"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CHANNEL_ID = "UCX6OQ3DkcsbYNE6H8uQQuVA"  # MrBeast
CHECK_INTERVAL = 10  # seconds

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

# Conversation states
SLUG, MARKET_IDX, NUM_OUTCOMES, OUTCOME1_IDX, OUTCOME2_IDX, ACTION, BUY_AMOUNT, CONFIRM_BUY, AUTO_BUY_YN, AUTO_BUY_AMOUNT, TARGET_SUBS, START_MONITOR = range(12)

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
                print("YouTube API quota exceeded or invalid key - rotating")
            else:
                print(f"YouTube API error: {response.status}")
    except Exception as e:
        print(f"YouTube fetch exception: {e}")
    return None, (key_index + 1) % len(YOUTUBE_API_KEYS)

async def monitor_mrbeast_subs(application: Application):
    print("MrBeast subscriber monitoring started")
    chat_id = application.bot_data['chat_id']
    target_subs = application.bot_data['target_subs']
    await safe_send_message(application.bot, chat_id, f"🚀 Monitoring MrBeast subscribers - trigger at >= {target_subs:,}")

    async with aiohttp.ClientSession() as session:
        last_subs = application.bot_data.get('last_subs')
        triggered = application.bot_data.get('triggered', False)
        key_index = application.bot_data.get('key_index', 0)

        while application.bot_data.get('monitoring', False):
            loop_start = time.time()
            current_subs, key_index = await get_subscriber_count(session, key_index)
            application.bot_data['key_index'] = key_index

            if current_subs is None:
                await safe_send_message(application.bot, chat_id, "⚠️ Failed to fetch subscriber count - retrying...")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            if last_subs is None:
                msg = f"Initial subscriber count: {current_subs:,}\n"
                if current_subs >= target_subs:
                    msg += "⚠️ Target already reached/exceeded!"
                else:
                    msg += f"Waiting for {target_subs - current_subs:,} more subscribers."
                await safe_send_message(application.bot, chat_id, msg)
            else:
                delta = current_subs - last_subs
                if delta != 0:
                    delta_str = f" (+{delta:,})" if delta > 0 else f" ({delta:,})"
                    await safe_send_message(application.bot, chat_id, f"Update: {current_subs:,}{delta_str}")

            application.bot_data['last_subs'] = current_subs

            if not triggered and current_subs >= target_subs:
                triggered = True
                application.bot_data['triggered'] = True
                trigger_msg = f"🚨 MRBEAST HIT {current_subs:,} SUBSCRIBERS! Target {target_subs:,} reached.\nExecuting trades..."
                await safe_send_message(application.bot, chat_id, trigger_msg)

                results = []
                token_id_sell = application.bot_data.get('token_id_sell')
                from_outcome = application.bot_data.get('from_outcome')

                if token_id_sell:
                    balance = get_balance(token_id_sell)
                    if balance > 0.01:
                        sell_start = time.time()
                        sell_result = place_market_order(token_id_sell, balance, SELL)
                        sell_dur = time.time() - sell_start
                        results.append(f"✅ Sold all {balance:.4f} shares of {from_outcome} (took {sell_dur:.3f}s)")
                    else:
                        results.append("No shares to sell.")

                buy_usdc = application.bot_data.get('buy_usdc', 0)
                token_id_buy = application.bot_data.get('token_id_buy')
                target_outcome = application.bot_data.get('target_outcome')

                if buy_usdc > 0 and token_id_buy:
                    buy_start = time.time()
                    buy_result = place_market_order(token_id_buy, buy_usdc, BUY)
                    buy_dur = time.time() - buy_start
                    results.append(f"✅ Bought ${buy_usdc:.2f} of {target_outcome} (took {buy_dur:.3f}s)")

                if results:
                    await safe_send_message(application.bot, chat_id, "\n".join(results))
                else:
                    await safe_send_message(application.bot, chat_id, "No actions performed.")

                await safe_send_message(application.bot, chat_id, "Monitoring stopped after successful trigger.")
                application.bot_data['monitoring'] = False
                break

            await asyncio.sleep(CHECK_INTERVAL)

# Telegram handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("MrBeast subscriber sniper bot\nEnter Polymarket event slug:")
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
        await update.message.reply_text("Select FROM outcome (usually 'No' - will sell ALL on trigger):")
        return OUTCOME1_IDX
    except:
        await update.message.reply_text("Invalid number.")
        return MARKET_IDX

async def get_outcome1_idx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        idx = int(update.message.text.strip())
        outcomes = context.user_data['outcomes']
        token_ids = context.user_data['token_ids']
        outcome = outcomes[idx]
        token_id = token_ids[idx]
        context.user_data['outcome1'] = outcome
        context.user_data['token_id1'] = token_id
        text = "Remaining outcomes:\n"
        for i, o in enumerate(outcomes):
            if i != idx:
                text += f"{i}: {o}\n"
        await update.message.reply_text(text + "\nSelect TO outcome (usually 'Yes' - will buy fixed amount on trigger):")
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
            "Initial action on FROM outcome — Buy position now or use existing? (b/s)"
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
    if action == 'b':
        await update.message.reply_text("Enter initial USDC amount to BUY on FROM outcome now:")
        return BUY_AMOUNT
    else:
        bal = get_balance(context.user_data['token_id1'])
        await update.message.reply_text(f"Current balance on FROM: {bal:.4f} shares\nProceeding to trigger setup...")
        return AUTO_BUY_YN

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
        await update.message.reply_text("Cancelled initial buy.")
        return AUTO_BUY_YN
    token_id = context.user_data['token_id1']
    usdc = context.user_data['initial_usdc']
    place_market_order(token_id, usdc, BUY)
    bal = get_balance(token_id)
    await update.message.reply_text(f"Initial BUY completed. New balance: {bal:.4f} shares")
    return AUTO_BUY_YN

async def get_auto_buy_yn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' in update.message.text.lower():
        await update.message.reply_text("Enter USDC amount to BUY on TO outcome when target reached:")
        return AUTO_BUY_AMOUNT
    else:
        context.user_data['buy_usdc'] = 0
        await update.message.reply_text("Enter target subscriber count for Yes resolution (full number, e.g. 450000000):")
        return TARGET_SUBS

async def get_auto_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
        context.user_data['buy_usdc'] = amount
        await update.message.reply_text("Enter target subscriber count for Yes resolution (full number, e.g. 450000000):")
        return TARGET_SUBS
    except:
        await update.message.reply_text("Invalid amount.")
        return AUTO_BUY_AMOUNT

async def get_target_subs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        target = int(update.message.text.strip().replace(",", ""))
        if target <= 0:
            raise ValueError
        context.user_data['target_subs'] = target
        msg = await build_confirm_message(context)
        await update.message.reply_text(msg)
        return START_MONITOR
    except:
        await update.message.reply_text("Invalid number. Enter full integer (e.g. 450000000)")
        return TARGET_SUBS

async def build_confirm_message(context: ContextTypes.DEFAULT_TYPE) -> str:
    from_o = context.user_data['outcome1']
    target_o = context.user_data['outcome2']
    buy_usdc = context.user_data.get('buy_usdc', 0)
    target_subs = context.user_data['target_subs']
    msg = f"On MrBeast reaching {target_subs:,} subscribers:\n"
    msg += f"• SELL ALL current shares of {from_o}\n"
    if buy_usdc > 0:
        msg += f"• BUY ${buy_usdc:.2f} USDC of {target_o}\n"
    else:
        msg += "• No buy on trigger\n"
    msg += "\nStart monitoring? (y/n)"
    return msg

async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' not in update.message.text.lower():
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    context.bot_data['token_id_sell'] = context.user_data['token_id1']
    context.bot_data['token_id_buy'] = context.user_data['token_id2']
    context.bot_data['buy_usdc'] = context.user_data.get('buy_usdc', 0)
    context.bot_data['from_outcome'] = context.user_data['outcome1']
    context.bot_data['target_outcome'] = context.user_data['outcome2']
    context.bot_data['target_subs'] = context.user_data['target_subs']
    context.bot_data['chat_id'] = update.effective_chat.id
    context.bot_data['monitoring'] = True
    context.bot_data['last_subs'] = None
    context.bot_data['triggered'] = False
    context.bot_data['key_index'] = 0

    context.application.create_task(monitor_mrbeast_subs(context.application))
    await update.message.reply_text("🚀 Monitoring started! Will trigger exactly once when target subscriber count is reached.")
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
            OUTCOME1_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_outcome1_idx)],
            OUTCOME2_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_outcome2_idx)],
            ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_action)],
            BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_buy_amount)],
            CONFIRM_BUY: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_buy)],
            AUTO_BUY_YN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_auto_buy_yn)],
            AUTO_BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_auto_buy_amount)],
            TARGET_SUBS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_target_subs)],
            START_MONITOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_monitor)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("stop", stop_monitor))
    application.add_handler(CommandHandler("status", status))

    print("MrBeast subscriber sniper bot running. Use /start")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    print("⚠️ REAL TRADING — KEEP DRY_RUN=true UNTIL FULLY TESTED!")
    main()
