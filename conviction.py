"""
Conviction scoring — multi-factor confluence layer for aggressive trading.

Philosophy: on a $5k account, you can't afford "mediocre" trades. Costs and
small position sizes mean only HIGH-CONVICTION setups (multiple signals
aligning) have positive expectancy after friction.

Scores each candidate signal on 8 dimensions. Only trades with score >=
MIN_CONVICTION_TO_TRADE fire. Position size and target scale with conviction.

Factors:
  1. Strong technical setup (strategy confidence >= 0.7)
  2. Relative strength vs SPY (RS >= 1.05)
  3. OBV accumulation (institutional volume)
  4. Order flow score (+2/+3)
  5. Positive news sentiment (score >= +2)
  6. Analyst upgrade/PT raise from reputable firm (score >= +2)
  7. Hot sector (rotation score >= 5)
  8. Favorable macro (VIX < 20, regime BULL)
"""


def score(signal: dict, context: dict, cfg) -> dict:
    """
    Returns:
      {
        "score"    : int    (0-8),
        "factors"  : list   (which factors triggered)
        "missing"  : list   (which factors did not),
        "should_trade" : bool,
        "risk_mult"    : float  (multiplier on ACCOUNT_RISK_PCT),
        "target_R"     : float  (R-multiple target),
      }

    signal expects: confidence, strategy
    context expects: rs, obv, of_score, sent_score, analyst_score,
                     sector_score, vix, regime
    """
    factors = []
    missing = []

    # 1. Strong technical setup (strategy confidence > 0.7)
    if signal.get("confidence", 0) >= 0.7:
        factors.append(f"strong-tech (conf {signal['confidence']:.2f})")
    else:
        missing.append(f"weak-tech (conf {signal.get('confidence',0):.2f})")

    # 2. Outperforming SPY by 5%+ (relative strength)
    rs = context.get("rs", 1.0)
    if rs >= 1.05:
        factors.append(f"strong-RS ({rs:.2f})")
    else:
        missing.append(f"weak-RS ({rs:.2f})")

    # 3. Institutional accumulation in volume
    obv = context.get("obv", "")
    if obv in ("ACCUMULATION", "STEALTH_BUY"):
        factors.append(f"OBV-{obv}")
    else:
        missing.append(f"OBV-{obv or 'unknown'}")

    # 4. Strong order flow score (+2 or +3 out of +3)
    of_score = context.get("of_score", 0)
    if of_score >= 2:
        factors.append(f"order-flow (+{of_score})")
    else:
        missing.append(f"weak-order-flow ({of_score:+d})")

    # 5. Positive news sentiment (base score, before analyst bonus)
    sent = context.get("sent_score", 0)
    if sent >= 2:
        factors.append(f"news (+{sent})")
    else:
        missing.append(f"neutral/neg-news ({sent:+d})")

    # 6. Analyst rating boost — upgrade or PT raise from reputable firm
    #    analyst_score comes from analyst_ratings.py (weighted by firm tier).
    #    Score >= +2 means at least one tier-2 upgrade or tier-1 PT raise.
    analyst = context.get("analyst_score", 0)
    if analyst >= 2:
        factors.append(f"analyst-upgrade (+{analyst})")
    elif analyst <= -2:
        missing.append(f"analyst-downgrade ({analyst:+d})")
    else:
        missing.append(f"no-analyst-signal ({analyst:+d})")

    # 7. Hot sector (top 3 in rotation, score >= 5)
    sect = context.get("sector_score", 0)
    if sect >= 5 or sect == 999:   # 999 = stock not in any sector bucket = no penalty
        factors.append(f"sector-hot ({sect:.1f})")
    else:
        missing.append(f"sector-cold ({sect:.1f})")

    # 8. Favorable macro (VIX < 20, regime BULL)
    vix = context.get("vix") or 0
    regime = context.get("regime", "")
    macro_ok = (vix < 20 if vix > 0 else True) and regime == "BULL"
    if macro_ok:
        factors.append(f"macro-OK (VIX {vix:.1f}, {regime})")
    else:
        missing.append(f"macro-soft (VIX {vix:.1f}, {regime})")

    total = len(factors)
    tier  = cfg.CONVICTION_RISK_TIERS.get(total) or cfg.CONVICTION_RISK_TIERS.get(
        min(cfg.CONVICTION_RISK_TIERS.keys(), key=lambda k: abs(k-total)))

    return {
        "score"        : total,
        "factors"      : factors,
        "missing"      : missing,
        "should_trade" : total >= cfg.MIN_CONVICTION_TO_TRADE,
        "risk_mult"    : tier["risk_mult"] if total >= cfg.MIN_CONVICTION_TO_TRADE else 0.0,
        "target_R"     : tier["target_R"]  if total >= cfg.MIN_CONVICTION_TO_TRADE else 0.0,
    }
