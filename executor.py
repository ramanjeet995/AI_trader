"""
Auto trade executor — places bracket orders on Alpaca.

A bracket order = entry + stop-loss + take-profit in one atomic order.

Guards:
  - Already holding the symbol     → skip
  - Shares = 0 after sizing        → skip
  - Notional > available BP        → skip (and decrement remaining BP across loop)
  - Invalid prices (stop >= entry) → skip
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


def get_buying_power(trade_client: TradingClient) -> float:
    try:
        return float(trade_client.get_account().buying_power)
    except Exception:
        return 0.0


def is_market_open(trade_client: TradingClient) -> tuple[bool, str]:
    """
    Returns (is_open, reason). Used to skip order placement outside RTH —
    Alpaca cancels DAY orders submitted past close, and even GTC market
    orders placed when closed sit in a weird limbo.
    """
    try:
        clock = trade_client.get_clock()
        if clock.is_open:
            return True, "market open"
        return False, f"market closed (next open: {clock.next_open})"
    except Exception as e:
        # Fail-safe: assume open if clock check fails (don't silently block)
        return True, f"clock check failed: {e}"


def execute(signal: dict, pos: dict, trade_client: TradingClient,
            remaining_bp: float | None = None) -> dict:
    """
    signal       : output from strategies.scan() with 'symbol' attached
    pos          : output from risk.position_size()
    remaining_bp : if provided, skip if notional > remaining_bp.
                   Caller should decrement after a successful order.

    Returns {status, order_id?, reason?, notional}
    """
    symbol = signal["symbol"]
    shares = pos["shares"]
    entry  = signal["entry"]
    stop   = signal["stop"]

    if shares <= 0:
        return {"status": "SKIPPED", "reason": "0 shares after sizing", "notional": 0}

    take_profit_price = pos["r2_target"]

    if stop >= entry or take_profit_price <= entry:
        return {"status": "SKIPPED",
                "reason": f"invalid prices entry={entry} stop={stop} tp={take_profit_price}",
                "notional": 0}

    notional = shares * entry
    if remaining_bp is not None and notional > remaining_bp:
        return {"status": "SKIPPED",
                "reason": f"insufficient BP — need ${notional:,.2f}, have ${remaining_bp:,.2f}",
                "notional": notional}

    try:
        # GTC so the take-profit + stop-loss legs survive past session close.
        # DAY would orphan the position overnight (legs cancel at 4PM ET).
        order_req = MarketOrderRequest(
            symbol        = symbol,
            qty           = shares,
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.GTC,
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
            "notional": notional,
        }
    except Exception as e:
        return {"status": "ERROR", "reason": str(e), "notional": notional}
