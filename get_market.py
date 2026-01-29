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
CHECK_INTERVAL = 2  # seconds

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

# Conversation states (no target subs)
SLUG, MARKET_IDX, SELL_OUTCOME_IDX, SELL_CHOICE, AUTO_BUY_YN, AUTO_BUY_AMOUNT, START_MONITOR = range(7)

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
    market_question = application.bot_data.get('market_question', 'Unknown market')

    await safe_send_message(application.bot, chat_id, f"🚀 Monitoring started!\nMarket: {market_question}\nWaiting for first subscriber increase...")

    async with aiohttp.ClientSession() as session:
        last_subs = None
        triggered = False
        key_index = application.bot_data.get('key_index', 0)
        initial_set = False

        while application.bot_data.get('monitoring', False):
            loop_start = time.time()
            current_subs, key_index = await get_subscriber_count(session, key_index)
            application.bot_data['key_index'] = key_index

            if current_subs is None:
                await safe_send_message(application.bot, chat_id, "⚠️ Failed to fetch subscriber count - retrying...")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # Send update every ~10s
            update_msg = f"Current: {current_subs:,} subscribers"
            if last_subs is not None:
                delta = current_subs - last_subs
                if delta != 0:
                    update_msg += f"\nChange: {delta:+,} subs"
                else:
                    update_msg += "\nNo change yet"
            else:
                update_msg += "\n(Initial count set - waiting for increase)"

            if not initial_set and last_subs is None:
                update_msg += "\nWaiting for first increase to trigger..."

            await safe_send_message(application.bot, chat_id, update_msg)

            # Set initial after first successful fetch
            if not initial_set:
                last_subs = current_subs
                initial_set = True
                await safe_send_message(application.bot, chat_id, f"Baseline set: {current_subs:,} subs\nMonitoring for increases...")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # Trigger on increase
            if not triggered and current_subs > last_subs:
                triggered = True
                delta = current_subs - last_subs
                trigger_start = time.time()

                results = []
                token_id_sell = application.bot_data.get('token_id_sell')
                sell_perc = application.bot_data.get('sell_perc', 0)
                from_outcome = application.bot_data.get('from_outcome')

                # PRIORITY 1: SELL first
                sell_dur = 0.0
                if sell_perc > 0 and token_id_sell:
                    balance = get_balance(token_id_sell)
                    if balance > 0.01:
                        sell_amount = balance * sell_perc
                        sell_start = time.time()
                        sell_result = place_market_order(token_id_sell, sell_amount, SELL)
                        sell_dur = time.time() - sell_start
                        results.append(f"✅ SOLD {sell_amount:.4f} shares ({sell_perc*100:.0f}%) of {from_outcome} (took {sell_dur:.3f}s)")
                    else:
                        results.append("⚠️ No shares to sell.")

                # PRIORITY 2: Then BUY Yes
                buy_dur = 0.0
                buy_usdc = application.bot_data.get('buy_usdc', 0)
                token_id_buy = application.bot_data.get('token_id_buy')
                target_outcome = application.bot_data.get('target_outcome')

                if buy_usdc > 0 and token_id_buy:
                    buy_start = time.time()
                    buy_result = place_market_order(token_id_buy, buy_usdc, BUY)
                    buy_dur = time.time() - buy_start
                    results.append(f"✅ BOUGHT ${buy_usdc:.2f} of {target_outcome} (took {buy_dur:.3f}s)")

                total_dur = time.time() - trigger_start
                trigger_msg = f"🚨 SUBSCRIBER INCREASE DETECTED!\nFrom {last_subs:,} → {current_subs:,} (+{delta:,})\nTotal execution time: {total_dur:.3f}s\n\n"
                if results:
                    trigger_msg += "\n".join(results)
                else:
                    trigger_msg += "No actions performed."

                await safe_send_message(application.bot, chat_id, trigger_msg)
                await safe_send_message(application.bot, chat_id, "Monitoring stopped after trigger.")
                application.bot_data['monitoring'] = False
                break

            last_subs = current_subs
            await asyncio.sleep(CHECK_INTERVAL)

# Telegram handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("MrBeast subscriber sniper bot\n\n1) Enter Polymarket event slug:")
    return SLUG

async def get_slug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    slug = update.message.text.strip()
    context.user_data['slug'] = slug
    markets = fetch_active_markets(slug)
    if not markets:
        await update.message.reply_text("No active markets found for this slug.")
        return ConversationHandler.END

    text = "2) Select which range/market to trade:\n"
    for i, m in enumerate(markets):
        text += f"{i}: {m.get('question', 'Unknown')}\n"
    await update.message.reply_text(text)
    return MARKET_IDX

async def get_market_idx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        idx = int(update.message.text.strip())
        markets = fetch_active_markets(context.user_data['slug'])
        market = markets[idx]
        context.user_data['market'] = market
        context.user_data['market_question'] = market.get('question', 'Unknown market')

        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        token_ids = market.get("clobTokenIds", [])
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)

        if len(outcomes) != 2 or len(token_ids) != 2:
            await update.message.reply_text("This bot only supports binary Yes/No markets.")
            return ConversationHandler.END

        context.user_data['outcomes'] = outcomes
        context.user_data['token_ids'] = token_ids

        text = "3) Select outcome to SELL on trigger (0 for Yes, 1 for No - usually 1):\n"
        for i, o in enumerate(outcomes):
            text += f"{i}: {o}\n"
        await update.message.reply_text(text)
        return SELL_OUTCOME_IDX
    except:
        await update.message.reply_text("Invalid market number.")
        return MARKET_IDX

async def get_sell_outcome_idx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        idx = int(update.message.text.strip())
        if idx not in [0, 1]:
            raise ValueError
        outcomes = context.user_data['outcomes']
        token_ids = context.user_data['token_ids']

        context.user_data['sell_idx'] = idx
        context.user_data['outcome_sell'] = outcomes[idx]
        context.user_data['token_id_sell'] = token_ids[idx]

        # Opposite for potential buy
        opposite_idx = 1 - idx
        context.user_data['outcome_buy'] = outcomes[opposite_idx]
        context.user_data['token_id_buy'] = token_ids[opposite_idx]

        balance = get_balance(token_ids[idx])
        context.user_data['current_balance'] = balance

        await update.message.reply_text(
            f"Selected to SELL: {outcomes[idx]}\n"
            f"Current balance: {balance:.4f} shares\n\n"
            "4) How much to sell on trigger (% of balance at trigger time):\n"
            "1 = 25%\n2 = 50%\n3 = 100%\nChoice:"
        )
        return SELL_CHOICE
    except:
        await update.message.reply_text("Invalid outcome index (0 or 1).")
        return SELL_OUTCOME_IDX

async def get_sell_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ch = update.message.text.strip()
    if ch not in ['1', '2', '3']:
        await update.message.reply_text("Please choose 1, 2, or 3.")
        return SELL_CHOICE

    percs = {'1': 0.25, '2': 0.50, '3': 1.00}
    perc = percs[ch]
    context.user_data['sell_perc'] = perc

    balance = context.user_data['current_balance']
    mid = get_mid_price(context.user_data['token_id_sell'])
    est_usd = balance * perc * mid if mid else "?"

    await update.message.reply_text(
        f"Will SELL {perc*100:.0f}% ({balance * perc:.4f} shares ≈ ${est_usd}) of {context.user_data['outcome_sell']}\n\n"
        f"5) Buy opposite outcome ('{context.user_data['outcome_buy']}') on trigger after selling? (y/n)"
    )
    return AUTO_BUY_YN

async def get_auto_buy_yn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' in update.message.text.lower():
        await update.message.reply_text("Enter USDC amount to BUY on opposite outcome:")
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
    msg = f"Market: {context.user_data['market_question']}\n\n"
    msg += "Trigger: On first detected subscriber increase\n"
    msg += f"• SELL {context.user_data['sell_perc']*100:.0f}% of {context.user_data['outcome_sell']}\n"
    if context.user_data.get('buy_usdc', 0) > 0:
        msg += f"• BUY ${context.user_data['buy_usdc']:.2f} of {context.user_data['outcome_buy']}\n"
    else:
        msg += "• No buy on opposite outcome\n"
    msg += "\nStart monitoring? (y/n)"
    return msg

async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'y' not in update.message.text.lower():
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    context.bot_data['token_id_sell'] = context.user_data['token_id_sell']
    context.bot_data['sell_perc'] = context.user_data['sell_perc']
    context.bot_data['token_id_buy'] = context.user_data['token_id_buy'] if context.user_data.get('buy_usdc', 0) > 0 else None
    context.bot_data['buy_usdc'] = context.user_data.get('buy_usdc', 0)
    context.bot_data['from_outcome'] = context.user_data['outcome_sell']
    context.bot_data['target_outcome'] = context.user_data['outcome_buy'] if context.user_data.get('buy_usdc', 0) > 0 else None
    context.bot_data['market_question'] = context.user_data['market_question']
    context.bot_data['chat_id'] = update.effective_chat.id
    context.bot_data['monitoring'] = True
    context.bot_data['key_index'] = 0

    context.application.create_task(monitor_mrbeast_subs(context.application))
    await update.message.reply_text("🚀 Monitoring started! Updates every ~10 seconds. Will trigger once on first subscriber increase.")
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
            SELL_OUTCOME_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sell_outcome_idx)],
            SELL_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sell_choice)],
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

    print("MrBeast subscriber change sniper bot running. Use /start")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    print("⚠️ REAL TRADING — KEEP DRY_RUN=true UNTIL FULLY TESTED!")
    main()
