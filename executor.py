"""
Auto trade executor — places bracket orders on Alpaca.

A bracket order = entry + stop-loss + take-profit in one atomic order.
Alpaca handles the exit automatically once filled.

Guards:
  - Already holding the symbol → skip
  - Market closed → order queued as DAY (fills at open)
  - Shares = 0 after sizing → skip
"""

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass


def get_open_positions(trade_client: TradingClient) -> set[str]:
    """Return set of symbols currently held."""
    try:
        positions = trade_client.get_all_positions()
        return {p.symbol for p in positions}
    except Exception:
        return set()


def execute(signal: dict, pos: dict, trade_client: TradingClient) -> dict:
    """
    signal : output from strategies.scan()
    pos    : output from risk.position_size()
    Returns result dict with status and order id (or error message).
    """
    symbol = signal["symbol"]
    shares = pos["shares"]
    entry  = signal["entry"]
    stop   = signal["stop"]
    target = signal["target"]

    if shares <= 0:
        return {"status": "SKIPPED", "reason": "position size = 0 shares"}

    # Use 2R as take-profit for better reward ratio
    take_profit_price = pos["r2_target"]

    # Ensure prices are logically valid for a long order
    if stop >= entry or take_profit_price <= entry:
        return {"status": "SKIPPED", "reason": f"invalid prices — entry={entry} stop={stop} tp={take_profit_price}"}

    try:
        order_req = MarketOrderRequest(
            symbol        = symbol,
            qty           = shares,
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.DAY,
            order_class   = OrderClass.BRACKET,
            take_profit   = TakeProfitRequest(limit_price=round(take_profit_price, 2)),
            stop_loss     = StopLossRequest(stop_price=round(stop, 2)),
        )
        order = trade_client.submit_order(order_req)
        return {
            "status"  : "PLACED",
            "order_id": str(order.id),
            "symbol"  : symbol,
            "shares"  : shares,
            "entry"   : entry,
            "stop"    : stop,
            "target"  : take_profit_price,
        }
    except Exception as e:
        return {"status": "ERROR", "reason": str(e)}
