"""
Position sizing using the strategy's golden rule:
  Never risk more than 1–2% of account per trade.

  shares = (account_value × risk_pct) / (entry - stop)
  capped at MAX_POSITION_PCT of portfolio value.
"""


def position_size(account_value: float, entry: float, stop: float, cfg) -> dict:
    """
    Returns dict with shares, notional, risk_dollars, and R values.
    """
    risk_dollars  = account_value * cfg.ACCOUNT_RISK_PCT
    stop_distance = abs(entry - stop)

    if stop_distance == 0:
        return {"shares": 0, "notional": 0, "risk_dollars": 0,
                "r1_target": entry, "r2_target": entry}

    shares = risk_dollars / stop_distance

    # Cap at MAX_POSITION_PCT of portfolio
    max_notional = account_value * cfg.MAX_POSITION_PCT
    notional     = shares * entry
    if notional > max_notional:
        shares   = max_notional / entry
        notional = max_notional

    shares = int(shares)   # whole shares only

    r1 = round(entry + stop_distance * 1, 2)   # 1R target
    r2 = round(entry + stop_distance * 2, 2)   # 2R target

    return {
        "shares"      : shares,
        "notional"    : round(shares * entry, 2),
        "risk_dollars": round(risk_dollars, 2),
        "r1_target"   : r1,
        "r2_target"   : r2,
    }
