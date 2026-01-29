import os
import sys
import json
import getpass
import requests
from pprint import pprint

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
except Exception as e:
    print("Missing dependencies. Run: pip install py-clob-client requests python-dotenv")
    raise

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon


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
        print(f"Error fetching market: {resp.status_code} {resp.text}")
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


def main():
    print("Polymarket CLI trading bot — minimal example")

    # Read credentials securely
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        print("PRIVATE_KEY not set in environment. You can paste it now (it will not be echoed).")
        private_key = getpass.getpass("Private key: ")

    wallet_address = os.getenv("WALLET_ADDRESS") or input("Your wallet address (0x...): ")

    signature_type_env = os.getenv("SIGNATURE_TYPE")
    signature_type = int(signature_type_env) if signature_type_env else 1
    funder = os.getenv("POLYMARKET_PROXY_ADDRESS") or None

    # Initialize client
    client_args = dict(host=CLOB_HOST, key=private_key, chain_id=CHAIN_ID)
    if signature_type in (1, 2):
        client_args.update({"signature_type": signature_type})
        if signature_type == 1 and funder:
            client_args["funder"] = funder

    client = ClobClient(**client_args)

    # Derive API creds (one-time; safe to run every start)
    print("Deriving API credentials (private key used locally to derive API key)...")
    api_creds = client.create_or_derive_api_creds()
    client.set_api_creds(api_creds)
    print("API credentials derived. API key:", api_creds.get("apiKey"))

    # 1) Get event/market
    slug = input("Enter event market slug (e.g. will-bitcoin-reach-100k-by-2025): ").strip()
    market = fetch_market_by_slug(slug)
    if not market:
        print("Market not found — exiting.")
        sys.exit(1)

    print("Market fetched — some fields:")
    # Print condensed view
    # clobTokenIds usually contains token IDs for outcomes
    clob_token_ids = market.get("clobTokenIds") or market.get("clob_token_ids")
    outcomes = market.get("outcomes") or market.get("labels") or market.get("outcome_labels")

    if outcomes and isinstance(outcomes, list):
        for i, o in enumerate(outcomes):
            # flexible handling: outcome may be string or dict
            if isinstance(o, dict):
                label = o.get("label") or o.get("name") or json.dumps(o)
            else:
                label = str(o)
            token_id = clob_token_ids[i] if clob_token_ids and i < len(clob_token_ids) else "<unknown_token_id>"
            print(f"[{i}] {label} — token_id: {token_id}")
    elif clob_token_ids:
        for i, tid in enumerate(clob_token_ids):
            print(f"[{i}] token_id: {tid}")
    else:
        print("Could not find outcome labels or clobTokenIds in the market response — raw JSON:")
        pprint(market)
        print("Please copy the token id you want to trade from the raw output above and enter it manually.")

    # Choose outcome
    if clob_token_ids:
        idx = choose_from_list("Select outcome index: ", [str(t) for t in clob_token_ids])
        token_id = clob_token_ids[idx]
    else:
        token_id = input("Enter token id to trade: ").strip()

    # Choose buy or sell
    side = input("Enter side (buy/sell): ").strip().lower()
    if side not in ("buy", "sell"):
        print("Invalid side")
        sys.exit(1)
    side_const = BUY if side == "buy" else SELL

    # Enter price and size
    price = float(input("Enter price (0.00 - 1.00): ").strip())
    size = float(input("Enter size (number of shares or dollar amount depending on order type): ").strip())

    # Choose order type
    print("Order types: [0] GTC (Good-Till-Cancelled), [1] FOK (Fill-Or-Kill), [2] FAK")
    ot = input("Select order type index (default 0): ").strip()
    order_type = OrderType.GTC
    if ot == "1":
        order_type = OrderType.FOK
    elif ot == "2":
        order_type = OrderType.FAK

    # Confirm
    print("Summary:")
    print(f"Market slug: {slug}")
    print(f"Token ID: {token_id}")
    print(f"Side: {side}")
    print(f"Price: {price}")
    print(f"Size: {size}")
    print(f"Order type: {order_type}")
    confirm = input("Type YES to confirm and place order: ").strip()
    if confirm != "YES":
        print("Order cancelled by user")
        sys.exit(0)

    # Build and sign order
    order_args = OrderArgs(price=price, size=size, side=side_const, token_id=token_id)
    signed_order = client.create_order(order_args)

    # Post order
    print("Placing order...")
    resp = client.post_order(signed_order, order_type)
    print("Response from Polymarket:")
    pprint(resp)


if __name__ == '__main__':
    main()
