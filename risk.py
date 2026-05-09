"""
Position sizing using the strategy's golden rule:
  Never risk more than 1–2% of account per trade.

  shares = (account_value × risk_pct) / (entry - stop)
  capped at MAX_POSITION_PCT of portfolio value.

The cap can reduce the trade size, in which case the actual $ at risk is
LOWER than the target risk_pct. We report the ACTUAL risk, not the target.
"""

import math


def position_size(account_value: float, entry: float, stop: float, cfg) -> dict:
    """
    Returns dict with shares, notional, ACTUAL risk_dollars, and R values.
    Note: risk_dollars reflects what's truly at risk after the position cap.
    """
    target_risk   = account_value * cfg.ACCOUNT_RISK_PCT
    stop_distance = abs(entry - stop)

    if stop_distance == 0 or entry <= 0:
        return {"shares": 0, "notional": 0,
                "risk_dollars": 0, "target_risk": target_risk,
                "r1_target": entry, "r2_target": entry,
                "capped": False}

    raw_shares   = target_risk / stop_distance
    max_notional = account_value * cfg.MAX_POSITION_PCT
    capped       = (raw_shares * entry) > max_notional

    if capped:
        shares = math.floor(max_notional / entry)
    else:
        shares = math.floor(raw_shares)

    actual_risk_dollars = shares * stop_distance
    notional            = shares * entry

    r1 = round(entry + stop_distance * 1, 2)
    r2 = round(entry + stop_distance * 2, 2)

    return {
        "shares"      : shares,
        "notional"    : round(notional, 2),
        "risk_dollars": round(actual_risk_dollars, 2),   # ACTUAL, not target
        "target_risk" : round(target_risk, 2),
        "r1_target"   : r1,
        "r2_target"   : r2,
        "capped"      : capped,
    }
