import os
import re
import requests
import telebot
import hashlib
from ecdsa import SigningKey, SECP256k1

# Polymarket trading imports
if os.environ.get("AUTO_TRADE", "false").lower() == "true":
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType

# Environment variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_TOKEN = os.environ.get("API_TOKEN")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")  # Revealed magic key, with or without 0x
WALLET_ADDRESS = os.environ.get("WALLET_ADDRESS")  # Optional, auto-derived if missing
MARKET_SLUG = os.environ.get("MARKET_SLUG", "what-will-mrbeast-say-during-his-next-youtube-video")
AUTO_TRADE = os.environ.get("AUTO_TRADE", "false").lower() == "true"
TRADE_AMOUNT = float(os.environ.get("TRADE_AMOUNT", "20"))  # USD per opportunity

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not set!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

# Derive wallet address from private key if not provided
def derive_address(private_key: str) -> str:
    pk = private_key[2:] if private_key.startswith("0x") else private_key
    priv_key_bytes = bytes.fromhex(pk)
    sk = SigningKey.from_string(priv_key_bytes, curve=SECP256k1)
    vk = sk.verifying_key
    uncompressed_pub = b'\x04' + vk.to_string()
    keccak = hashlib.sha3_256(uncompressed_pub).digest()
    return "0x" + keccak[-20:].hex()

if PRIVATE_KEY and not WALLET_ADDRESS:
    WALLET_ADDRESS = derive_address(PRIVATE_KEY)

# Video ID extraction
def extract_video_id(user_input):
    patterns = [
        r'(?:v=|\/embed\/|\/shorts\/|\/watch\?v=|youtu\.be\/)([0-9A-Za-z_-]{11})',
        r'^([0-9A-Za-z_-]{11})$'
    ]
    for pattern in patterns:
        match = re.search(pattern, user_input)
        if match:
            return match.group(1)
    return None

# Clean transcript extraction
def extract_transcript_text(data):
    text_parts = []
    def collect(obj):
        if isinstance(obj, str):
            text_parts.append(obj)
        elif isinstance(obj, dict):
            if "text" in obj and isinstance(obj["text"], str):
                text_parts.append(obj["text"])
            else:
                for v in obj.values():
                    collect(v)
        elif isinstance(obj, list):
            for item in obj:
                collect(item)
    collect(data)
    return " ".join(text_parts)

# Word groups (exact match for current Feb 2026 markets)
word_groups = {
    "Dollar": r"\bdollar(s)?\b",
    "Thousand/Million": r"\b(thousand|million|billion)(s)?\b",
    "Challenge": r"\bchallenge(s)?\b",
    "Eliminated": r"\beliminated?\b",
    "Trap": r"\btrap(s)?\b",
    "Car/Supercar": r"\b(car|supercar)(s)?\b",
    "Tesla/Lamborghini": r"\b(tesla|lamborghini)(s)?\b",
    "Helicopter/Jet": r"\b(helicopter|jet)(s)?\b",
    "Island": r"\bisland(s)?\b",
    "Mystery Box": r"\bmystery\s+box(es)?\b",
    "Massive": r"\bmassive\b",
    "World's Biggest/Largest": r"\bworld'?s?\s+(biggest|largest)\b",
    "Beast Games": r"\bbeast\s+games\b",
    "Feastables": r"\bfeastables\b",
    "MrBeast": r"\bmr\.?\s*beast\b",
    "Insane": r"\binsane\b",
    "Subscribe": r"\bsubscrib(e|ed|ing|er|s)?\b"
}

# Keywords to match market questions (case-insensitive)
polymarket_keywords = {
    "Dollar": "dollar",
    "Thousand/Million": "thousand|million|billion",
    "Challenge": "challenge",
    "Eliminated": "eliminated",
    "Trap": "trap",
    "Car/Supercar": "car|supercar",
    "Tesla/Lamborghini": "tesla|lamborghini",
    "Helicopter/Jet": "helicopter|jet",
    "Island": "island",
    "Mystery Box": "mystery box",
    "Massive": "massive",
    "World's Biggest/Largest": "world.?s (biggest|largest)",
    "Beast Games": "beast games",
    "Feastables": "feastables",
    "MrBeast": "mrbeast|mr\.?beast",
    "Insane": "insane",
    "Subscribe": "subscribe"
}

# Fixed thresholds (as requested — no dynamic parsing)
thresholds = {
    "Dollar": 10,
    "Thousand/Million": 10,
    **{cat: 1 for cat in word_groups if cat not in ["Dollar", "Thousand/Million"]}
}

# Fetch Polymarket data using slug (fixes "no active market" error)
def get_polymarket_data():
    if not MARKET_SLUG:
        print("MARKET_SLUG not set")
        return None, None

    try:
        url = f"https://gamma-api.polymarket.com/events/slug/{MARKET_SLUG}"
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()

        # API sometimes wraps in {"event": {...}}, handle both
        event = data.get("event", data)
        markets = event.get("markets", [])

        prices = {}
        token_ids = {}

        for market in markets:
            if not market.get("active"):
                continue

            question_lower = market["question"].lower()
            matched_cat = None

            for cat, keyword in polymarket_keywords.items():
                if re.search(keyword, question_lower):
                    matched_cat = cat
                    break

            if matched_cat:
                outcome_prices = market.get("outcomePrices") or market.get("outcome_prices", [])
                if outcome_prices and len(outcome_prices) >= 2:
                    yes_price = float(outcome_prices[0])  # Yes is always first
                    prices[matched_cat] = yes_price

                # Prefer tokens array, fallback to clobTokenIds
                yes_token = None
                for token in market.get("tokens", []):
                    if token.get("outcome", "").lower() == "yes":
                        yes_token = token["token_id"]
                        break
                if yes_token:
                    token_ids[matched_cat] = yes_token
                elif "clobTokenIds" in market and market["clobTokenIds"]:
                    token_ids[matched_cat] = market["clobTokenIds"][0]  # Yes first

        return prices, token_ids

    except Exception as e:
        print(f"Polymarket fetch error: {e}")
        return None, None

def format_results(text_lower):
    counts = {cat: len(re.findall(pattern, text_lower)) for cat, pattern in word_groups.items()}
    sorted_counts = dict(sorted(counts.items()))
    total = sum(sorted_counts.values())

    # Word count table
    msg = "<pre>"
    msg += f"{'Category':<30} {'Count':>8}\n"
    msg += "-" * 40 + "\n"
    for category, count in sorted_counts.items():
        msg += f"{category:<30} {count:>8}\n"
    msg += "-" * 40 + "\n"
    msg += f"{'TOTAL':<30} {total:>8}\n"
    msg += "</pre>"

    # Polymarket section
    prices, token_ids = get_polymarket_data()
    poly_section = ""
    opportunities = []
    trade_section = ""

    if prices:
        poly_section += "\n<b>📈 Polymarket MrBeast Markets (Live Yes Prices)</b>\n<pre>"
        poly_section += f"{'Category':<30} {'Count':>6} {'≥Thresh':>9} {'Yes ¢':>8} {'Status':>20}\n"
        poly_section += "-" * 80 + "\n"

        for cat, count in sorted_counts.items():
            thresh = thresholds.get(cat, 1)
            yes_p = prices.get(cat)
            status = ""
            if count >= thresh and yes_p is not None and yes_p < 0.95:
                edge = (1.0 - yes_p) / yes_p * 100
                status = f"SNIPABLE (~{edge:.0f}% edge)"
                opportunities.append((cat, token_ids.get(cat), yes_p))

            yes_str = f"{yes_p:.2f}" if yes_p is not None else "N/A"
            poly_section += f"{cat:<30} {count:>6} {f'≥{thresh}':>9} {yes_str:>8} {status:>20}\n"

        poly_section += "-" * 80 + "\n"
        if opportunities:
            poly_section += f"\n<b>🚨 {len(opportunities)} OPPORTUNITIES DETECTED!</b>"
        else:
            poly_section += "\nNo strong edges right now."
        poly_section += "</pre>"
    else:
        poly_section += "\n<i>⚠️ No active market found / API issue / wrong slug. Set MARKET_SLUG env var correctly.</i>"

    # Auto-trading (market orders for fast fill)
    if AUTO_TRADE and PRIVATE_KEY and opportunities and prices:
        try:
            pk = PRIVATE_KEY[2:] if PRIVATE_KEY.startswith("0x") else PRIVATE_KEY
            client = ClobClient(host="https://clob.polymarket.com", key=pk, chain_id=137)
            creds = client.create_or_derive_api_creds()
            client.api_key = creds["api_key"]
            client.api_secret = creds["api_secret"]
            client.api_passphrase = creds["api_passphrase"]

            address = client.get_address() or WALLET_ADDRESS or "unknown"
            trade_section += f"\n<b>🤖 Auto-trading from {address[:8]}... (${TRADE_AMOUNT} per opp)</b>"

            for cat, token_id, yes_p in opportunities:
                if not token_id:
                    trade_section += f"\n⚠️ {cat}: No token_id"
                    continue

                mo = MarketOrderArgs(
                    token_id=token_id,
                    amount=TRADE_AMOUNT,
                    side="BUY",
                    order_type=OrderType.FOK
                )
                try:
                    signed = client.create_market_order(mo)
                    resp = client.post_order(signed, OrderType.FOK)
                    if resp.get("status") == "SUCCESS" or resp.get("order_id"):
                        trade_section += f"\n✅ Bought {cat} Yes (~${TRADE_AMOUNT})"
                    else:
                        trade_section += f"\n⚠️ {cat}: {resp.get('message', 'Failed')}"
                except Exception as e:
                    trade_section += f"\n❌ {cat}: {str(e)[:80]}"
        except Exception as e:
            trade_section += f"\n❌ Trading setup failed: {str(e)[:150]}"

    return f"<b>MrBeast Word Count + Sniper 🚀</b>\n\n{msg}{poly_section}{trade_section}"

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "<b>MrBeast Word Counter + Polymarket Sniper Bot! 👋</b>\n\n"
        "Send YouTube URL/ID, transcript text, or .txt file.\n\n"
        "Features:\n"
        "• Auto-transcript + buzzword counts\n"
        "• Live Polymarket odds for current MrBeast event\n"
        "• Fixed thresholds: Dollar & Thousand/Million ≥10, others ≥1\n"
        f"• Current slug: {MARKET_SLUG}\n"
        f"• Auto-snipe Yes shares (${TRADE_AMOUNT} each if AUTO_TRADE=true)\n\n"
        f"Wallet: {WALLET_ADDRESS or 'Not configured'}"
    )
    bot.reply_to(message, welcome_text, parse_mode='HTML')

@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_text = message.text.strip()
    if not user_text:
        return

    video_id = extract_video_id(user_text)

    if video_id and API_TOKEN:
        bot.reply_to(message, "🔄 Fetching transcript...")
        try:
            url = "https://www.youtube-transcript.io/api/transcripts"
            headers = {"Authorization": f"Basic {API_TOKEN}", "Content-Type": "application/json"}
            payload = {"ids": [video_id]}
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            raw_text = extract_transcript_text(response.json())
            if not raw_text.strip():
                bot.reply_to(message, "No transcript found. Paste manually.")
                return
        except Exception as e:
            bot.reply_to(message, f"❌ Fetch error: {str(e)[:200]}")
            return
    else:
        raw_text = user_text

    result_msg = format_results(raw_text.lower())
    bot.send_message(message.chat.id, result_msg, parse_mode='HTML')

@bot.message_handler(content_types=['document'])
def handle_document(message):
    doc = message.document
    if not (doc.mime_type == 'text/plain' or doc.file_name.lower().endswith('.txt')):
        bot.reply_to(message, "Send a plain .txt file only.")
        return

    bot.reply_to(message, "📄 Processing file...")
    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        transcript = downloaded.decode('utf-8', errors='replace')
        result_msg = format_results(transcript.lower())
        bot.send_message(message.chat.id, result_msg, parse_mode='HTML')
    except Exception as e:
        bot.reply_to(message, f"❌ File error: {str(e)}")

print("Bot running...")
bot.infinity_polling()
