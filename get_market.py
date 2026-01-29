#!/usr/bin/env python3
"""
polymarket_youtube_trigger_bot.py
- Polymarket Telegram bot with:
  * normal buy/sell
  * trigger-based sell on YouTube subscriber-change (polling ~1.8s)
  * rotates multiple YouTube Data API keys (env YT_KEYS comma-separated)
  * prioritizes sell/buy actions before sending Telegram notifications
"""
import os
import json
import asyncio
import time
import aiohttp
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from web3 import Web3

# Polymarket / CLOB imports (keeps same pattern)
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

# Telegram (async)
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

# -------------------------
# Configuration (env)
# -------------------------
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# YouTube
YT_KEYS = os.getenv("YT_KEYS", "")  # comma-separated list of API keys (user said they have 6)
YT_KEY_LIST = [k.strip() for k in YT_KEYS.split(",") if k.strip()]
YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID", "UCX6OQ3DkcsbYNE6H8uQQuVA")  # default MrBeast
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.8"))  # seconds

# Polymarket / CLOB
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com/")
CONDITIONAL_TOKENS = os.getenv(
    "CONDITIONAL_TOKENS", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
)

# sanity checks
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN required in .env")

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))

# create clob client only if PRIVATE_KEY provided (otherwise dry-run only)
client = None
if PRIVATE_KEY:
    try:
        client = ClobClient(host=CLOB_API, key=PRIVATE_KEY, chain_id=137, signature_type=1, funder=WALLET_ADDRESS)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
    except Exception as e:
        print("Warning: could not fully init ClobClient:", e)
        client = client  # maybe partial

# Minimal ERC-1155 ABI for balanceOf
ERC1155_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

# -------------------------
# Globals for monitor
# -------------------------
LAST_SUB_COUNT = None
YT_KEY_INDEX = 0
MONITOR_TASK = None

# Conversation states
(
    SLUG,
    MARKET_IDX,
    MENU,
    OUTCOME_IDX,
    NORMAL_OUTCOME,
    NORMAL_AMOUNT,
    TRIGGER_SELECT_OUTCOME,
    TRIGGER_SELL_CHOICE,
    TRIGGER_CUSTOM_SELL,
    TRIGGER_BUY_YN,
    TRIGGER_BUY_AMOUNT,
    START_MONITOR,
) = range(12)

# --- Helpers: Polymarket/Gamma ---
def fetch_active_markets(slug):
    try:
        r = requests.get(f"{GAMMA_API}/events/slug/{slug}", timeout=10)
        r.raise_for_status()
        event = r.json()
        markets = event.get("markets", []) or []
        active = [m for m in markets if m.get("active", False) and not m.get("closed", True)]
        return active
    except Exception as e:
        print("fetch_active_markets error:", e)
        return []

def normalize_outcomes_and_token_ids(market):
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
    try:
        contract = w3.eth.contract(address=w3.to_checksum_address(CONDITIONAL_TOKENS), abi=ERC1155_ABI)
        balance_wei = contract.functions.balanceOf(w3.to_checksum_address(WALLET_ADDRESS), int(token_id)).call()
        return balance_wei / 1_000_000  # assumes 6 decimals
    except Exception as e:
        print("get_balance_shares error:", e)
        return 0.0

def place_market_order(token_id, amount, side):
    """
    For BUY: amount = USDC float (dollars)
    For SELL: amount = shares float
    """
    if DRY_RUN or client is None:
        print(f"[DRY RUN] place_market_order token={token_id} side={side} amount={amount}")
        return {"status": "dry_run"}
    try:
        args = MarketOrderArgs(token_id=token_id, amount=amount, side=side, order_type=OrderType.FOK)
        signed = client.create_market_order(args)
        resp = client.post_order(signed, OrderType.FOK)
        print("Order response:", resp)
        return resp
    except Exception as e:
        print("place_market_order error:", e)
        return {"error": str(e)}

# --- Telegram message helper ---
async def safe_send_message(bot, chat_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        print("safe_send_message error:", e)

# -------------------------
# YouTube polling helpers
# -------------------------
async def fetch_subscriber_count(session, channel_id):
    """Rotate through keys for each request. Returns int subscriber count or None."""
    global YT_KEY_INDEX, YT_KEY_LIST
    if not YT_KEY_LIST:
        return None
    # pick key
    key = YT_KEY_LIST[YT_KEY_INDEX % len(YT_KEY_LIST)]
    YT_KEY_INDEX += 1
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {"part": "statistics", "id": channel_id, "key": key}
    try:
        async with session.get(url, params=params, timeout=10) as resp:
            if resp.status != 200:
                # try consuming json to print message
                try:
                    data = await resp.json()
                except:
                    data = {"status": resp.status}
                print("YT API non-200:", resp.status, data)
                return None
            data = await resp.json()
            items = data.get("items", [])
            if not items:
                return None
            stats = items[0].get("statistics", {})
            subs = stats.get("subscriberCount")
            if subs is None:
                return None
            return int(subs)
    except Exception as e:
        print("fetch_subscriber_count error:", e)
        return None

# -------------------------
# Monitor coroutine (runs as a background task)
# -------------------------
async def monitor_subscribers(application, channel_id):
    """
    Polls YouTube every POLL_INTERVAL seconds and:
     - sends count updates every poll to Telegram
     - if count changes from last seen: prioritize executing configured sell/buy actions, then send notification
    """
    global LAST_SUB_COUNT
    if 'chat_id' not in application.bot_data:
        print("No chat_id set; cannot send updates.")
        return

    chat_id = application.bot_data['chat_id']
    # load trigger config from bot_data
    # token_id_sell, sell_pct, token_id_buy, buy_usdc, from_outcome, target_outcome, monitoring flag
    print("YouTube monitor started for channel:", channel_id, "interval:", POLL_INTERVAL)
    async with aiohttp.ClientSession() as session:
        # initial population
        initial = True
        while application.bot_data.get("monitoring_yt", False):
            loop_start = time.time()
            subs = await fetch_subscriber_count(session, channel_id)
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            # Always send update every poll — but if change -> trade first then notify
            # Determine change
            changed = False
            prev = LAST_SUB_COUNT
            if subs is not None:
                if prev is None:
                    changed = False
                else:
                    changed = (subs != prev)
                LAST_SUB_COUNT = subs

            # If changed and trigger configured -> execute trades (SELL then BUY) before sending message
            if changed and application.bot_data.get("trigger_config"):
                cfg = application.bot_data['trigger_config']
                # SELL first
                results = []
                sell_result = None
                sell_desc = "No sell configured"
                if cfg.get("sell_pct", 0) > 0 and cfg.get("token_id_sell"):
                    token_id_sell = cfg["token_id_sell"]
                    # compute shares to sell based on percent of current balance
                    bal = get_balance_shares(token_id_sell)
                    sell_shares = bal * cfg["sell_pct"]
                    if sell_shares > 0:
                        sell_result = place_market_order(token_id_sell, sell_shares, SELL)
                        sell_desc = f"Sold {sell_shares:.6f} shares"
                        results.append(sell_desc)
                    else:
                        results.append("Sell configured but balance is zero")
                # BUY after sell if configured
                buy_result = None
                buy_desc = "No buy configured"
                if cfg.get("buy_usdc", 0) > 0 and cfg.get("token_id_buy"):
                    token_id_buy = cfg["token_id_buy"]
                    buy_amt = cfg["buy_usdc"]
                    buy_result = place_market_order(token_id_buy, buy_amt, BUY)
                    buy_desc = f"Bought ${buy_amt:.2f} USDC"
                    results.append(buy_desc)
                # After trades done, send combined notification
                trade_msg = f"🚨 Subscriber change detected ({prev} → {subs}) at {ts}\n\n"
                trade_msg += "Executed actions (SELL → BUY priority):\n"
                trade_msg += "\n".join(f"• {r}" for r in results) if results else "• No actions configured"
                # send trade message
                await safe_send_message(application.bot, chat_id, trade_msg)

            # Always send subscriber count update (after trades)
            update_msg = f"Subscriber update @ {ts}\nChannel: {channel_id}\nSubscribers: {subs if subs is not None else 'N/A'}"
            await safe_send_message(application.bot, chat_id, update_msg)

            # maintain interval
            elapsed = time.time() - loop_start
            sleep_for = max(0, POLL_INTERVAL - elapsed)
            await asyncio.sleep(sleep_for)
    print("YouTube monitor stopped")

# -------------------------
# Telegram handlers / conversation flow
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter Polymarket event slug:")
    return SLUG

async def get_slug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    slug = update.message.text.strip()
    context.user_data['slug'] = slug
    markets = fetch_active_markets(slug)
    if not markets:
        await update.message.reply_text("No active markets found.")
        return ConversationHandler.END
    context.user_data['markets'] = markets
    text = f"Found {len(markets)} active market(s):\n"
    for i, m in enumerate(markets):
        text += f"{i}: {m.get('question', 'Unknown')}\n"
    text += "\nSelect market number:"
    await update.message.reply_text(text)
    return MARKET_IDX

async def get_market_idx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(update.message.text.strip())
        markets = context.user_data['markets']
        market = markets[idx]
        outcomes, token_ids = normalize_outcomes_and_token_ids(market)
        context.user_data['market'] = market
        context.user_data['outcomes'] = outcomes
        context.user_data['token_ids'] = token_ids
        # After market selected, show menu: normal buy/sell/trigger
        menu = "Choose action:\n1 = Normal BUY\n2 = Normal SELL\n3 = Trigger action (YouTube subscriber change)"
        await update.message.reply_text(menu)
        return MENU
    except Exception:
        await update.message.reply_text("Invalid market index.")
        return MARKET_IDX

async def menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt not in ("1", "2", "3"):
        await update.message.reply_text("Enter 1, 2 or 3")
        return MENU
    context.user_data['menu_choice'] = txt
    if txt in ("1", "2"):  # normal buy/sell
        # ask outcome
        outcomes = context.user_data['outcomes']
        msg = "Select outcome:\n"
        for i, o in enumerate(outcomes):
            msg += f"{i}: {o}\n"
        await update.message.reply_text(msg)
        return NORMAL_OUTCOME
    else:  # trigger action
        # ask outcome to SELL on trigger (0 for yes, 1 for no)
        outcomes = context.user_data['outcomes']
        msg = "Select outcome to SELL on trigger (index):\n"
        for i, o in enumerate(outcomes):
            msg += f"{i}: {o}\n"
        msg += "\n(You will be asked sell % then optional buy on YES afterwards)"
        await update.message.reply_text(msg)
        return TRIGGER_SELECT_OUTCOME

# --- Normal buy/sell flow ---
async def normal_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(update.message.text.strip())
        outcomes = context.user_data['outcomes']
        token_ids = context.user_data['token_ids']
        outcome = outcomes[idx]
        token_id = token_ids[idx]
        context.user_data['normal_outcome_idx'] = idx
        context.user_data['normal_token_id'] = token_id
        context.user_data['normal_outcome_name'] = outcome
        # ask amount
        if context.user_data['menu_choice'] == "1":
            await update.message.reply_text(f"Normal BUY -> {outcome}\nEnter USDC amount to BUY:")
        else:
            bal = get_balance_shares(token_id)
            context.user_data['balance'] = bal
            await update.message.reply_text(f"Normal SELL -> {outcome}\nYour balance: {bal:.6f} shares\nEnter shares to SELL:")
        return NORMAL_AMOUNT
    except:
        await update.message.reply_text("Invalid outcome index.")
        return NORMAL_OUTCOME

async def normal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(update.message.text.strip())
        menu_choice = context.user_data['menu_choice']
        token_id = context.user_data['normal_token_id']
        outcome = context.user_data['normal_outcome_name']
        if menu_choice == "1":  # BUY USDC
            resp = place_market_order(token_id, val, BUY)
            await update.message.reply_text(f"BUY placed (or simulated). Response: {resp}")
        else:  # SELL shares
            bal = context.user_data.get('balance', 0)
            if val > bal:
                await update.message.reply_text(f"Requested sell {val} > balance {bal}. Cancelled.")
                return ConversationHandler.END
            resp = place_market_order(token_id, val, SELL)
            await update.message.reply_text(f"SELL placed (or simulated). Response: {resp}")
        return ConversationHandler.END
    except:
        await update.message.reply_text("Invalid amount.")
        return NORMAL_AMOUNT

# --- Trigger setup flow ---
async def trigger_select_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(update.message.text.strip())
        outcomes = context.user_data['outcomes']
        token_ids = context.user_data['token_ids']
        context.user_data['trigger_outcome_idx'] = idx
        context.user_data['token_id_sell'] = token_ids[idx]
        context.user_data['from_outcome_name'] = outcomes[idx]
        # ask sell % (25,50,100,4=custom)
        bal = get_balance_shares(context.user_data['token_id_sell'])
        context.user_data['balance'] = bal
        await update.message.reply_text(
            f"Selected to SELL on trigger: {context.user_data['from_outcome_name']}\n"
            f"Current balance: {bal:.6f} shares\n\n"
            "Choose sell percent on trigger:\n1 = 25%\n2 = 50%\n3 = 100%\n4 = Custom shares\nEnter choice:"
        )
        return TRIGGER_SELL_CHOICE
    except:
        await update.message.reply_text("Invalid index.")
        return TRIGGER_SELECT_OUTCOME

async def trigger_sell_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ch = update.message.text.strip()
    if ch not in ("1", "2", "3", "4"):
        await update.message.reply_text("Enter 1/2/3/4")
        return TRIGGER_SELL_CHOICE
    bal = context.user_data['balance']
    if ch == "4":
        await update.message.reply_text("Enter custom number of shares to SELL on trigger:")
        return TRIGGER_CUSTOM_SELL
    percs = {"1": 0.25, "2": 0.5, "3": 1.0}
    sell_pct = percs[ch]
    context.user_data['sell_pct'] = sell_pct
    context.user_data['sell_shares'] = bal * sell_pct
    await update.message.reply_text(
        f"Will SELL {context.user_data['sell_shares']:.6f} shares ({sell_pct*100:.0f}%) on trigger.\n"
        "Also BUY YES after selling? (y/n)"
    )
    return TRIGGER_BUY_YN

async def trigger_custom_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        shares = float(update.message.text.strip())
        bal = context.user_data['balance']
        if shares <= 0 or shares > bal:
            await update.message.reply_text("Invalid custom share amount. Must be >0 and <= balance.")
            return TRIGGER_CUSTOM_SELL
        context.user_data['sell_shares'] = shares
        context.user_data['sell_pct'] = shares / bal if bal > 0 else 0
        await update.message.reply_text(f"Will SELL {shares:.6f} shares on trigger.\nAlso BUY YES after selling? (y/n)")
        return TRIGGER_BUY_YN
    except:
        await update.message.reply_text("Invalid number.")
        return TRIGGER_CUSTOM_SELL

async def trigger_buy_yn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if 'y' in txt:
        # ask buy USDC amount for YES
        outcomes = context.user_data['outcomes']
        # determine YES outcome index (we'll assume 0 = yes, but let user choose where to buy)
        # Ask which outcome to BUY (usually yes)
        await update.message.reply_text("Enter USDC amount to BUY on target outcome (provide outcome index or type 'same' to buy on same outcome):\n" +
                                        "\n".join(f"{i}: {o}" for i,o in enumerate(outcomes)))
        return TRIGGER_BUY_AMOUNT
    else:
        # finalize config and start monitoring
        await finalize_and_start_monitor(update, context)
        return START_MONITOR

async def trigger_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    # allow "same" or an index and an amount? User asked earlier to "tell how much quantity to buy" — here ask amount in USDC only
    # We'll accept input like "1 10" -> outcome_idx amount OR "same 10" OR "10" (buy on outcome 0 default)
    parts = txt.split()
    target_idx = None
    amount = None
    try:
        if len(parts) == 1:
            # maybe just number -> default target = 0
            amount = float(parts[0])
            target_idx = 0
        elif len(parts) == 2:
            # idx amount
            if parts[0].lower() == 'same':
                target_idx = context.user_data['trigger_outcome_idx']
            else:
                target_idx = int(parts[0])
            amount = float(parts[1])
        else:
            raise ValueError
    except Exception:
        await update.message.reply_text("Invalid format. Examples:\n'10' (buy $10 on outcome 0)\n'1 10' (buy $10 on outcome index 1)\n'same 10'")
        return TRIGGER_BUY_AMOUNT

    token_ids = context.user_data['token_ids']
    if target_idx < 0 or target_idx >= len(token_ids):
        await update.message.reply_text("Invalid target outcome index.")
        return TRIGGER_BUY_AMOUNT
    context.user_data['buy_usdc'] = amount
    context.user_data['token_id_buy'] = token_ids[target_idx]
    context.user_data['target_outcome_name'] = context.user_data['outcomes'][target_idx]
    await finalize_and_start_monitor(update, context)
    return START_MONITOR

async def finalize_and_start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Prepare trigger_config in bot_data and start background monitor
    token_id_sell = context.user_data.get('token_id_sell')
    sell_pct = context.user_data.get('sell_pct', 0)
    sell_shares = context.user_data.get('sell_shares', 0)
    token_id_buy = context.user_data.get('token_id_buy')
    buy_usdc = context.user_data.get('buy_usdc', 0)
    # Save config
    context.application.bot_data['trigger_config'] = {
        "token_id_sell": token_id_sell,
        "sell_pct": sell_pct,
        "sell_shares": sell_shares,
        "token_id_buy": token_id_buy,
        "buy_usdc": buy_usdc,
        "from_outcome": context.user_data.get('from_outcome_name'),
        "target_outcome": context.user_data.get('target_outcome_name', context.user_data.get('from_outcome_name')),
    }
    context.application.bot_data['chat_id'] = update.effective_chat.id
    context.application.bot_data['monitoring_yt'] = True
    # start background monitor task
    global MONITOR_TASK
    if MONITOR_TASK is None or MONITOR_TASK.done():
        MONITOR_TASK = context.application.create_task(monitor_subscribers(context.application, YT_CHANNEL_ID))
    await update.message.reply_text("Trigger configured and monitoring started. Use /stop_yt to stop monitoring.")
    return

async def stop_yt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data['monitoring_yt'] = False
    await update.message.reply_text("YouTube monitoring stopping...")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    monitoring = context.application.bot_data.get('monitoring_yt', False)
    await update.message.reply_text(f"DRY_RUN={DRY_RUN}\nYT monitoring: {monitoring}")

# -------------------------
# Build conversation handler mapping and main()
# -------------------------
def build_conv_handler():
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SLUG: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_slug)],
            MARKET_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_market_idx)],
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_choice)],
            NORMAL_OUTCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, normal_outcome)],
            NORMAL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, normal_amount)],
            TRIGGER_SELECT_OUTCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, trigger_select_outcome)],
            TRIGGER_SELL_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, trigger_sell_choice)],
            TRIGGER_CUSTOM_SELL: [MessageHandler(filters.TEXT & ~filters.COMMAND, trigger_custom_sell)],
            TRIGGER_BUY_YN: [MessageHandler(filters.TEXT & ~filters.COMMAND, trigger_buy_yn)],
            TRIGGER_BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, trigger_buy_amount)],
        },
        fallbacks=[CommandHandler("stop_yt", stop_yt), CommandHandler("cancel", lambda u,c: (c.bot.send_message(chat_id=u.effective_chat.id, text="Cancelled."), ConversationHandler.END)[1])],
        allow_reentry=True,
    )
    return conv

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(build_conv_handler())
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop_yt", stop_yt))
    print("Bot running. Use /start. DRY_RUN=", DRY_RUN)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
