"""
Polymarket CLI trading bot — improved
Includes:
 - Fix for ApiCreds object attribute access
 - Optional Telegram integration (polling-based) using TELEGRAM_BOT_TOKEN

Usage:
  1) Install dependencies:
       pip install py-clob-client requests python-dotenv
  2) Create a .env file with required vars (see below)
  3) Run:
       python polymarket_trading_bot.py

Security:
 - Never commit .env or paste PRIVATE_KEY publicly.
 - Use a dedicated wallet with limited funds for bots.

.env variables (minimum):
 - PRIVATE_KEY
 - WALLET_ADDRESS
Optional for Telegram:
 - TELEGRAM_BOT_TOKEN

Telegram usage:
 - Send /start to your bot to register the chat.
 - /help to see commands
 - /trade <slug> <outcome_index> <buy/sell> <price> <size>  -> queues an order and returns an order_id
 - /confirm <order_id> -> confirms and places the queued order
 - /cancel <order_id> -> cancels queued order
 - /status -> returns a short summary

This script requires you to run it continuously (it polls Telegram updates). Be careful and test with tiny amounts.
"""

import os
import sys
import json
import time
import threading
import logging
import requests
import getpass
from pprint import pprint
from dotenv import load_dotenv

# Third-party client (py_clob_client)
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
except Exception as e:
    print("Missing dependencies. Run: pip install py-clob-client requests python-dotenv")
    raise

# Load .env
load_dotenv()

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# Simple in-memory queue for pending telegram orders
pending_orders = {}
pending_lock = threading.Lock()
next_order_id = 1

# Telegram state
telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
telegram_offset = None
registered_chats = set()

# Logging
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'))
logger = logging.getLogger('polymarket-bot')


def get_env_or_prompt(name, secret=False):
    val = os.getenv(name)
    if val:
        return val
    if secret:
        return getpass.getpass(f"Enter {name}: ")
    return input(f"Enter {name}: ")


def fetch_market_by_slug(slug):
    url = f"{GAMMA_API_BASE}/markets/slug/{slug}"
    resp = requests.get(url)
    if resp.status_code != 200:
        logger.error("Error fetching market: %s %s", resp.status_code, resp.text)
        return None
    return resp.json()


def choose_from_list(prompt, items):
    for i, it in enumerate(items):
        print(f"[{i}] {it}")
    idx = input(prompt)
    try:
        idx = int(idx)
        if 0 <= idx < len(items):
            return idx
    except:
        pass
    print("Invalid selection")
    return None


# Telegram helper functions
def telegram_api(method, params=None):
    global telegram_token
    if not telegram_token:
        return None
    url = f"https://api.telegram.org/bot{telegram_token}/{method}"
    try:
        r = requests.post(url, data=params or {})
        return r.json()
    except Exception as e:
        logger.exception("Telegram API error")
        return None


def send_telegram_message(chat_id, text):
    return telegram_api('sendMessage', {'chat_id': chat_id, 'text': text})


def process_telegram_message(msg, clob_client):
    global next_order_id
    chat = msg.get('message') or msg.get('edited_message')
    if not chat:
        return
    chat_id = chat['chat']['id']
    text = chat.get('text','').strip()
    logger.info('Telegram message from %s: %s', chat_id, text)
    if text.startswith('/'):
        parts = text.split()
        cmd = parts[0].lower()
        if cmd == '/start':
            registered_chats.add(chat_id)
            send_telegram_message(chat_id, 'Registered this chat. Use /help to see commands.')
        elif cmd == '/help':
            send_telegram_message(chat_id, 'Commands:\n/trade <slug> <outcome_index> <buy/sell> <price> <size>\n/confirm <order_id>\n/cancel <order_id>\n/status')
        elif cmd == '/status':
            # minimal status: wallet address and simple markets count
            send_telegram_message(chat_id, f'Bot running. Wallet: {clob_client.address if hasattr(clob_client,"address") else "<unknown>"}')
        elif cmd == '/trade':
            # /trade slug outcome_index buy 0.44 10
            if len(parts) < 6:
                send_telegram_message(chat_id, 'Usage: /trade <slug> <outcome_index> <buy/sell> <price> <size>')
                return
            slug = parts[1]
            try:
                outcome_index = int(parts[2])
                side = parts[3].lower()
                price = float(parts[4])
                size = float(parts[5])
            except Exception:
                send_telegram_message(chat_id, 'Invalid parameters. Ensure outcome_index is integer, price and size are numbers.')
                return
            # Fetch market to validate token id
            market = fetch_market_by_slug(slug)
            if not market:
                send_telegram_message(chat_id, f'Market {slug} not found')
                return
            clob_token_ids = market.get('clobTokenIds') or market.get('clob_token_ids')
            if not clob_token_ids or outcome_index < 0 or outcome_index >= len(clob_token_ids):
                send_telegram_message(chat_id, 'Invalid outcome index for this market')
                return
            token_id = clob_token_ids[outcome_index]
            side_const = BUY if side == 'buy' else SELL
            with pending_lock:
                oid = str(next_order_id)
                next_order_id += 1
                pending_orders[oid] = {
                    'slug': slug,
                    'token_id': token_id,
                    'side': side,
                    'side_const': side_const,
                    'price': price,
                    'size': size,
                    'chat_id': chat_id,
                    'market': market,
                }
            send_telegram_message(chat_id, f'Order queued with id {oid}. Confirm with /confirm {oid} or cancel with /cancel {oid}')
        elif cmd == '/confirm':
            if len(parts) < 2:
                send_telegram_message(chat_id, 'Usage: /confirm <order_id>')
                return
            oid = parts[1]
            with pending_lock:
                order = pending_orders.get(oid)
            if not order:
                send_telegram_message(chat_id, f'No pending order with id {oid}')
                return
            # Only allow the original requester to confirm
            if order['chat_id'] != chat_id:
                send_telegram_message(chat_id, 'You are not the owner of this pending order')
                return
            # Place order
            try:
                order_args = OrderArgs(price=order['price'], size=order['size'], side=order['side_const'], token_id=order['token_id'])
                signed_order = clob_client.create_order(order_args)
                resp = clob_client.post_order(signed_order, OrderType.GTC)
                send_telegram_message(chat_id, f'Order placed. Response: {json.dumps(resp)}')
                with pending_lock:
                    pending_orders.pop(oid, None)
            except Exception as e:
                logger.exception('Error placing order')
                send_telegram_message(chat_id, f'Error placing order: {e}')
        elif cmd == '/cancel':
            if len(parts) < 2:
                send_telegram_message(chat_id, 'Usage: /cancel <order_id>')
                return
            oid = parts[1]
            with pending_lock:
                order = pending_orders.pop(oid, None)
            if order:
                send_telegram_message(chat_id, f'Pending order {oid} cancelled')
            else:
                send_telegram_message(chat_id, f'No pending order {oid}')
        else:
            send_telegram_message(chat_id, 'Unknown command. Use /help')


def telegram_polling_loop(clob_client):
    global telegram_offset
    if not telegram_token:
        logger.info('No TELEGRAM_BOT_TOKEN set — skipping telegram integration')
        return
    logger.info('Starting telegram polling...')
    while True:
        try:
            url = f'https://api.telegram.org/bot{telegram_token}/getUpdates'
            params = {}
            if telegram_offset:
                params['offset'] = telegram_offset
            r = requests.get(url, params=params, timeout=60)
            data = r.json()
            if not data.get('ok'):
                time.sleep(2)
                continue
            for upd in data.get('result', []):
                telegram_offset = upd['update_id'] + 1
                process_telegram_message(upd, clob_client)
        except Exception:
            logger.exception('Telegram polling error')
            time.sleep(5)


def main():
    # Read credentials securely
    private_key = os.getenv('PRIVATE_KEY')
    if not private_key:
        print('PRIVATE_KEY not set in environment. You can paste it now (it will not be echoed).')
        private_key = getpass.getpass('Private key: ')

    wallet_address = os.getenv('WALLET_ADDRESS') or input('Your wallet address (0x...): ')

    signature_type_env = os.getenv('SIGNATURE_TYPE')
    signature_type = int(signature_type_env) if signature_type_env else 1
    funder = os.getenv('POLYMARKET_PROXY_ADDRESS') or None

    # Initialize client
    client_args = dict(host=CLOB_HOST, key=private_key, chain_id=CHAIN_ID)
    if signature_type in (1, 2, 3):
        client_args.update({"signature_type": signature_type})
        if signature_type == 1 and funder:
            client_args["funder"] = funder

    clob_client = ClobClient(**client_args)

    # Derive API creds (one-time; safe to run every start)
    logger.info('Deriving API credentials (private key used locally to derive API key)...')
    api_creds = clob_client.create_or_derive_api_creds()

    # api_creds may be a dict-like, dataclass, or object. Normalize to dict for safe access & logging.
    try:
        if isinstance(api_creds, dict):
            creds = api_creds
        elif hasattr(api_creds, 'to_dict'):
            creds = api_creds.to_dict()
        else:
            # fallback: try __dict__
            creds = getattr(api_creds, '__dict__', {}) or {k: getattr(api_creds,k) for k in dir(api_creds) if not k.startswith('_') and not callable(getattr(api_creds,k))}
    except Exception:
        creds = {}

    # Print whichever key name exists
    api_key_val = creds.get('apiKey') or creds.get('api_key') or creds.get('apiKeyHex') or creds.get('apiKeyHexString') or creds.get('apiKeyHex')
    logger.info('API credentials derived. API key (partial): %s', str(api_key_val)[:8] if api_key_val else '<unknown>')

    # set the creds back on the client if required (py_clob_client may accept object or dict)
    try:
        clob_client.set_api_creds(api_creds)
    except Exception:
        try:
            clob_client.set_api_creds(creds)
        except Exception:
            logger.exception('Failed to set API creds on client — continuing but you may see auth errors')

    # Start telegram polling thread if token present
    if telegram_token:
        t = threading.Thread(target=telegram_polling_loop, args=(clob_client,), daemon=True)
        t.start()

    # Interactive CLI loop (simple)
    print('Polymarket CLI trading bot — interactive mode. Type HELP for commands or use Telegram.')
    print('Commands: get <slug>, trade <slug> <outcome_index> <buy/sell> <price> <size>, exit')

    while True:
        try:
            cmd = input('> ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nExiting')
            break
        if not cmd:
            continue
        parts = cmd.split()
        if parts[0].lower() == 'exit':
            break
        if parts[0].lower() == 'get' and len(parts) >= 2:
            slug = parts[1]
            m = fetch_market_by_slug(slug)
            pprint(m)
            continue
        if parts[0].lower() == 'trade' and len(parts) >= 6:
            _, slug, outcome_idx_s, side, price_s, size_s = parts[:6]
            try:
                outcome_idx = int(outcome_idx_s)
                price = float(price_s)
                size = float(size_s)
            except Exception:
                print('Invalid numeric params')
                continue
            market = fetch_market_by_slug(slug)
            if not market:
                print('Market not found')
                continue
            clob_token_ids = market.get('clobTokenIds') or market.get('clob_token_ids')
            if not clob_token_ids or outcome_idx < 0 or outcome_idx >= len(clob_token_ids):
                print('Invalid outcome index')
                continue
            token_id = clob_token_ids[outcome_idx]
            side_const = BUY if side.lower() == 'buy' else SELL
            print('Summary:')
            print(f'Slug: {slug}\nToken: {token_id}\nSide: {side}\nPrice: {price}\nSize: {size}')
            confirm = input('Type YES to confirm: ').strip()
            if confirm != 'YES':
                print('Cancelled')
                continue
            try:
                order_args = OrderArgs(price=price, size=size, side=side_const, token_id=token_id)
                signed_order = clob_client.create_order(order_args)
                resp = clob_client.post_order(signed_order, OrderType.GTC)
                print('Order response:')
                pprint(resp)
                # Notify registered telegram chats
                for chat in list(registered_chats):
                    send_telegram_message(chat, f'Order placed via CLI. Response: {resp}')
            except Exception as e:
                logger.exception('Error placing order')
                print('Error placing order:', e)
            continue
        print('Unknown command')


if __name__ == '__main__':
    main()
