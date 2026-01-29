#!/usr/bin/env python3
"""
polymarket_telegram_bot.py

Telegram bot to select a Polymarket event -> market -> outcome -> buy/sell -> confirm -> place market order.

Requirements:
- python 3.10+
- python-dotenv
- requests
- web3
- py-clob-client (your package)
- python-telegram-bot >=20 (async)
- aiohttp

Install (example):
pip install python-dotenv requests web3 python-telegram-bot aiohttp

Usage:
- Create a .env file with PRIVATE_KEY, WALLET_ADDRESS, TELEGRAM_BOT_TOKEN and optionally DRY_RUN=true.
- Run: python polymarket_telegram_bot.py
"""
import os
import json
import requests
from dotenv import load_dotenv
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
import asyncio
from datetime import datetime, timezone

# Telegram imports (async)
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

load_dotenv()

# --- Config from env ---
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"  # default true for safety

if not all([WALLET_ADDRESS, TELEGRAM_BOT_TOKEN]):
    raise ValueError("Set WALLET_ADDRESS and TELEGRAM_BOT_TOKEN in environment (PRIVATE_KEY optional for dry run).")

# APIs
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com/")
CONDITIONAL_TOKENS = os.getenv("CONDITIONAL_TOKENS", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

# Web3 + clob client (if PRIVATE_KEY not provided, client creation is skipped and only dry-run allowed)
w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
client = None
if PRIVATE_KEY:
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
        print("ClobClient credentials set.")
    except Exception as e:
        print("Warning: failed to set client creds:", e)

# Minimal ERC1155 ABI for balanceOf
ERC1155_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Conversation states
SLUG, MARKET_IDX, OUTCOME_IDX, ACTION, AMOUNT, CONFIRM = range(6)

# Helper functions
def fetch_active_markets(slug: str):
    """Fetch event by slug from Gamma and return active markets list"""
    try:
        r = requests.get(f"{GAMMA_API}/events/slug/{slug}", timeout=10)
        r.raise_for_status()
        event = r.json()
        markets = event.get("markets", []) or []
        # pick markets that are active and not closed
        active = [m for m in markets if m.get("active", False) and not m.get("closed", True)]
        return active
    except Exception as e:
        print("fetch_active_markets error:", e)
        return []

def normalize_outcomes_and_token_ids(market):
    """Some responses encode outcomes or token ids as JSON strings; handle both."""
    outcomes = market.get("outcomes", [])
    token_ids = market.get("clobTokenIds", [])
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except:
            outcomes = []
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except:
            token_ids = []
    return outcomes, token_ids

def get_mid_price(token_id):
    """Approx mid from order book. Returns float or None."""
    global client
    if not client:
        return None
    try:
        book = client.get_order_book(token_id)
        bids = [float(p) for p, _ in book.get("bids", [])] if book.get("bids") else []
        asks = [float(p) for p, _ in book.get("asks", [])] if book.get("asks") else []
        if bids and asks:
            return (max(bids) + min(asks)) / 2.0
        if bids:
            return max(bids)
        if asks:
            return min(asks)
        return None
    except Exception as e:
        print("get_mid_price error:", e)
        return None

def get_balance_shares(token_id):
    """Return balance in shares (assumes token decimals = 6)."""
    try:
        contract = w3.eth.contract(address=w3.to_checksum_address(CONDITIONAL_TOKENS), abi=ERC1155_ABI)
        balance_wei = contract.functions.balanceOf(w3.to_checksum_address(WALLET_ADDRESS), int(token_id)).call()
        return balance_wei / 1_000_000  # assuming 6 decimals
    except Exception as e:
        print("get_balance_shares error:", e)
        return 0.0

def place_market_order(token_id, amount, side):
    """
    Place market order using ClobClient.
    - For BUY: amount is USDC value (float)
    - For SELL: amount is shares to sell (float)
    This mirrors your existing pattern. In dry-run mode we only print.
    """
    if DRY_RUN or client is None:
        print(f"[DRY RUN] place_market_order: token={token_id} side={side} amount={amount}")
        return {"status": "dry_run"}
    try:
        args = MarketOrderArgs(token_id=token_id, amount=amount, side=side, order_type=OrderType.FOK)
        signed = client.create_market_order(args)
        resp = client.post_order(signed, OrderType.FOK)
        print("Order response:", resp)
        return resp
    except Exception as e:
        print("place_market_order error:", e)
        return None

# --- Telegram handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter Polymarket event slug (example: 'is-elon-mars-possible'):")
    return SLUG

async def got_slug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    slug = update.message.text.strip()
    context.user_data['slug'] = slug
    markets = fetch_active_markets(slug)
    if not markets:
        await update.message.reply_text("No active markets found for that slug. Try another slug or check spelling.")
        return ConversationHandler.END
    # store markets
    context.user_data['markets'] = markets
    text = f"Found {len(markets)} active market(s). Choose market number:\n\n"
    for i, m in enumerate(markets):
        q = m.get("question") or m.get("name") or "Unknown question"
        text += f"{i}: {q}\n"
    await update.message.reply_text(text)
    return MARKET_IDX

async def got_market_idx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(update.message.text.strip())
        markets = context.user_data.get('markets', [])
        market = markets[idx]
        outcomes, token_ids = normalize_outcomes_and_token_ids(market)
        if not outcomes or not token_ids:
            await update.message.reply_text("Selected market missing outcomes or token ids.")
            return ConversationHandler.END
        context.user_data['market'] = market
        context.user_data['outcomes'] = outcomes
        context.user_data['token_ids'] = token_ids

        # show outcomes
        text = "Select an outcome number:\n"
        for i, o in enumerate(outcomes):
            text += f"{i}: {o}\n"
        await update.message.reply_text(text)
        return OUTCOME_IDX
    except Exception:
        await update.message.reply_text("Invalid market index.")
        return MARKET_IDX

async def got_outcome_idx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(update.message.text.strip())
        outcomes = context.user_data['outcomes']
        token_ids = context.user_data['token_ids']
        outcome = outcomes[idx]
        token_id = token_ids[idx]
        context.user_data['outcome'] = outcome
        context.user_data['token_id'] = token_id

        # show mid price and ask action
        mid = get_mid_price(token_id)
        mid_str = f"{mid:.6f}" if mid else "N/A"
        await update.message.reply_text(f"Selected outcome: {outcome}\nMid price: {mid_str}\n\nDo you want to Buy or Sell? (b/s)")
        return ACTION
    except Exception:
        await update.message.reply_text("Invalid outcome index.")
        return OUTCOME_IDX

async def got_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = update.message.text.strip().lower()
    if action not in ('b', 's'):
        await update.message.reply_text("Please type 'b' to Buy or 's' to Sell.")
        return ACTION
    context.user_data['action'] = action
    # BUY: ask USDC amount. SELL: ask shares to sell (will check balance)
    if action == 'b':
        await update.message.reply_text("Enter USDC amount to BUY (example: 10.0):")
    else:
        # check balance
        token_id = context.user_data['token_id']
        bal = get_balance_shares(token_id)
        context.user_data['balance'] = bal
        await update.message.reply_text(f"Your balance on this outcome: {bal:.6f} shares\nEnter number of shares to SELL (e.g. 0.5):")
    return AMOUNT

async def got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        val = float(txt)
        if val <= 0:
            raise ValueError
        action = context.user_data['action']
        token_id = context.user_data['token_id']
        outcome = context.user_data['outcome']

        if action == 's':
            # validate shares <= balance
            bal = context.user_data.get('balance', 0)
            if val > bal:
                await update.message.reply_text(f"Requested sell shares ({val}) exceed balance ({bal}). Enter a smaller amount.")
                return AMOUNT
            # estimate USDC from mid price
            mid = get_mid_price(token_id)
            est_usdc = val * mid if mid else None
            est_str = f"≈ ${est_usdc:.2f}" if est_usdc else "N/A"
            context.user_data['amount'] = val  # shares
            await update.message.reply_text(f"SELL {val:.6f} shares of {outcome} {est_str}\nConfirm? (y/n)")
            return CONFIRM
        else:
            # BUY: val is USDC to spend
            mid = get_mid_price(token_id)
            est_shares = val / mid if mid and mid > 0 else None
            est_str = f"≈ {est_shares:.6f} shares" if est_shares else "N/A"
            context.user_data['amount'] = val  # USDC
            await update.message.reply_text(f"BUY ${val:.2f} USDC of {outcome} {est_str}\nConfirm? (y/n)")
            return CONFIRM
    except Exception:
        await update.message.reply_text("Invalid numeric amount. Try again.")
        return AMOUNT

async def got_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if 'y' not in txt:
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    action = context.user_data['action']
    token_id = context.user_data['token_id']
    outcome = context.user_data['outcome']
    amt = context.user_data['amount']

    if action == 'b':
        # amt is USDC value to buy
        resp = place_market_order(token_id, amt, BUY)
        await update.message.reply_text(f"BUY order placed (or simulated). Response: {resp}")
    else:
        # amt is shares to sell
        resp = place_market_order(token_id, amt, SELL)
        await update.message.reply_text(f"SELL order placed (or simulated). Response: {resp}")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Flow cancelled.")
    return ConversationHandler.END

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"DRY_RUN={DRY_RUN}. Clob client available: {client is not None}")

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SLUG: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_slug)],
            MARKET_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_market_idx)],
            OUTCOME_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_outcome_idx)],
            ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_action)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_amount)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("status", status))

    print("Bot started. Use /start in Telegram. DRY_RUN =", DRY_RUN)
    app.run_polling()

if __name__ == "__main__":
    main()
