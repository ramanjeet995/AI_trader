"""
Position sizing using the strategy's golden rule:
  Never risk more than 1–2% of account per trade.

  shares = (account_value × risk_pct) / (entry - stop)
  capped at MAX_POSITION_PCT of portfolio value.

The cap can reduce the trade size, in which case the actual $ at risk is
LOWER than the target risk_pct. We report the ACTUAL risk, not the target.
"""

import math


def position_size(account_value: float, entry: float, stop: float, cfg,
                  risk_mult: float = 1.0, target_R: float = 2.0) -> dict:
    """
    Returns dict with shares, notional, ACTUAL risk_dollars, and R values.

    risk_mult : conviction multiplier on cfg.ACCOUNT_RISK_PCT (1.0 = base)
    target_R  : R-multiple for the take-profit target (was hardcoded 2R)
    """
    target_risk   = account_value * cfg.ACCOUNT_RISK_PCT * risk_mult
    stop_distance = abs(entry - stop)

    if stop_distance == 0 or entry <= 0:
        return {"shares": 0, "notional": 0,
                "risk_dollars": 0, "target_risk": target_risk,
                "r1_target": entry, "r2_target": entry, "r_target": entry,
                "target_R": target_R, "capped": False}

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
    r_main = round(entry + stop_distance * target_R, 2)   # dynamic target

    return {
        "shares"      : shares,
        "notional"    : round(notional, 2),
        "risk_dollars": round(actual_risk_dollars, 2),
        "target_risk" : round(target_risk, 2),
        "r1_target"   : r1,
        "r2_target"   : r2,
        "r_target"    : r_main,     # use this as the take-profit
        "target_R"    : target_R,
        "capped"      : capped,
    }
