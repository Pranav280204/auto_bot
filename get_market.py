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

# FIXED: Removed signature_type=1 and funder (assuming standard EOA wallet)
client = ClobClient(
    host=CLOB_API,
    key=PRIVATE_KEY,
    chain_id=137
)

try:
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    print("API credentials set successfully")
except Exception as e:
    print(f"API creds error: {e}")
    exit(1)

# Check allowances (important for real trades)
try:
    allowances = client.allowances()
    print(f"Current USDC allowance: {allowances.get('USDC', 0)}")
    if float(allowances.get('USDC', 0)) < 1000:
        print("⚠️ USDC allowance is low — approve more USDC via Polymarket UI before trading!")
except Exception as e:
    print(f"Failed to fetch allowances: {e}")

print(f"Connected to Polymarket with wallet: {WALLET_ADDRESS[:6]}...{WALLET_ADDRESS[-4:]}")
print(f"Dry run mode: {DRY_RUN}")

# Conversation states
SLUG, MARKET_IDX, ACTION_TYPE, NORMAL_BUY_OUTCOME, NORMAL_BUY_AMOUNT, NORMAL_SELL_OUTCOME, NORMAL_SELL_CHOICE, TRIGGER_SELL_OUTCOME, TRIGGER_SELL_CHOICE, AUTO_BUY_YN, AUTO_BUY_AMOUNT, START_MONITOR = range(12)

# ... (fetch_active_markets, get_mid_price, get_balance remain unchanged)

def place_market_order(token_id, amount, side):
    if DRY_RUN:
        print(f"[DRY RUN] Would place {side} market order for {amount} on token {token_id}")
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
        print("Order placed successfully:", resp)
        return resp
    except Exception as e:
        error_msg = str(e)
        print(f"Order placement failed: {error_msg}")
        return {"error": error_msg}

# ... (safe_send_message, get_subscriber_count, monitor_mrbeast_subs remain mostly unchanged, but updated results handling below)

async def monitor_mrbeast_subs(application: Application):
    # ... (unchanged until trigger block)
    while application.bot_data.get('monitoring', False):
        # ... (fetch logic unchanged)
        if not triggered and current_subs > last_subs:
            triggered = True
            # ... (timing setup)
            results = []
            token_id_sell = application.bot_data.get('token_id_sell')
            sell_perc = application.bot_data.get('sell_perc', 0)
            from_outcome = application.bot_data.get('from_outcome')

            # PRIORITY 1: SELL first
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
                        err = sell_result.get("error", "Unknown error") if sell_result else "Unknown error"
                        results.append(f"❌ SELL failed: {err} (took {sell_dur:.3f}s)")
                else:
                    results.append("⚠️ No shares to sell.")

            # PRIORITY 2: Then BUY opposite
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
                    err = buy_result.get("error", "Unknown error") if buy_result else "Unknown error"
                    results.append(f"❌ BUY failed: {err} (took {buy_dur:.3f}s)")

            # ... (rest of trigger message unchanged)

# Telegram handlers (only changed normal buy/sell for proper success/failure reporting)

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
            msg = f"[DRY RUN] Would BUY ${amount:.2f} on {outcome} (≈ {est_shares} shares)"
        elif result and "error" not in result:
            msg = f"✅ Normal BUY executed: ${amount:.2f} on {outcome}\nEstimated ≈ {est_shares} shares"
        else:
            err = result.get("error", "Unknown error") if result else "Failed to place order"
            msg = f"❌ BUY failed: {err}\nCheck console logs for details."

        await update.message.reply_text(msg)
        return ConversationHandler.END
    except ValueError:
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
    balance = get_balance(token_id)  # refresh
    sell_amount = balance * perc
    mid = get_mid_price(token_id)
    est_usd = sell_amount * mid if mid else "N/A"
    result = place_market_order(token_id, sell_amount, SELL)

    if DRY_RUN:
        msg = f"[DRY RUN] Would SELL {sell_amount:.4f} shares ({perc*100:.0f}%) of {outcome} (≈ ${est_usd})"
    elif result and "error" not in result:
        msg = f"✅ Normal SELL executed: {sell_amount:.4f} shares ({perc*100:.0f}%) of {outcome}\n≈ ${est_usd}"
    else:
        err = result.get("error", "Unknown error") if result else "Failed to place order"
        msg = f"❌ SELL failed: {err}\nCheck console logs for details."

    await update.message.reply_text(msg)
    return ConversationHandler.END

# ... (rest of the handlers and main() unchanged)

if __name__ == "__main__":
    print("⚠️ REAL TRADING — KEEP DRY_RUN=true UNTIL FULLY TESTED!")
    print("If you get 'invalid signature' again, ensure your py_clob_client is up to date and USDC allowance is set.")
    main()
