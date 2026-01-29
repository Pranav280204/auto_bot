import os
import json
import requests
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

# === CONFIG ===
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
DRY_RUN = os.getenv("DRY_RUN", "True").lower() == "true"

if not PRIVATE_KEY or not WALLET_ADDRESS:
    print("ERROR: Set PRIVATE_KEY and WALLET_ADDRESS in .env")
    exit(1)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYGON_RPC = "https://polygon-rpc.com/"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# === DEBUG INFO (no mismatch check) ===
print("=== DEBUG INFO ===")
print(f"PRIVATE_KEY (truncated): {PRIVATE_KEY[:10]}...{PRIVATE_KEY[-6:]}")
print(f"WALLET_ADDRESS: {WALLET_ADDRESS}")

try:
    acct = Account.from_key(PRIVATE_KEY)
    derived = acct.address.lower()
    print(f"Derived address from key: {derived}")
except Exception as e:
    print(f"Invalid PRIVATE_KEY: {e}")
    exit(1)

# === Init client ===
client = ClobClient(
    host=CLOB_API,
    key=PRIVATE_KEY,
    chain_id=137,
    signature_type=1,  # Magic Link / POLY_PROXY
    funder=WALLET_ADDRESS
)

try:
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    print("API credentials derived and set successfully!")
except Exception as e:
    print(f"Creds derivation failed: {e}")
    exit(1)

print(f"Dry run mode: {DRY_RUN}\n")

# === Helper functions ===
def fetch_active_markets(slug):
    url = f"{GAMMA_API}/events/slug/{slug}"
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()
        markets = data.get("markets", [])
        return [m for m in markets if m.get("active", False) and not m.get("closed", True)]
    except Exception as e:
        print(f"Error fetching markets: {e}")
        return []

def get_mid_price(token_id):
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
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    abi = [{"inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}]
    contract = w3.eth.contract(address=w3.to_checksum_address(CONDITIONAL_TOKENS), abi=abi)
    try:
        bal = contract.functions.balanceOf(w3.to_checksum_address(WALLET_ADDRESS), int(token_id)).call()
        return bal / 1_000_000
    except Exception as e:
        print(f"Balance error: {e}")
        return 0.0

def place_market_order(token_id, amount, side):
    if DRY_RUN:
        print(f"[DRY RUN] Would {side} {amount} on token {token_id}")
        return
    try:
        args = MarketOrderArgs(token_id=token_id, amount=amount, side=side, order_type=OrderType.FOK)
        signed = client.create_market_order(args)
        resp = client.post_order(signed, OrderType.FOK)
        print("Order placed successfully!")
        print("Response:", resp)
    except Exception as e:
        print(f"ORDER PLACEMENT FAILED: {e}")
        import traceback
        traceback.print_exc()

# === Main flow ===
print("=== Polymarket Simple Buy/Sell Test ===\n")

slug = input("1) Enter event slug: ").strip()
markets = fetch_active_markets(slug)

if not markets:
    print("No active markets found for this slug.")
    exit(1)

print(f"\nFound {len(markets)} active market(s):")
for i, m in enumerate(markets):
    print(f"{i}: {m.get('question', 'Unknown')}")

market_idx = int(input("\n2) Select market number: "))
market = markets[market_idx]

outcomes = market.get("outcomes", [])
if isinstance(outcomes, str):
    outcomes = json.loads(outcomes)
token_ids = market.get("clobTokenIds", [])
if isinstance(token_ids, str):
    token_ids = json.loads(token_ids)

print("\nOutcomes:")
for i, (outcome, tid) in enumerate(zip(outcomes, token_ids)):
    mid = get_mid_price(tid) or "N/A"
    bal = get_balance(tid)
    print(f"{i}: {outcome} | Mid price: {mid:.4f if isinstance(mid, float) else mid} | Your balance: {bal:.4f} shares")

outcome_idx = int(input("\n3) Select outcome number: "))
token_id = token_ids[outcome_idx]
outcome_name = outcomes[outcome_idx]
mid = get_mid_price(token_id) or 0.5

print(f"\nSelected: {outcome_name} (token_id: {token_id})")

action = input("4) Buy or Sell? (b/s): ").strip().lower()
if action not in ['b', 's']:
    print("Invalid action.")
    exit(1)

side = BUY if action == 'b' else SELL
side_name = "BUY" if action == 'b' else "SELL"

if action == 'b':
    amount = float(input(f"Enter USDC amount to {side_name}: "))
    est_shares = amount / mid if mid > 0 else "?"
    print(f"Estimated shares: {est_shares}")
else:
    bal = get_balance(token_id)
    print(f"Current balance: {bal:.4f} shares")
    amount = float(input(f"Enter shares to {side_name} (max {bal:.4f}): "))
    if amount > bal:
        print("Not enough shares!")
        exit(1)
    est_usdc = amount * mid
    print(f"Estimated USDC: ${est_usdc:.2f}")

confirm = input(f"\nConfirm {side_name} {amount} on {outcome_name}? (y/n): ")
if confirm.lower() != 'y':
    print("Cancelled.")
    exit(1)

print("\nPlacing order...")
place_market_order(token_id, str(amount), side)
print("Done!")
