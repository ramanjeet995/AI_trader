"""
Position manager — trails stops on winning positions so we let winners run.

Runs at the start of every scan (FULL/NEWS/CATALYST). For each open position:
  - Compute current P&L in R-multiples (R = entry minus original stop)
  - Ratchet the protective stop-loss order UP based on conviction-aware rules
  - Never lower a stop — only tighten
  - Exit immediately if SPY regime flips to BEAR

Rules (R-multiple based on profit since entry):
  R < 1.0        : do nothing — let original stop work
  1.0 <= R < 2.0 : move stop to break-even (lock in winning trade)
  2.0 <= R < 3.0 : trail stop to entry + 1R (lock in +1R profit)
  R >= 3.0       : trail stop to max(current - 2*ATR, entry + 2R)
                   → captures trend continuation while protecting gains

If price closed below SMA20 on the daily, tighten stop hard regardless of R
(trend break warning).
"""

from datetime import datetime, timedelta

import pandas as pd

from alpaca.trading.requests import GetOrdersRequest, ReplaceOrderRequest
from alpaca.trading.enums import QueryOrderStatus
from alpaca.data.requests import StockLatestTradeRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame


def _get_current_price(symbol, data_client):
    """Latest trade price for a symbol. Returns None on failure."""
    try:
        req   = StockLatestTradeRequest(symbol_or_symbols=symbol)
        resp  = data_client.get_stock_latest_trade(req)
        trade = resp.get(symbol) if isinstance(resp, dict) else resp
        return float(getattr(trade, "price", 0) or 0)
    except Exception:
        return None


def _get_atr_and_sma20(symbol, data_client, period: int = 14):
    """Return (latest_atr, latest_sma20, latest_close) or (None, None, None)."""
    try:
        end   = datetime.utcnow()
        start = end - timedelta(days=60)
        req   = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                                  start=start, end=end, feed="iex")
        bars  = data_client.get_stock_bars(req).df
        if bars is None or bars.empty:
            return None, None, None
        if isinstance(bars.index, pd.MultiIndex):
            df = bars.loc[symbol]
        else:
            df = bars
        if len(df) < period + 1:
            return None, None, None
        hl   = df["high"] - df["low"]
        hc   = (df["high"] - df["close"].shift()).abs()
        lc   = (df["low"]  - df["close"].shift()).abs()
        tr   = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr  = float(tr.rolling(period).mean().iloc[-1])
        sma20  = float(df["close"].rolling(20).mean().iloc[-1])
        close  = float(df["close"].iloc[-1])
        return atr, sma20, close
    except Exception:
        return None, None, None


def _find_protective_stop(symbol, trade_client):
    """Find the active stop-loss order for this symbol (from bracket child)."""
    try:
        orders = trade_client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol], limit=20
        ))
        for o in orders:
            type_str = str(o.type).lower()
            side_str = str(o.side).lower()
            # Looking for SELL STOP or SELL STOP_LIMIT (the protective leg)
            if ("stop" in type_str) and "sell" in side_str:
                return o
        return None
    except Exception:
        return None


def manage_positions(trade_client, data_client, spy_regime: str = "", cfg=None) -> dict:
    """
    Review all open positions, trail stops as appropriate, exit on regime flip.
    Returns summary dict for logging.
    """
    summary = {
        "reviewed": 0, "moved_to_breakeven": 0, "trailed_1R": 0,
        "trailed_atr": 0, "regime_closed": 0, "no_stop": 0,
        "no_change": 0, "errors": 0, "details": [],
    }

    try:
        positions = trade_client.get_all_positions()
    except Exception as e:
        summary["errors"] += 1
        summary["error_message"] = str(e)
        return summary

    if not positions:
        return summary

    for position in positions:
        symbol = position.symbol
        try:
            result = _manage_one(position, trade_client, data_client, spy_regime, cfg)
            summary["reviewed"] += 1
            action = result.get("action", "")
            if   action == "breakeven":   summary["moved_to_breakeven"] += 1
            elif action == "trail_1R":    summary["trailed_1R"] += 1
            elif action == "trail_atr":   summary["trailed_atr"] += 1
            elif action == "regime_exit": summary["regime_closed"] += 1
            elif action == "no_stop":     summary["no_stop"] += 1
            elif action in ("hold", "no_change_needed"): summary["no_change"] += 1
            summary["details"].append(result)
        except Exception as e:
            summary["errors"] += 1
            summary["details"].append({"symbol": symbol, "action": "error", "error": str(e)})

    return summary


def _manage_one(position, trade_client, data_client, spy_regime: str, cfg) -> dict:
    symbol = position.symbol
    entry  = float(position.avg_entry_price)
    qty    = int(float(position.qty))

    # ─── Regime-flip exit ─────────────────────────────────────────────────────
    # If SPY flipped to BEAR while we were long, exit immediately.
    if spy_regime == "BEAR":
        try:
            trade_client.close_position(symbol)
            return {"symbol": symbol, "action": "regime_exit",
                    "reason": "SPY flipped to BEAR — closing all longs"}
        except Exception as e:
            return {"symbol": symbol, "action": "regime_exit_failed", "error": str(e)}

    # ─── Get current price and protective stop order ─────────────────────────
    current = _get_current_price(symbol, data_client)
    if current is None or current <= 0:
        return {"symbol": symbol, "action": "no_price"}

    stop_order = _find_protective_stop(symbol, trade_client)
    if stop_order is None:
        return {"symbol": symbol, "action": "no_stop",
                "reason": "no protective stop-loss order found"}

    try:
        current_stop = float(stop_order.stop_price)
    except (TypeError, AttributeError):
        return {"symbol": symbol, "action": "no_stop", "reason": "stop price unreadable"}

    stop_distance = entry - current_stop   # original 1R distance
    if stop_distance <= 0:
        return {"symbol": symbol, "action": "invalid_stop"}

    r_multiple = (current - entry) / stop_distance

    # ─── Decide new stop based on R-multiple ──────────────────────────────────
    new_stop = current_stop
    action   = "hold"
    note     = ""

    if r_multiple < 1.0:
        return {"symbol": symbol, "action": "hold",
                "r_multiple": round(r_multiple, 2),
                "current": round(current, 2), "current_stop": round(current_stop, 2)}

    elif 1.0 <= r_multiple < 2.0:
        # Move stop to break-even (with tiny buffer above entry to cover slippage/fees)
        new_stop = round(entry * 1.001, 2)
        action   = "breakeven"
        note     = "moved to break-even"

    elif 2.0 <= r_multiple < 3.0:
        # Trail at entry + 1R (locks in +1R profit minimum)
        new_stop = round(entry + stop_distance * 1.0, 2)
        action   = "trail_1R"
        note     = "trailed to +1R floor"

    else:  # r_multiple >= 3.0
        # Aggressive trail: max(current - 2*ATR, entry + 2R)
        atr, sma20, latest_close = _get_atr_and_sma20(symbol, data_client)
        candidates = [current_stop, entry + stop_distance * 2.0]
        if atr is not None:
            candidates.append(current - 2 * atr)
        # Trend break warning: if today closed below SMA20, tighten harder
        if sma20 is not None and latest_close is not None and latest_close < sma20:
            # Tighten to recent price minus 1*ATR if ATR known, else trail at +2R
            if atr is not None:
                candidates.append(current - 1 * atr)
            note = " (trend break: closed below SMA20)"
        new_stop = round(max(candidates), 2)
        action   = "trail_atr"
        note     = f"trailed ATR-style at R={r_multiple:.1f}" + note

    # Only update if the new stop is HIGHER than current (never widen)
    if new_stop <= current_stop + 0.01:
        return {"symbol": symbol, "action": "no_change_needed",
                "r_multiple": round(r_multiple, 2),
                "current_stop": round(current_stop, 2),
                "computed_stop": round(new_stop, 2)}

    # Replace the stop-loss order
    try:
        trade_client.replace_order_by_id(
            order_id=stop_order.id,
            order_data=ReplaceOrderRequest(stop_price=new_stop)
        )
        return {
            "symbol": symbol, "action": action, "note": note,
            "r_multiple": round(r_multiple, 2),
            "current": round(current, 2), "entry": round(entry, 2),
            "old_stop": round(current_stop, 2), "new_stop": new_stop,
        }
    except Exception as e:
        return {"symbol": symbol, "action": "replace_failed",
                "current_stop": round(current_stop, 2),
                "new_stop": new_stop, "error": str(e)}
