"""
Place a notional market order — buy $5000 worth of AAPL.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv(Path(__file__).parent / ".env")

API_KEY    = os.environ["ALPACA_API_KEY"]
API_SECRET = os.environ["ALPACA_API_SECRET"]
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

client = TradingClient(API_KEY, API_SECRET, paper=PAPER)

order_request = MarketOrderRequest(
    symbol       = "AAPL",
    notional     = 5000,       # dollar amount instead of share qty
    side         = OrderSide.BUY,
    time_in_force= TimeInForce.DAY,
)

order = client.submit_order(order_request)

print(f"Order submitted!")
print(f"  ID        : {order.id}")
print(f"  Symbol    : {order.symbol}")
print(f"  Side      : {order.side}")
print(f"  Notional  : ${order.notional}")
print(f"  Status    : {order.status}")
print(f"  Created at: {order.created_at}")
