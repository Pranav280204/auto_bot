import os
import json
import time
import asyncio
import aiohttp
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
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

# ---------- Config ----------
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DRY_RUN = os.getenv("DRY_RUN", "True").lower() == "true"
GAMMA_API = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
CLOB_API = os.getenv("CLOB_API", "https://clob.polymarket.com")
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com/")
CONDITIONAL_TOKENS = os.getenv("CONDITIONAL_TOKENS", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

# YouTube monitoring
YT_API_KEYS = [k.strip() for k in os.getenv("YOUTUBE_API_KEYS", "").split(",") if k.strip()]
YT_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "UCX6OQ3DkcsbYNE6H8uQQuVA")
POLL_INTERVAL = float(os.getenv("YT_POLL_INTERVAL", "1"))
TELEGRAM_HEARTBEAT = float(os.getenv("YT_HEARTBEAT", "10"))

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in environment")

# ---------- Web3 + ClobClient ----------
w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
client = None
if PRIVATE_KEY:
    try:
        client = ClobClient(host=CLOB_API, key=PRIVATE_KEY, chain_id=137, signature_type=1, funder=WALLET_ADDRESS)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
    except Exception as e:
        print("Warning: failed to create/set ClobClient creds:", e)

ERC1155_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

# ---------- Conversation states ----------
SLUG, MARKET_IDX, OUTCOME_IDX, MODE_CHOICE, ACTION, AMOUNT, CONFIRM, SELL_CHOICE, BUY_AFTER_YN, BUY_AMOUNT = range(10)

# ---------- Helpers ----------
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
    try:
        if not client:
            return None
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
        return balance_wei / 1_000_000
    except Exception as e:
        print("get_balance_shares error:", e)
        return 0.0

def place_market_order(token_id, amount, side):
    if DRY_RUN or client is None:
        print(f"[DRY RUN] place_market_order token={token_id} side={side} amount={amount}")
        return {"status": "dry_run"}
    try:
        args = MarketOrderArgs(token_id=token_id, amount=amount, side=side, order_type=OrderType.FOK)
        signed = client.create_market_order(args)
        resp = client.post_order(signed, OrderType.FOK)
        print("Order placed:", resp)
        return resp
    except Exception as e:
        print("place_market_order error:", e)
        return None

# ---------- YouTube helper ----------
class YTKeyRotator:
    def __init__(self, keys):
        self.keys = keys or []
        self.idx = 0

    def get_key(self):
        if not self.keys:
            return None
        k = self.keys[self.idx % len(self.keys)]
        self.idx += 1
        return k

async def fetch_channel_subs(session: aiohttp.ClientSession, api_key: str, channel_id: str):
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {"part": "statistics", "id": channel_id, "key": api_key}
    try:
        async with session.get(url, params=params, timeout=10) as resp:
            if resp.status != 200:
                text = await resp.text()
                return {"error": f"HTTP {resp.status}: {text}"}
            data = await resp.json()
            items = data.get("items", [])
            if not items:
                return {"error": "no items"}
            stats = items[0].get("statistics", {})
            subs = int(stats.get("subscriberCount", 0))
            return {"subs": subs}
    except Exception as e:
        return {"error": str(e)}

# ---------- Monitoring coroutine ----------
async def monitor_youtube_and_trigger(application: Application):
    print("YouTube monitor: started")
    rotator = YTKeyRotator(application.bot_data.get("yt_api_keys", []))
    channel_id = application.bot_data.get("yt_channel_id")
    chat_id = application.bot_data.get("chat_id")
    last_subs = None
    last_telegram_time = 0.0
    trade_log = []

    async with aiohttp.ClientSession() as session:
        # Initial fetch
        for _ in range(3):
            key = rotator.get_key()
            if not key:
                break
            res = await fetch_channel_subs(session, key, channel_id)
            if "subs" in res:
                last_subs = res["subs"]
                break
        if last_subs is None:
            last_subs = 0

        while application.bot_data.get("yt_monitoring", False):
            loop_start = time.time()
            now = datetime.now(timezone.utc)
            key = rotator.get_key()
            if not key:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            res = await fetch_channel_subs(session, key, channel_id)
            subs = last_subs
            changed = False

            if "subs" in res:
                subs = res["subs"]
                if subs != last_subs:
                    changed = True
                    token_sell = application.bot_data.get("token_id_sell")
                    sell_shares = application.bot_data.get("sell_shares", 0)
                    token_buy = application.bot_data.get("token_id_buy")
                    buy_usdc = application.bot_data.get("buy_usdc", 0)

                    if sell_shares > 0 and token_sell:
                        t0 = time.time()
                        resp = place_market_order(token_sell, sell_shares, SELL)
                        took = time.time() - t0
                        trade_log.append(f"SELL {sell_shares:.6f} shares (took {took:.3f}s) | resp={resp}")

                    if buy_usdc > 0 and token_buy:
                        t0 = time.time()
                        resp = place_market_order(token_buy, buy_usdc, BUY)
                        took = time.time() - t0
                        trade_log.append(f"BUY ${buy_usdc:.2f} YES (took {took:.3f}s) | resp={resp}")

                    last_subs = subs

            # Telegram heartbeat
            now_ts = time.time()
            if now_ts - last_telegram_time >= TELEGRAM_HEARTBEAT:
                msg_lines = [
                    f"📊 MrBeast Subscriber Monitor",
                    f"Time: {now.strftime('%H:%M:%S')} UTC",
                    f"Subscribers: {subs:,}",
                ]
                if trade_log:
                    msg_lines.append("\n🚨 TRADES EXECUTED SINCE LAST UPDATE")
                    msg_lines.extend(trade_log)
                    trade_log.clear()
                else:
                    if changed:
                        msg_lines.append("\n✅ Change detected but no trades configured.")
                    else:
                        msg_lines.append("\nNo change detected.")

                msg_text = "\n".join(msg_lines)
                try:
                    await application.bot.send_message(chat_id=chat_id, text=msg_text)
                except Exception as e:
                    print("Telegram send error:", e)

                last_telegram_time = now_ts

            elapsed = time.time() - loop_start
            await asyncio.sleep(max(0, POLL_INTERVAL - elapsed))

    print("YouTube monitor: stopped")

# ---------- Telegram Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter Polymarket event slug:")
    return SLUG

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop monitoring and end conversation"""
    if context.application.bot_data.get("yt_monitoring", False):
        context.application.bot_data["yt_monitoring"] = False
        await update.message.reply_text("⏹️ YouTube monitoring stopped.")
    else:
        await update.message.reply_text("✅ No active monitoring to stop.")
    return ConversationHandler.END

async def got_slug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    slug = update.message.text.strip()
    markets = fetch_active_markets(slug)
    if not markets:
        await update.message.reply_text("No active markets found.")
        return ConversationHandler.END

    context.user_data['slug'] = slug
    context.user_data['markets'] = markets

    text = f"Found {len(markets)} active market(s):\n"
    for i, m in enumerate(markets):
        text += f"{i}: {m.get('question', 'Unknown')}\n"
    text += "\nSelect market number:"
    await update.message.reply_text(text)
    return MARKET_IDX

async def got_market_idx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(update.message.text.strip())
        markets = context.user_data['markets']
        market = markets[idx]

        outcomes, token_ids = normalize_outcomes_and_token_ids(market)
        if not outcomes or not token_ids:
            await update.message.reply_text("Market missing outcomes or token ids.")
            return ConversationHandler.END

        context.user_data['market'] = market
        context.user_data['outcomes'] = outcomes
        context.user_data['token_ids'] = token_ids

        await update.message.reply_text(
            "Choose action:\n1 = Normal Buy\n2 = Normal Sell\n3 = Trigger action (YouTube subscriber change)"
        )
        return MODE_CHOICE
    except Exception:
        await update.message.reply_text("Invalid market index.")
        return MARKET_IDX

async def got_mode_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ch = update.message.text.strip()
    if ch not in ("1", "2", "3"):
        await update.message.reply_text("Choose 1, 2 or 3.")
        return MODE_CHOICE

    context.user_data['mode'] = ch

    outcomes = context.user_data['outcomes']
    text = "Select outcome number:\n"
    for i, o in enumerate(outcomes):
        text += f"{i}: {o}\n"
    await update.message.reply_text(text)
    return OUTCOME_IDX

async def got_outcome_idx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(update.message.text.strip())
        outcomes = context.user_data['outcomes']
        token_ids = context.user_data['token_ids']
        outcome = outcomes[idx]
        token_id = token_ids[idx]

        context.user_data['outcome'] = outcome
        context.user_data['token_id'] = token_id

        mid = get_mid_price(token_id)
        mid_str = f"{mid:.6f}" if mid else "N/A"
        mode = context.user_data['mode']

        if mode == "1":  # Normal Buy
            await update.message.reply_text(f"Normal BUY selected. Outcome: {outcome} | Mid: {mid_str}\nEnter USDC amount to BUY:")
            context.user_data['normal_action'] = 'buy'
            return AMOUNT
        elif mode == "2":  # Normal Sell
            bal = get_balance_shares(token_id)
            context.user_data['balance'] = bal
            await update.message.reply_text(f"Normal SELL selected. Outcome: {outcome} | Your balance: {bal:.6f} shares\nEnter shares to SELL (or type 25/50/100 for percent):")
            context.user_data['normal_action'] = 'sell'
            return AMOUNT
        else:  # Trigger mode
            await update.message.reply_text("Trigger action selected.\nDo you want to SELL on subscriber change? (y/n)")
            return ACTION
    except Exception:
        await update.message.reply_text("Invalid outcome index.")
        return OUTCOME_IDX

async def got_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text.startswith('y'):
        token_id = context.user_data['token_id']
        bal = get_balance_shares(token_id)
        context.user_data['balance'] = bal
        await update.message.reply_text(
            f"You have {bal:.6f} shares on this outcome.\nChoose percent to SELL on trigger:\n1=25%\n2=50%\n3=100%\n4=custom (enter shares)"
        )
        return SELL_CHOICE
    else:
        await update.message.reply_text("Do you want to BUY YES on trigger when subscriber count changes? (y/n)")
        return BUY_AFTER_YN

async def got_sell_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ch = update.message.text.strip()
    bal = context.user_data.get('balance', 0.0)

    if ch in ('1', '2', '3'):
        per = {'1': 0.25, '2': 0.5, '3': 1.0}[ch]
        sell_shares = bal * per
        context.user_data['sell_shares'] = sell_shares
        await update.message.reply_text(f"Will SELL {sell_shares:.6f} shares on trigger.\nAfter selling, do you want to BUY YES on trigger? (y/n)")
        return BUY_AFTER_YN
    elif ch == '4':
        await update.message.reply_text("Enter CUSTOM shares to sell on trigger (numeric):")
        return CONFIRM
    else:
        await update.message.reply_text("Invalid choice.")
        return SELL_CHOICE

async def got_confirm_custom_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(update.message.text.strip())
        bal = context.user_data.get('balance', 0.0)
        if val <= 0 or val > bal:
            await update.message.reply_text("Invalid shares (must be >0 and <= balance). Enter again:")
            return CONFIRM
        context.user_data['sell_shares'] = val
        await update.message.reply_text(f"Will SELL {val:.6f} shares on trigger.\nAfter selling, do you want to BUY YES on trigger? (y/n)")
        return BUY_AFTER_YN
    except Exception:
        await update.message.reply_text("Invalid numeric input. Enter shares as a number:")
        return CONFIRM

async def got_buy_after_yn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if txt.startswith('y'):
        await update.message.reply_text("Enter USDC amount to BUY on trigger (for YES outcome):")
        return BUY_AMOUNT
    else:
        await update.message.reply_text("Trigger configured. Starting monitoring...")
        return await start_trigger_monitoring(update, context)

async def got_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(update.message.text.strip())
        if val <= 0:
            raise ValueError
        context.user_data['buy_usdc'] = val
        await update.message.reply_text(f"Will BUY ${val:.2f} USDC of YES after selling on trigger.\nStarting monitoring...")
        return await start_trigger_monitoring(update, context)
    except Exception:
        await update.message.reply_text("Invalid amount. Enter numeric USDC value:")
        return BUY_AMOUNT

async def got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount_str = update.message.text.strip()
        normal_action = context.user_data.get('normal_action')

        token_id = context.user_data['token_id']

        if normal_action == 'buy':
            usdc_amount = float(amount_str)
            await update.message.reply_text(f"Placing BUY order for ${usdc_amount:.2f}...")
            resp = place_market_order(token_id, usdc_amount, BUY)
        else:  # sell
            if amount_str in ['25', '50', '100']:
                percent = int(amount_str) / 100.0
                bal = context.user_data.get('balance', 0.0)
                shares = bal * percent
            else:
                shares = float(amount_str)
            await update.message.reply_text(f"Placing SELL order for {shares:.6f} shares...")
            resp = place_market_order(token_id, shares, SELL)

        await update.message.reply_text(f"Order response: {resp}")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"Invalid amount: {e}\nPlease enter a valid number.")
        return AMOUNT

async def start_trigger_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    user = context.user_data

    app.bot_data['token_id_sell'] = user.get('token_id')
    app.bot_data['sell_shares'] = user.get('sell_shares', 0.0)

    # Determine buy token (opposite outcome if possible)
    token_ids = user.get('token_ids', [])
    token_buy = None
    if len(token_ids) >= 2:
        sel_tok = user.get('token_id')
        for t in token_ids:
            if t != sel_tok:
                token_buy = t
                break
    if token_buy is None:
        token_buy = user.get('token_id')

    app.bot_data['token_id_buy'] = token_buy
    app.bot_data['buy_usdc'] = user.get('buy_usdc', 0.0)

    app.bot_data['chat_id'] = update.effective_chat.id
    app.bot_data['yt_monitoring'] = True
    app.bot_data['yt_api_keys'] = YT_API_KEYS
    app.bot_data['yt_channel_id'] = YT_CHANNEL_ID

    app.create_task(monitor_youtube_and_trigger(app))

    await update.message.reply_text("🚀 Monitoring started. Will act when subscriber count changes.\nUse /stop to stop monitoring.")
    return ConversationHandler.END

# ---------- Main ----------
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SLUG: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_slug)],
            MARKET_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_market_idx)],
            OUTCOME_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_outcome_idx)],
            MODE_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_mode_choice)],
            ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_action)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_amount)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_confirm_custom_sell)],
            SELL_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_sell_choice)],
            BUY_AFTER_YN: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_buy_after_yn)],
            BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_buy_amount)],
        },
        fallbacks=[
            CommandHandler("stop", stop),
            CommandHandler("cancel", stop),
        ],
        allow_reentry=True,
    )

    # Global stop command (works even outside conversation)
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(conv_handler)

    print("Bot started successfully!")
    print("Commands: /start - Begin setup | /stop - Stop monitoring")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
