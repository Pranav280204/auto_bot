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
from datetime import datetime, timezone, timedelta
import time
from collections import deque

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
import telegram.error

# Load environment variables
load_dotenv()

# Polymarket configs
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
DRY_RUN = os.getenv("DRY_RUN", "False").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")

if not PRIVATE_KEY:
    raise ValueError("PRIVATE_KEY is missing in .env")
if not WALLET_ADDRESS:
    raise ValueError("WALLET_ADDRESS is missing in .env")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing in .env")
if not TWITTER_API_KEY:
    raise ValueError("TWITTER_API_KEY is missing in .env")

# Constants
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYGON_RPC = "https://polygon-rpc.com/"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Minimal ERC-1155 ABI
ERC1155_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Initialize Web3 and ClobClient
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
print("\n⚠️ REQUIRED APPROVALS (do once):")
print("• BUY: Approve USDC to exchange contracts")
print("• SELL: setApprovalForAll on Conditional Tokens for exchange contracts")
print("Exchange addrs: 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E & 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296\n")

# Twitter monitoring configs
TARGET_ACCOUNT = "elonmusk"
CHECK_INTERVAL = 0.5  # Reduced to 0.5s for faster detection
PRINT_FULL_TEXT = True

# Tweet deduplication: store recent tweet IDs
SEEN_TWEET_IDS = deque(maxlen=100)  # Keep last 100 tweet IDs to avoid reprocessing

# Conversation states
SLUG = 0
MARKET_IDX = 1
OUTCOME_IDX = 2
RANGE_CONFIG = 3
BUY_RANGE_1 = 4
SELL_RANGE_1 = 5
USE_RANGE_2 = 6
BUY_RANGE_2 = 7
SELL_RANGE_2 = 8
CONFIRM_CONFIG = 9


def fetch_active_markets(slug):
    """Fetch active markets for a given slug"""
    url = f"{GAMMA_API}/events/slug/{slug}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        event = response.json()
        if not event or "markets" not in event:
            return []
        markets = event.get("markets", [])
        active = [m for m in markets if m.get("active", False) and not m.get("closed", True)]
        return active
    except Exception as e:
        print(f"Fetch markets error: {e}")
        return []


def get_mid_price(token_id):
    """Get mid price from orderbook"""
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
    """Get token balance from blockchain"""
    try:
        contract = w3.eth.contract(
            address=w3.to_checksum_address(CONDITIONAL_TOKENS), 
            abi=ERC1155_ABI
        )
        balance_wei = contract.functions.balanceOf(
            w3.to_checksum_address(WALLET_ADDRESS), 
            int(token_id)
        ).call()
        return balance_wei / 1_000_000
    except Exception as e:
        print(f"Balance fetch error: {e}")
        return 0.0


async def place_market_order_async(token_id, amount, side):
    """Async wrapper for placing market orders - OPTIMIZED FOR SPEED"""
    if DRY_RUN:
        print(f"[DRY RUN] Would place {side} for {amount} on {token_id}")
        return {"status": "dry_run", "success": True}
    
    try:
        # Run synchronous order creation in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        
        # Create order args
        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=side,
            order_type=OrderType.FOK
        )
        
        # Create and post order in thread pool
        signed = await loop.run_in_executor(None, client.create_market_order, args)
        resp = await loop.run_in_executor(None, client.post_order, signed, OrderType.FOK)
        
        print(f"Order response ({side}): {resp}")
        return {"status": "success", "response": resp, "success": True}
    except Exception as e:
        print(f"Order placement failed ({side}): {e}")
        return {"status": "error", "error": str(e), "success": False}


def should_execute_trade(current_price, buy_range, sell_range):
    """
    Determine if trade should execute based on current price and ranges
    Returns: (should_buy, should_sell)
    """
    should_buy = False
    should_sell = False
    
    if buy_range and len(buy_range) == 2:
        buy_min, buy_max = buy_range
        if buy_min <= current_price <= buy_max:
            should_buy = True
    
    if sell_range and len(sell_range) == 2:
        sell_min, sell_max = sell_range
        if sell_min <= current_price <= sell_max:
            should_sell = True
    
    return should_buy, should_sell


async def execute_trades_parallel(token_id, buy_amount, sell_amount, should_buy, should_sell, chat_id, bot):
    """
    Execute buy and sell trades in parallel for maximum speed
    """
    tasks = []
    trade_start = time.time()
    
    if should_buy and buy_amount > 0:
        tasks.append(("BUY", place_market_order_async(token_id, buy_amount, BUY)))
    
    if should_sell and sell_amount > 0:
        tasks.append(("SELL", place_market_order_async(token_id, sell_amount, SELL)))
    
    if not tasks:
        return []
    
    # Execute all trades in parallel
    results = []
    for trade_type, task in tasks:
        result = await task
        results.append((trade_type, result))
    
    trade_end = time.time()
    trade_duration = trade_end - trade_start
    
    # Send quick confirmation
    success_trades = [t for t, r in results if r.get("success")]
    if success_trades:
        msg = f"⚡ {', '.join(success_trades)} executed in {trade_duration:.2f}s"
        await safe_send_message(bot, chat_id, msg)
    
    return results


async def check_for_new_tweets(session, application):
    """
    Optimized tweet checking with deduplication
    """
    # Get current timestamp for this check
    now = datetime.now(timezone.utc)
    
    # Look back 60 seconds to catch tweets reliably
    since_time = now - timedelta(seconds=60)
    until_time = now + timedelta(seconds=5)
    
    since_str = since_time.strftime("%Y-%m-%d_%H:%M:%S_UTC")
    until_str = until_time.strftime("%Y-%m-%d_%H:%M:%S_UTC")
    
    query = f"from:{TARGET_ACCOUNT} since:{since_str} until:{until_str} include:nativeretweets"
    url = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    params = {"query": query, "queryType": "Latest"}
    headers = {"X-API-Key": TWITTER_API_KEY}
    
    all_tweets = []
    next_cursor = None
    detection_start = time.time()
    
    # Fetch all tweets with pagination
    while True:
        if next_cursor:
            params["cursor"] = next_cursor
        
        try:
            async with session.get(url, headers=headers, params=params, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    tweets = data.get("tweets", [])
                    all_tweets.extend(tweets)
                    
                    if data.get("has_next_page", False) and data.get("next_cursor"):
                        next_cursor = data.get("next_cursor")
                    else:
                        break
                else:
                    print(f"Twitter API Error: {response.status}")
                    break
        except Exception as e:
            print(f"Tweet fetch error: {e}")
            break
    
    # Filter out already seen tweets using tweet ID
    new_tweets = []
    for tweet in all_tweets:
        tweet_id = tweet.get('id') or tweet.get('tweetId') or tweet.get('tweet_id')
        if tweet_id and tweet_id not in SEEN_TWEET_IDS:
            new_tweets.append(tweet)
            SEEN_TWEET_IDS.append(tweet_id)
    
    if new_tweets:
        detection_end = time.time()
        print(f"DETECTION: Found {len(new_tweets)} NEW tweets in {detection_end - detection_start:.3f}s")
        
        chat_id = application.bot_data.get('chat_id')
        token_id = application.bot_data.get('token_id')
        
        # Get current price
        current_price = get_mid_price(token_id)
        
        if current_price is None:
            await safe_send_message(application.bot, chat_id, "⚠️ Could not fetch current price!")
            return
        
        # Check both range configurations
        range_1 = application.bot_data.get('range_1', {})
        range_2 = application.bot_data.get('range_2', {})
        
        should_buy_1, should_sell_1 = should_execute_trade(
            current_price,
            range_1.get('buy_range'),
            range_1.get('sell_range')
        )
        
        should_buy_2, should_sell_2 = should_execute_trade(
            current_price,
            range_2.get('buy_range'),
            range_2.get('sell_range')
        )
        
        # Combine decisions (execute if either range triggers)
        should_buy = should_buy_1 or should_buy_2
        should_sell = should_sell_1 or should_sell_2
        
        # Get amounts
        buy_amount_1 = range_1.get('buy_amount', 0)
        buy_amount_2 = range_2.get('buy_amount', 0)
        sell_amount_1 = range_1.get('sell_amount', 0)
        sell_amount_2 = range_2.get('sell_amount', 0)
        
        # Calculate total amounts
        total_buy = 0
        total_sell = 0
        
        if should_buy_1:
            total_buy += buy_amount_1
        if should_buy_2:
            total_buy += buy_amount_2
        if should_sell_1:
            total_sell += sell_amount_1
        if should_sell_2:
            total_sell += sell_amount_2
        
        # Execute trades in parallel
        if should_buy or should_sell:
            await execute_trades_parallel(
                token_id,
                total_buy,
                total_sell,
                should_buy,
                should_sell,
                chat_id,
                application.bot
            )
        else:
            await safe_send_message(
                application.bot,
                chat_id,
                f"📊 Current price {current_price:.4f} - No range match (no trade)"
            )
        
        # Send tweet notification
        message = f"{'═' * 60}\n🆕 {len(new_tweets)} NEW TWEET(S) FROM @{TARGET_ACCOUNT}\n{'═' * 60}\n\n"
        
        for tweet in new_tweets:
            created_str = tweet.get('createdAt', '??')
            try:
                tweet_time = datetime.strptime(created_str, "%a %b %d %H:%M:%S %z %Y")
                latency = (now - tweet_time).total_seconds()
                latency_str = f"{latency:.2f}s ago"
                time_str = tweet_time.strftime('%Y-%m-%d %H:%M:%S UTC')
            except:
                latency_str = "??"
                time_str = "??"
            
            message += f"[{time_str}] ({latency_str})\n"
            
            text = tweet.get('text', '').strip()
            if PRINT_FULL_TEXT:
                message += f"{text}\n"
            else:
                preview = text[:90] + "…" if len(text) > 90 else text
                message += f"{preview}\n"
            
            message += "─" * 60 + "\n"
        
        await safe_send_message(application.bot, chat_id, message)
        
        # Final timing
        total_cycle = time.time() - detection_start
        print(f"TOTAL CYCLE TIME: {total_cycle:.3f}s")


async def safe_send_message(bot, chat_id, text):
    """Send message with retry logic"""
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except telegram.error.TimedOut:
        print("Timeout on send_message - retrying once...")
        await asyncio.sleep(2)
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except:
            print("Failed to send message after retry")
    except Exception as e:
        print(f"Send message error: {e}")


async def monitor_elon_activity(application: Application):
    """Main monitoring loop"""
    print("🔍 Monitoring loop started")
    
    # Use longer timeout for persistent connection
    timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
    
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while application.bot_data.get('monitoring', False):
            try:
                start_time = time.time()
                await check_for_new_tweets(session, application)
                duration = time.time() - start_time
                
                # Dynamic sleep to maintain check interval
                sleep_time = max(0, CHECK_INTERVAL - duration)
                await asyncio.sleep(sleep_time)
            except Exception as e:
                print(f"Monitor loop error: {e}")
                await asyncio.sleep(CHECK_INTERVAL)
    
    print("🛑 Monitoring loop ended")


# ═══════════════════════════════════════════════════════════════
# TELEGRAM CONVERSATION HANDLERS
# ═══════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start configuration"""
    context.user_data.clear()
    await update.message.reply_text(
        "🚀 Welcome to Polymarket Tweet Trading Bot!\n\n"
        "Enter Polymarket event slug:\n"
        "(e.g., elon-musk-of-tweets-january-16-january-23)"
    )
    return SLUG


async def get_slug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get and validate slug"""
    slug = update.message.text.strip()
    context.user_data['slug'] = slug
    
    markets = fetch_active_markets(slug)
    if not markets:
        await update.message.reply_text(
            "❌ No active markets found.\n"
            "Check the slug or try /start again."
        )
        return ConversationHandler.END
    
    context.user_data['markets'] = markets
    
    text = f"✅ Found {len(markets)} active market(s):\n\n"
    for i, m in enumerate(markets):
        text += f"{i}: {m.get('question', 'Unknown')}\n"
    
    await update.message.reply_text(text + "\nSelect market number:")
    return MARKET_IDX


async def get_market_idx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Select market"""
    try:
        idx = int(update.message.text.strip())
        markets = context.user_data['markets']
        
        if idx < 0 or idx >= len(markets):
            raise ValueError
        
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
        
        text = "📊 Outcomes:\n\n"
        for i, outcome in enumerate(outcomes):
            text += f"{i}: {outcome}\n"
        
        await update.message.reply_text(text + "\nSelect outcome index:")
        return OUTCOME_IDX
    except:
        await update.message.reply_text("❌ Invalid number. Try again:")
        return MARKET_IDX


async def get_outcome_idx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Select outcome"""
    try:
        idx = int(update.message.text.strip())
        outcomes = context.user_data['outcomes']
        
        if idx < 0 or idx >= len(outcomes):
            raise ValueError
        
        token_ids = context.user_data['token_ids']
        token_id = token_ids[idx]
        selected = outcomes[idx]
        
        context.user_data['token_id'] = token_id
        context.user_data['selected'] = selected
        
        mid = get_mid_price(token_id)
        price_str = f"{mid:.4f} USDC" if mid else "unavailable"
        
        await update.message.reply_text(
            f"✅ Selected: {selected}\n"
            f"💰 Current mid price: {price_str}\n\n"
            f"How many trading ranges do you want?\n"
            f"1 = Single range\n"
            f"2 = Two ranges\n"
            f"Enter choice:"
        )
        return RANGE_CONFIG
    except:
        await update.message.reply_text("❌ Invalid index. Try again:")
        return OUTCOME_IDX


async def get_range_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Configure number of ranges"""
    choice = update.message.text.strip()
    
    if choice not in ['1', '2']:
        await update.message.reply_text("❌ Enter '1' or '2':")
        return RANGE_CONFIG
    
    context.user_data['num_ranges'] = int(choice)
    
    await update.message.reply_text(
        "📈 RANGE 1 - BUY CONFIGURATION\n\n"
        "Enter buy price range (min,max) or 'skip' to skip buying:\n"
        "Example: 0.45,0.55"
    )
    return BUY_RANGE_1


async def get_buy_range_1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get buy range for range 1"""
    text = update.message.text.strip().lower()
    
    if text == 'skip':
        context.user_data['range_1_buy'] = None
        context.user_data['range_1_buy_amount'] = 0
    else:
        try:
            parts = text.split(',')
            if len(parts) != 2:
                raise ValueError
            
            buy_min = float(parts[0].strip())
            buy_max = float(parts[1].strip())
            
            if buy_min >= buy_max or buy_min < 0 or buy_max > 1:
                raise ValueError
            
            context.user_data['range_1_buy'] = (buy_min, buy_max)
            
            await update.message.reply_text(
                f"✅ Buy range 1: {buy_min:.4f} - {buy_max:.4f}\n\n"
                f"Enter USDC amount to buy when triggered:"
            )
            return BUY_AMOUNT_1
        except:
            await update.message.reply_text(
                "❌ Invalid format. Use 'min,max' or 'skip':"
            )
            return BUY_RANGE_1
    
    await update.message.reply_text(
        "📉 RANGE 1 - SELL CONFIGURATION\n\n"
        "Enter sell price range (min,max) or 'skip' to skip selling:\n"
        "Example: 0.65,0.75"
    )
    return SELL_RANGE_1


async def get_buy_amount_1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get buy amount for range 1"""
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
        
        context.user_data['range_1_buy_amount'] = amount
        
        await update.message.reply_text(
            f"✅ Will buy ${amount:.2f} USDC worth\n\n"
            f"📉 RANGE 1 - SELL CONFIGURATION\n\n"
            f"Enter sell price range (min,max) or 'skip' to skip selling:\n"
            f"Example: 0.65,0.75"
        )
        return SELL_RANGE_1
    except:
        await update.message.reply_text("❌ Invalid amount. Try again:")
        return BUY_AMOUNT_1


# Additional state for buy amount
BUY_AMOUNT_1 = 100
BUY_AMOUNT_2 = 101
SELL_AMOUNT_1 = 102
SELL_AMOUNT_2 = 103


async def get_sell_range_1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get sell range for range 1"""
    text = update.message.text.strip().lower()
    
    if text == 'skip':
        context.user_data['range_1_sell'] = None
        context.user_data['range_1_sell_amount'] = 0
    else:
        try:
            parts = text.split(',')
            if len(parts) != 2:
                raise ValueError
            
            sell_min = float(parts[0].strip())
            sell_max = float(parts[1].strip())
            
            if sell_min >= sell_max or sell_min < 0 or sell_max > 1:
                raise ValueError
            
            context.user_data['range_1_sell'] = (sell_min, sell_max)
            
            await update.message.reply_text(
                f"✅ Sell range 1: {sell_min:.4f} - {sell_max:.4f}\n\n"
                f"Enter number of shares to sell when triggered:"
            )
            return SELL_AMOUNT_1
        except:
            await update.message.reply_text(
                "❌ Invalid format. Use 'min,max' or 'skip':"
            )
            return SELL_RANGE_1
    
    # Check if we need range 2
    if context.user_data.get('num_ranges') == 2:
        await update.message.reply_text(
            "📈 RANGE 2 - BUY CONFIGURATION\n\n"
            "Enter buy price range (min,max) or 'skip':\n"
            "Example: 0.30,0.40"
        )
        return BUY_RANGE_2
    else:
        # Show summary and confirm
        return await show_confirmation(update, context)


async def get_sell_amount_1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get sell amount for range 1"""
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
        
        context.user_data['range_1_sell_amount'] = amount
        
        # Check if we need range 2
        if context.user_data.get('num_ranges') == 2:
            await update.message.reply_text(
                f"✅ Will sell {amount:.2f} shares\n\n"
                f"📈 RANGE 2 - BUY CONFIGURATION\n\n"
                f"Enter buy price range (min,max) or 'skip':\n"
                f"Example: 0.30,0.40"
            )
            return BUY_RANGE_2
        else:
            # Show summary and confirm
            return await show_confirmation(update, context)
    except:
        await update.message.reply_text("❌ Invalid amount. Try again:")
        return SELL_AMOUNT_1


async def get_buy_range_2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get buy range for range 2"""
    text = update.message.text.strip().lower()
    
    if text == 'skip':
        context.user_data['range_2_buy'] = None
        context.user_data['range_2_buy_amount'] = 0
    else:
        try:
            parts = text.split(',')
            if len(parts) != 2:
                raise ValueError
            
            buy_min = float(parts[0].strip())
            buy_max = float(parts[1].strip())
            
            if buy_min >= buy_max or buy_min < 0 or buy_max > 1:
                raise ValueError
            
            context.user_data['range_2_buy'] = (buy_min, buy_max)
            
            await update.message.reply_text(
                f"✅ Buy range 2: {buy_min:.4f} - {buy_max:.4f}\n\n"
                f"Enter USDC amount to buy when triggered:"
            )
            return BUY_AMOUNT_2
        except:
            await update.message.reply_text(
                "❌ Invalid format. Use 'min,max' or 'skip':"
            )
            return BUY_RANGE_2
    
    await update.message.reply_text(
        "📉 RANGE 2 - SELL CONFIGURATION\n\n"
        "Enter sell price range (min,max) or 'skip':\n"
        "Example: 0.80,0.90"
    )
    return SELL_RANGE_2


async def get_buy_amount_2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get buy amount for range 2"""
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
        
        context.user_data['range_2_buy_amount'] = amount
        
        await update.message.reply_text(
            f"✅ Will buy ${amount:.2f} USDC worth\n\n"
            f"📉 RANGE 2 - SELL CONFIGURATION\n\n"
            f"Enter sell price range (min,max) or 'skip':\n"
            f"Example: 0.80,0.90"
        )
        return SELL_RANGE_2
    except:
        await update.message.reply_text("❌ Invalid amount. Try again:")
        return BUY_AMOUNT_2


async def get_sell_range_2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get sell range for range 2"""
    text = update.message.text.strip().lower()
    
    if text == 'skip':
        context.user_data['range_2_sell'] = None
        context.user_data['range_2_sell_amount'] = 0
    else:
        try:
            parts = text.split(',')
            if len(parts) != 2:
                raise ValueError
            
            sell_min = float(parts[0].strip())
            sell_max = float(parts[1].strip())
            
            if sell_min >= sell_max or sell_min < 0 or sell_max > 1:
                raise ValueError
            
            context.user_data['range_2_sell'] = (sell_min, sell_max)
            
            await update.message.reply_text(
                f"✅ Sell range 2: {sell_min:.4f} - {sell_max:.4f}\n\n"
                f"Enter number of shares to sell when triggered:"
            )
            return SELL_AMOUNT_2
        except:
            await update.message.reply_text(
                "❌ Invalid format. Use 'min,max' or 'skip':"
            )
            return SELL_RANGE_2
    
    return await show_confirmation(update, context)


async def get_sell_amount_2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get sell amount for range 2"""
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
        
        context.user_data['range_2_sell_amount'] = amount
        
        return await show_confirmation(update, context)
    except:
        await update.message.reply_text("❌ Invalid amount. Try again:")
        return SELL_AMOUNT_2


async def show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show configuration summary and ask for confirmation"""
    summary = "═" * 50 + "\n"
    summary += "📋 CONFIGURATION SUMMARY\n"
    summary += "═" * 50 + "\n\n"
    
    summary += f"🎯 Outcome: {context.user_data['selected']}\n"
    summary += f"🔢 Token ID: {context.user_data['token_id']}\n\n"
    
    # Range 1
    summary += "📊 RANGE 1:\n"
    if context.user_data.get('range_1_buy'):
        buy_min, buy_max = context.user_data['range_1_buy']
        buy_amt = context.user_data.get('range_1_buy_amount', 0)
        summary += f"  📈 BUY: ${buy_amt:.2f} when price in [{buy_min:.4f}, {buy_max:.4f}]\n"
    else:
        summary += f"  📈 BUY: Disabled\n"
    
    if context.user_data.get('range_1_sell'):
        sell_min, sell_max = context.user_data['range_1_sell']
        sell_amt = context.user_data.get('range_1_sell_amount', 0)
        summary += f"  📉 SELL: {sell_amt:.2f} shares when price in [{sell_min:.4f}, {sell_max:.4f}]\n"
    else:
        summary += f"  📉 SELL: Disabled\n"
    
    # Range 2 if configured
    if context.user_data.get('num_ranges') == 2:
        summary += "\n📊 RANGE 2:\n"
        if context.user_data.get('range_2_buy'):
            buy_min, buy_max = context.user_data['range_2_buy']
            buy_amt = context.user_data.get('range_2_buy_amount', 0)
            summary += f"  📈 BUY: ${buy_amt:.2f} when price in [{buy_min:.4f}, {buy_max:.4f}]\n"
        else:
            summary += f"  📈 BUY: Disabled\n"
        
        if context.user_data.get('range_2_sell'):
            sell_min, sell_max = context.user_data['range_2_sell']
            sell_amt = context.user_data.get('range_2_sell_amount', 0)
            summary += f"  📉 SELL: {sell_amt:.2f} shares when price in [{sell_min:.4f}, {sell_max:.4f}]\n"
        else:
            summary += f"  📉 SELL: Disabled\n"
    
    summary += "\n" + "═" * 50 + "\n"
    summary += "Start monitoring? (y/n)"
    
    await update.message.reply_text(summary)
    return CONFIRM_CONFIG


async def confirm_and_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirm configuration and start monitoring"""
    if 'y' not in update.message.text.lower():
        await update.message.reply_text("❌ Configuration cancelled.")
        return ConversationHandler.END
    
    # Store configuration in bot_data
    context.bot_data['token_id'] = context.user_data['token_id']
    context.bot_data['chat_id'] = update.effective_chat.id
    
    # Build range configurations
    range_1 = {
        'buy_range': context.user_data.get('range_1_buy'),
        'buy_amount': context.user_data.get('range_1_buy_amount', 0),
        'sell_range': context.user_data.get('range_1_sell'),
        'sell_amount': context.user_data.get('range_1_sell_amount', 0)
    }
    
    range_2 = {
        'buy_range': context.user_data.get('range_2_buy'),
        'buy_amount': context.user_data.get('range_2_buy_amount', 0),
        'sell_range': context.user_data.get('range_2_sell'),
        'sell_amount': context.user_data.get('range_2_sell_amount', 0)
    }
    
    context.bot_data['range_1'] = range_1
    context.bot_data['range_2'] = range_2
    context.bot_data['monitoring'] = True
    
    # Clear seen tweets to start fresh
    SEEN_TWEET_IDS.clear()
    
    # Start monitoring task
    context.application.create_task(monitor_elon_activity(context.application))
    
    await update.message.reply_text(
        "✅ Monitoring started!\n\n"
        f"🔍 Watching @{TARGET_ACCOUNT}\n"
        f"⚡ Check interval: {CHECK_INTERVAL}s\n"
        f"📊 Ranges configured: {context.user_data.get('num_ranges')}\n\n"
        "Use /stop to stop monitoring\n"
        "Use /status to check status"
    )
    
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel configuration"""
    await update.message.reply_text("❌ Configuration cancelled.")
    return ConversationHandler.END


async def stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop monitoring"""
    context.bot_data['monitoring'] = False
    await update.message.reply_text("🛑 Monitoring stopped.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show monitoring status"""
    if context.bot_data.get('monitoring', False):
        token_id = context.bot_data.get('token_id', 'N/A')
        current_price = get_mid_price(token_id) if token_id != 'N/A' else None
        
        msg = f"✅ Monitoring is ACTIVE\n\n"
        msg += f"🎯 Token ID: {token_id}\n"
        if current_price:
            msg += f"💰 Current Price: {current_price:.4f}\n"
        msg += f"👀 Watching: @{TARGET_ACCOUNT}\n"
        msg += f"⏱️ Seen tweets: {len(SEEN_TWEET_IDS)}\n"
        
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("⭕ Not monitoring. Use /start to begin.")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    """Main entry point"""
    # Custom request with increased timeouts
    custom_request = HTTPXRequest(
        connection_pool_size=20,
        read_timeout=30,
        write_timeout=30,
        connect_timeout=15,
        pool_timeout=30,
        media_write_timeout=60
    )
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).request(custom_request).build()
    
    # Conversation handler with all states
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SLUG: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_slug)],
            MARKET_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_market_idx)],
            OUTCOME_IDX: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_outcome_idx)],
            RANGE_CONFIG: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_range_config)],
            BUY_RANGE_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_buy_range_1)],
            BUY_AMOUNT_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_buy_amount_1)],
            SELL_RANGE_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sell_range_1)],
            SELL_AMOUNT_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sell_amount_1)],
            BUY_RANGE_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_buy_range_2)],
            BUY_AMOUNT_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_buy_amount_2)],
            SELL_RANGE_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sell_range_2)],
            SELL_AMOUNT_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sell_amount_2)],
            CONFIRM_CONFIG: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_and_start)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("stop", stop_monitor))
    application.add_handler(CommandHandler("status", status))
    
    print("=" * 60)
    print("🤖 TELEGRAM BOT STARTED")
    print("=" * 60)
    print(f"⚠️  DRY RUN MODE: {DRY_RUN}")
    print(f"📊 Target Account: @{TARGET_ACCOUNT}")
    print(f"⏱️  Check Interval: {CHECK_INTERVAL}s")
    print("=" * 60)
    print("\nUse /start in your Telegram chat to begin configuration")
    print("=" * 60)
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    if not DRY_RUN:
        print("\n" + "!" * 60)
        print("⚠️  WARNING: REAL MONEY TRADING ENABLED!")
        print("!" * 60)
        print("Set DRY_RUN=true in .env for testing\n")
    
    main()
