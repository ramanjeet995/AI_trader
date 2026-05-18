from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from dotenv import load_dotenv
import os
load_dotenv()
tc = TradingClient(os.environ['ALPACA_API_KEY'], os.environ['ALPACA_API_SECRET'], paper=True)
ps = tc.get_all_positions()
acct = tc.get_account()
print(f"Account value: ${float(acct.equity):,.2f}")
print(f"Cash: ${float(acct.cash):,.2f}")
print()
if not ps:
    print("No open positions")
else:
    for p in ps:
        entry = float(p.avg_entry_price)
        current = float(p.current_price)
        pnl = float(p.unrealized_pl)
        pnl_pct = float(p.unrealized_plpc) * 100
        qty = int(float(p.qty))
        cost = entry * qty
        print(f"{p.symbol:<6} qty={qty}  entry=${entry:.2f}  now=${current:.2f}  P&L=${pnl:+,.2f} ({pnl_pct:+.1f}%)  cost=${cost:,.0f}")

# Check stop orders
print()
orders = tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=20))
stops = [o for o in orders if 'stop' in str(o.type).lower()]
if stops:
    print("Active stop-loss orders:")
    for o in stops:
        print(f"  {o.symbol:<6} stop @ ${float(o.stop_price):.2f}")
else:
    print("No active stop-loss orders")
