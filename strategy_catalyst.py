"""
Catalyst strategy — sizing and bracket levels for event-driven trades.

Different risk model from swing:
  - Tight stop (% based, NOT ATR — gap stocks have absurd ATR)
  - Tight target (just take the move, don't be greedy on a gap)
  - Half the swing position size (gap stocks are wilder)
  - Force-exit by EOD day 2 (avoid drifting into a stale long)
"""

import math


def build_signal(detection: dict, account_value: float, cfg) -> dict:
    """
    Given a fired detection from catalyst_detector.detect(), build a
    {symbol, entry, stop, target, shares, notional, risk_$} signal.
    """
    entry  = detection["current"]
    # Stop = max(% stop floor, just below day's low) — whichever is tighter
    pct_stop      = entry * (1 - cfg.CATALYST_STOP_PCT / 100)
    day_low_stop  = detection["day_low"] * 0.995    # 0.5% below today's low
    stop          = max(pct_stop, day_low_stop)     # tighter (higher) of the two
    target        = entry * (1 + cfg.CATALYST_TARGET_PCT / 100)

    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return {"valid": False, "reason": "bad risk (stop >= entry)"}

    # Half of normal swing risk
    catalyst_risk_pct = cfg.ACCOUNT_RISK_PCT * cfg.CATALYST_SIZE_FACTOR
    risk_dollars      = account_value * catalyst_risk_pct
    shares            = math.floor(risk_dollars / risk_per_share)

    # Position-size cap (smaller than swing)
    notional       = shares * entry
    max_notional   = account_value * cfg.CATALYST_MAX_POSITION_PCT
    if notional > max_notional:
        shares   = math.floor(max_notional / entry)
        notional = shares * entry

    if shares <= 0:
        return {"valid": False, "reason": "0 shares after sizing"}

    actual_risk = shares * risk_per_share

    return {
        "valid"       : True,
        "entry"       : round(entry, 2),
        "stop"        : round(stop, 2),
        "target"      : round(target, 2),
        "shares"      : shares,
        "notional"    : round(notional, 2),
        "risk_dollars": round(actual_risk, 2),
        "target_risk" : round(risk_dollars, 2),
    }
