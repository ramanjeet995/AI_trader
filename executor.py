"""
Trade executor — sizes positions, places OTO orders on Alpaca.

  - position_size(): conviction-aware sizing with dynamic R target
  - execute(): market buy + attached stop-loss (no take-profit ceiling).
    Exits are managed by position_manager.py which trails stops as price
    rises — letting winners run instead of capping at a fixed target.
  - close_position(), list_catalyst_positions(): position lifecycle helpers
  - is_market_open(): clock check used to skip pre-market submissions

Guards on execute():
  - Already holding the symbol     → skip
  - Shares = 0 after sizing        → skip
  - Notional > available BP        → skip
  - Invalid prices (stop >= entry) → skip
"""

import math
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass


# ─── Position sizing ──────────────────────────────────────────────────────────

def position_size(account_value: float, entry: float, stop: float, cfg,
                  risk_mult: float = 1.0, target_R: float = 2.0) -> dict:
    """
    Calculate share count, stop/target levels for a trade.
    risk_mult : conviction multiplier on cfg.ACCOUNT_RISK_PCT (1.0 = base)
    target_R  : R-multiple for take-profit (2.0 = standard 2R, 5.0 = stretch)
    """
    target_risk   = account_value * cfg.ACCOUNT_RISK_PCT * risk_mult
    stop_distance = abs(entry - stop)

    if stop_distance == 0 or entry <= 0:
        return {"shares": 0, "notional": 0, "risk_dollars": 0,
                "target_risk": target_risk, "r1_target": entry,
                "r2_target": entry, "r_target": entry,
                "target_R": target_R, "capped": False}

    raw_shares   = target_risk / stop_distance
    max_notional = account_value * cfg.MAX_POSITION_PCT
    capped       = (raw_shares * entry) > max_notional
    shares       = math.floor(max_notional / entry if capped else raw_shares)

    return {
        "shares"      : shares,
        "notional"    : round(shares * entry, 2),
        "risk_dollars": round(shares * stop_distance, 2),
        "target_risk" : round(target_risk, 2),
        "r1_target"   : round(entry + stop_distance * 1, 2),
        "r2_target"   : round(entry + stop_distance * 2, 2),
        "r_target"    : round(entry + stop_distance * target_R, 2),
        "target_R"    : target_R,
        "capped"      : capped,
    }


# ─── Position queries ─────────────────────────────────────────────────────────


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


def close_position(symbol: str, trade_client: TradingClient) -> dict:
    """Market-close a position (used for catalyst force-exit on Day 2)."""
    try:
        order = trade_client.close_position(symbol)
        return {"status": "CLOSED", "order_id": str(order.id), "symbol": symbol}
    except Exception as e:
        return {"status": "ERROR", "reason": str(e), "symbol": symbol}


def list_catalyst_positions(trade_client: TradingClient, prefix: str) -> list[dict]:
    """
    Find currently-held positions opened by the catalyst mode (by inspecting
    historical orders' client_order_id prefix). Returns [{symbol, entry_date}].
    """
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        from datetime import datetime, timedelta

        # Look at last 10 days of filled orders
        since  = datetime.utcnow() - timedelta(days=10)
        orders = trade_client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, after=since, limit=200,
        ))
        cat_orders = [o for o in orders
                      if o.client_order_id and o.client_order_id.startswith(prefix + "-")
                      and str(o.side).endswith("BUY")
                      and str(o.status).endswith("FILLED")]

        held = {p.symbol for p in trade_client.get_all_positions()}
        out  = []
        for o in cat_orders:
            if o.symbol in held:
                out.append({
                    "symbol"     : o.symbol,
                    "filled_at"  : o.filled_at,
                    "client_id"  : o.client_order_id,
                })
        return out
    except Exception:
        return []


def execute(signal: dict, pos: dict, trade_client: TradingClient,
            remaining_bp: float | None = None,
            client_order_id: str | None = None) -> dict:
    """
    Place a market buy with an attached stop-loss (OTO = one-triggers-other).
    NO take-profit ceiling — position_manager.py trails the stop up as
    the trade moves in our favor, letting winners run.

    signal       : output from strategies.scan() with 'symbol' attached
    pos          : output from position_size()
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

    if stop >= entry:
        return {"status": "SKIPPED",
                "reason": f"invalid prices entry={entry} stop={stop}",
                "notional": 0}

    notional = shares * entry
    if remaining_bp is not None and notional > remaining_bp:
        return {"status": "SKIPPED",
                "reason": f"insufficient BP — need ${notional:,.2f}, have ${remaining_bp:,.2f}",
                "notional": notional}

    try:
        # OTO: market buy triggers a GTC stop-loss sell.
        # No take-profit leg — position_manager.py trails the stop upward
        # as the trade moves in our favor (break-even → +1R → ATR trail).
        # This removes the hard ceiling that was capping winners.
        order_kwargs = dict(
            symbol        = symbol,
            qty           = shares,
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.GTC,
            order_class   = OrderClass.OTO,
            stop_loss     = StopLossRequest(stop_price=round(stop, 2)),
        )
        if client_order_id:
            order_kwargs["client_order_id"] = client_order_id
        order_req = MarketOrderRequest(**order_kwargs)
        order = trade_client.submit_order(order_req)
        return {
            "status"  : "PLACED",
            "order_id": str(order.id),
            "symbol"  : symbol,
            "shares"  : shares,
            "entry"   : entry,
            "stop"    : stop,
            "notional": notional,
        }
    except Exception as e:
        return {"status": "ERROR", "reason": str(e), "notional": notional}
