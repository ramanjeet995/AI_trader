"""
Check status of all open/recent orders.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

load_dotenv(Path(__file__).parent / ".env")

client = TradingClient(
    os.environ["ALPACA_API_KEY"],
    os.environ["ALPACA_API_SECRET"],
    paper=True,
)

orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=10))

if not orders:
    print("No recent orders found.")
else:
    for o in orders:
        filled = f"  Filled qty : {o.filled_qty} @ avg ${o.filled_avg_price}" if o.filled_qty else ""
        print(f"{o.symbol} | {str(o.side):<18} | ${o.notional or str(o.qty)+' shares':<10} | {str(o.status):<30}{filled}")
