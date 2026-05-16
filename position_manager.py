"""
Position manager — trails stops on winning positions so we let winners run.

Runs at the start of every scan (FULL/NEWS/CATALYST). For each open position:
  - Compute current P&L in R-multiples (R = entry minus ORIGINAL stop)
  - Ratchet the protective stop-loss order UP based on conviction-aware rules
  - Never lower a stop — only tighten
  - Exit immediately if SPY regime flips to BEAR

Persists original (entry, stop) per symbol in position_state.json so we can
correctly compute R-multiples even after we've trailed the stop above entry.

Rules (R-multiple based on profit since entry):
  R < 1.0        : do nothing — give trade room to develop
  1.0 <= R < 2.0 : move stop to break-even (was +2R, but 52% of trades never
                   reached +2R — moving to +1R prevents green-to-red reversals)
  2.0 <= R < 3.0 : trail stop to entry + 0.5R (lock in half-R profit)
  3.0 <= R < 5.0 : trail stop to entry + 1R (lock in +1R profit)
  R >= 5.0       : trail stop to max(current - 1.5*ATR, entry + 2R)
                   → tighter ATR trail (was 2x) for trend continuation

If price closed below SMA20 on the daily, tighten stop hard regardless of R
(trend break warning).
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from alpaca.trading.requests import GetOrdersRequest, ReplaceOrderRequest
from alpaca.trading.enums import QueryOrderStatus
from alpaca.data.requests import StockLatestTradeRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame


STATE_FILE = Path(__file__).parent / "position_state.json"


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception:
        pass


def _get_or_set_original(state: dict, symbol: str, entry: float, current_stop: float) -> dict:
    """
    Track the original (entry, stop) per symbol so we keep correct R-math
    after the stop is trailed above entry.

    - First time we see a symbol: record current entry + stop as ORIGINAL.
    - Already tracked: return saved values.
    - If saved entry differs significantly (new position in same symbol):
      reset to new values.
    """
    saved = state.get(symbol)
    if saved and abs(saved.get("entry", 0) - entry) < 0.01:
        return saved
    # New or re-entered: record original
    # Use current_stop as original stop ONLY if it's below entry (sane).
    # Otherwise (e.g., we caught the position mid-trail), fall back to
    # 2% below entry as an estimate.
    original_stop = current_stop if current_stop < entry else round(entry * 0.98, 2)
    state[symbol] = {"entry": entry, "original_stop": original_stop,
                     "first_seen": datetime.utcnow().isoformat()}
    return state[symbol]


def _prune_closed(state: dict, held_symbols: set):
    """Drop state for positions that are no longer open."""
    return {sym: data for sym, data in state.items() if sym in held_symbols}


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
        "reviewed": 0, "moved_to_breakeven": 0, "trailed_half_R": 0,
        "trailed_1R": 0, "trailed_atr": 0, "regime_closed": 0,
        "time_stopped": 0, "no_stop": 0, "no_change": 0, "errors": 0,
        "details": [],
    }

    try:
        positions = trade_client.get_all_positions()
    except Exception as e:
        summary["errors"] += 1
        summary["error_message"] = str(e)
        return summary

    if not positions:
        # Still prune state to drop closed positions
        _save_state({})
        return summary

    state = _load_state()
    held_symbols = {p.symbol for p in positions}
    state = _prune_closed(state, held_symbols)

    for position in positions:
        symbol = position.symbol
        try:
            result = _manage_one(position, trade_client, data_client, spy_regime, cfg, state)
            summary["reviewed"] += 1
            action = result.get("action", "")
            if   action == "breakeven":   summary["moved_to_breakeven"] += 1
            elif action == "trail_half_R": summary["trailed_half_R"] += 1
            elif action == "trail_1R":    summary["trailed_1R"] += 1
            elif action == "trail_atr":   summary["trailed_atr"] += 1
            elif action == "regime_exit": summary["regime_closed"] += 1
            elif action == "time_stop":   summary["time_stopped"] += 1
            elif action == "no_stop":     summary["no_stop"] += 1
            elif action in ("hold", "no_change_needed"): summary["no_change"] += 1
            summary["details"].append(result)
        except Exception as e:
            summary["errors"] += 1
            summary["details"].append({"symbol": symbol, "action": "error", "error": str(e)})

    _save_state(state)
    return summary


MAX_HOLD_DAYS = 20   # Force-close positions held longer than this (dead money)


def _manage_one(position, trade_client, data_client, spy_regime: str, cfg, state: dict = None) -> dict:
    if state is None:
        state = {}
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

    # ─── Time stop — close positions that go nowhere in MAX_HOLD_DAYS ────────
    saved = state.get(symbol)
    if saved and saved.get("first_seen"):
        try:
            first_seen = datetime.fromisoformat(saved["first_seen"])
            hold_days = (datetime.utcnow() - first_seen).days
            if hold_days >= MAX_HOLD_DAYS:
                trade_client.close_position(symbol)
                return {"symbol": symbol, "action": "time_stop",
                        "reason": f"held {hold_days} days — force-closing dead money",
                        "hold_days": hold_days}
        except Exception:
            pass   # If we can't parse date, skip time stop — don't block normal management

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

    # Look up or record the ORIGINAL stop distance (the initial 1R unit).
    # Once we trail the stop above entry, current_stop > entry and the simple
    # formula (entry - current_stop) becomes negative — so we use the saved
    # original from state to keep R-math correct.
    saved = _get_or_set_original(state, symbol, entry, current_stop)
    original_stop = float(saved["original_stop"])
    stop_distance = entry - original_stop   # original 1R distance
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
        # Break-even at +1R (was +2R). Most trades never reached +2R before
        # reversing — this stops the bleed from green-to-red.
        new_stop = round(entry * 1.001, 2)
        action   = "breakeven"
        note     = "moved to break-even at +1R"

    elif 2.0 <= r_multiple < 3.0:
        # Lock in half-R (new tier — preserves some profit while giving room)
        new_stop = round(entry + stop_distance * 0.5, 2)
        action   = "trail_half_R"
        note     = "trailed to +0.5R floor"

    elif 3.0 <= r_multiple < 5.0:
        # Trail at entry + 1R (locks in +1R profit minimum)
        new_stop = round(entry + stop_distance * 1.0, 2)
        action   = "trail_1R"
        note     = "trailed to +1R floor"

    else:  # r_multiple >= 5.0
        # Tighter ATR trail: 1.5x ATR (was 2x) — captures more of the move
        atr, sma20, latest_close = _get_atr_and_sma20(symbol, data_client)
        candidates = [current_stop, entry + stop_distance * 2.0]
        if atr is not None:
            candidates.append(current - 1.5 * atr)
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
