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
PRIVATE_KEY = os.getenv("PRIVATE_KEY")  # This MUST be the EXPORTED private key from Polymarket (Cash > ... > Export Private Key)
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")  # Your Polymarket wallet address (the proxy address shown in settings)
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

# IMPORTANT: For Magic/email login wallets → signature_type=1 + funder required
# If you get "invalid signature", try signature_type=2 (some newer proxy wallets use this)
client = ClobClient(
    host=CLOB_API,
    key=PRIVATE_KEY,
    chain_id=137,
    signature_type=1,  # 1 for Magic/POLY_PROXY wallets (most email logins)
    funder=WALLET_ADDRESS
)

try:
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    print("API credentials derived successfully")
except Exception as e:
    print(f"Failed to derive API creds: {e}")
    print("Possible causes: Wrong PRIVATE_KEY (must be exported from Polymarket), wrong signature_type, or wallet not supported.")
    exit(1)

# Check allowances (should be unlimited for Magic/proxy wallets)
try:
    allowances = client.allowances()
    print(f"USDC allowance: {allowances.get('USDC', 'N/A')}")
except Exception as e:
    print(f"Allowance check failed: {e}")

print(f"Connected to Polymarket | Wallet: {WALLET_ADDRESS[:6]}...{WALLET_ADDRESS[-4:]}")
print(f"Dry run: {DRY_RUN}")
print("If orders fail with 'invalid signature':")
print("   • Double-check you exported the correct private key from Polymarket (Cash section > ... > Export Private Key)")
print("   • Try changing signature_type=2 in the code")
print("   • Ensure PRIVATE_KEY starts with '0x' and is 66 characters long")

# Rest of your functions (fetch_active_markets, get_mid_price, get_balance, safe_send_message, etc.)
# ... (unchanged from your original code)

def place_market_order(token_id, amount, side):
    if DRY_RUN:
        print(f"[DRY RUN] Would place {side} market order: {amount} on token {token_id}")
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
        print("Order successful:", resp)
        return resp
    except Exception as e:
        error_msg = str(e)
        print(f"Order failed: {error_msg}")
        return {"error": error_msg}

# Monitor function with better error reporting
async def monitor_mrbeast_subs(application: Application):
    # ... (same as before until trigger)
    if not triggered and current_subs > last_subs:
        # ... 
        results = []
        # SELL
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
                    err = sell_result.get("error", "Unknown") if isinstance(sell_result, dict) else "Unknown"
                    results.append(f"❌ SELL failed: {err} (took {sell_dur:.3f}s)")
            else:
                results.append("⚠️ No shares to sell.")
        # BUY
        if buy_usdc > 0 and token_id_buy:
            buy_start = time.time()
            buy_result = place_market_order(token_id_buy, buy_usdc, BUY)
            buy_dur = time.time() - buy_start
            if buy_result and "error" not in buy_result:
                results.append(f"✅ BOUGHT ${buy_usdc:.2f} of {target_outcome} (took {buy_dur:.3f}s)")
            else:
                err = buy_result.get("error", "Unknown") if isinstance(buy_result, dict) else "Unknown"
                results.append(f"❌ BUY failed: {err} (took {buy_dur:.3f}s)")

        # ... (send message with results)

# Normal buy/sell with better feedback (same as my previous response)
# ... (use the versions that check "error" in result and show ✅/❌)

# Keep all handlers the same as your original (or my previous improved version)

if __name__ == "__main__":
    print("⚠️ MAGIC/EMAIL WALLET MODE — Ensure PRIVATE_KEY is correctly exported!")
    main()
