import os
import json
import requests
from dotenv import load_dotenv
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.constants import BUY, SELL
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
    signature_type=0,  # FIXED: 0 for direct EOA private key (was 1, causing invalid signature)
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
print("IMPORTANT: Before trading, ensure USDC & conditional token approvals are set for the exchange contracts!")
print("Run the allowance script once (provided in previous messages) or you will get revert/allowance errors.")

# Conversation states
SLUG, MARKET_IDX, ACTION_TYPE, NORMAL_BUY_OUTCOME, NORMAL_BUY_AMOUNT, NORMAL_SELL_OUTCOME, NORMAL_SELL_CHOICE, TRIGGER_SELL_OUTCOME, TRIGGER_SELL_CHOICE, AUTO_BUY_YN, AUTO_BUY_AMOUNT, START_MONITOR = range(12)

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
        bid_prices = [float(b.price) for b in book.bids] if book.bids else []
        ask_prices = [float(a.price) for a in book.asks] if book.asks else []
        if bid_prices and ask_prices:
            return (max(bid_prices) + min(ask_prices)) / 2
        elif bid_prices:
            return max(bid_prices)
        elif ask_prices:
            return min(ask_prices)
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
        if isinstance(resp, dict) and resp.get("error"):
            raise Exception(resp["error"])
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

            update_msg = f"Current: {current_subs:,} subscribers"
            if last_subs is not None:
                delta = current_subs - last_subs
                update_msg += f"\nChange: {delta:+,} subs" if delta != 0 else "\nNo change yet"
            else:
                update_msg += "\n(Initial count set - waiting for increase)"

            await safe_send_message(application.bot, chat_id, update_msg)

            if not initial_set:
                last_subs = current_subs
                initial_set = True
                await safe_send_message(application.bot, chat_id, f"Baseline set: {current_subs:,} subs\nMonitoring for increases...")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

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
                        if sell_result and "error" not in sell_result:
                            results.append(f"✅ SOLD {sell_amount:.4f} shares ({sell_perc*100:.0f}%) of {from_outcome} (took {sell_dur:.3f}s)")
                        else:
                            err = sell_result.get("error", "Unknown error") if sell_result else "No response"
                            results.append(f"❌ SELL FAILED: {err} (took {sell_dur:.3f}s)")
                    else:
                        results.append("⚠️ No shares to sell.")

                # PRIORITY 2: Then BUY opposite
                buy_dur = 0.0
                buy_usdc = application.bot_data.get('buy_usdc', 0)
                token_id_buy = application.bot_data.get('token_id_buy')
                target_outcome = application.bot_data.get('target_outcome')

                if buy_usdc > 0 and token_id_buy:
                    buy_start = time.time()
                    buy_result = place_market_order(token_id_buy, buy_usdc, BUY)
                    buy_dur = time.time() - buy_start
                    if buy_result and "error" not in buy_result:
                        results.append(f"✅ BOUGHT ${buy_usdc:.2f} of {target_outcome} (took {buy_dur:.3f}s)")
                    else:
                        err = buy_result.get("error", "Unknown error") if buy_result else "No response"
                        results.append(f"❌ BUY FAILED: {err} (took {buy_dur:.3f}s)")

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

# Telegram handlers (unchanged until normal buy/sell)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("MrBeast subscriber sniper bot\n\n1) Enter Polymarket event slug:")
    return SLUG

# ... (all handlers up to ACTION_TYPE are unchanged)

async def normal_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
        token_id = context.user_data['normal_token_id']
        outcome = context.user_data['normal_outcome']
        mid = get_mid_price(token_id)
        est_shares = amount / mid if mid and mid > 0 else "N/A"
        result = place_market_order(token_id, amount, BUY)

        if DRY_RUN:
            msg = f"[DRY RUN] Would BUY ${amount:.2f} on {outcome}\nEstimated ≈ {est_shares} shares"
        elif result and "error" not in result:
            msg = f"✅ Normal BUY executed: ${amount:.2f} on {outcome}\nEstimated ≈ {est_shares} shares"
        else:
            err = result.get("error", "Unknown error") if result else "No response"
            msg = f"❌ BUY FAILED: {err}\nCheck console for full error details."
        await update.message.reply_text(msg)
        return ConversationHandler.END
    except:
        await update.message.reply_text("Invalid amount.")
        return NORMAL_BUY_AMOUNT

async def normal_sell_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ch = update.message.text.strip()
    if ch not in ['1', '2', '3']:
        await update.message.reply_text("Please choose 1, 2, or 3.")
        return NORMAL_SELL_CHOICE

    percs = {'1': 0.25, '2': 0.50, '3': 1.00}
    perc = percs[ch]
    token_id = context.user_data['normal_token_id']
    outcome = context.user_data['normal_outcome']
    balance = get_balance(token_id)
    sell_amount = balance * perc
    mid = get_mid_price(token_id)
    est_usd = f"{sell_amount * mid:.2f}" if mid else "N/A"

    result = place_market_order(token_id, sell_amount, SELL)

    if DRY_RUN:
        msg = f"[DRY RUN] Would SELL {sell_amount:.4f} shares ({perc*100:.0f}%) of {outcome}\n≈ ${est_usd}"
    elif result and "error" not in result:
        msg = f"✅ Normal SELL executed: {sell_amount:.4f} shares ({perc*100:.0f}%) of {outcome}\n≈ ${est_usd}"
    else:
        err = result.get("error", "Unknown error") if result else "No response"
        msg = f"❌ SELL FAILED: {err}\nCheck console for full error details."
    await update.message.reply_text(msg)
    return ConversationHandler.END

# Trigger flow handlers remain the same (they use the improved place_market_order and will show errors properly)

# ... (rest of handlers unchanged: get_trigger_*, build_confirm_message, start_monitor, cancel, stop_monitor, status, main)

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
            ACTION_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_action_type)],
            NORMAL_BUY_OUTCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, normal_buy_outcome)],
            NORMAL_BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, normal_buy_amount)],
            NORMAL_SELL_OUTCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, normal_sell_outcome)],
            NORMAL_SELL_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, normal_sell_choice)],
            TRIGGER_SELL_OUTCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trigger_sell_outcome)],
            TRIGGER_SELL_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trigger_sell_choice)],
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
