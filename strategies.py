"""
Strategy signal generators — A, B, C as defined in the strategy doc.

Each function returns a dict with:
  signal     : "BUY" | None     (long-only)
  strategy   : "A" | "B" | "C"
  confidence : 0.0–1.0           (used for scoring when multiple fire)
  entry      : suggested entry price
  stop       : stop-loss price
  target     : profit target price
  reason     : human-readable explanation
"""

import pandas as pd
from market_structure import Regime


# ── Strategy A: Trend Pullback ───────────────────────────────────────────────

def strategy_a(df: pd.DataFrame, regime: Regime, cfg) -> dict:
    """
    BUY when:
      - Regime is BULL
      - Price pulled back into the EMA20/SMA50 zone (whichever is lower as floor,
        whichever is higher as ceiling — handles deep pullbacks where EMA20 dips
        below SMA50)
      - RSI between 40–55 (healthy pullback, not broken)
      - Last candle is bullish (close > open)
    """
    result = {"signal": None, "strategy": "A", "confidence": 0.0,
              "entry": None, "stop": None, "target": None, "reason": ""}

    if regime != Regime.BULL:
        result["reason"] = f"regime={regime.value}, need BULL"
        return result

    last  = df.iloc[-1]
    price = last["close"]
    ema20 = last["ema20"]
    sma50 = last["sma50"]
    rsi   = last["rsi"]

    # Tighter pullback zone: only EMA20 ±3% (was full EMA20-SMA50 range).
    # Momentum stocks rarely pull back to SMA50; if they do, it's a trend break.
    zone_low      = ema20 * 0.97
    zone_high     = ema20 * 1.03
    in_pullback   = zone_low <= price <= zone_high

    bullish_candle = last["close"] > last["open"]
    rsi_ok         = cfg.STRAT_A_RSI_LOW <= rsi <= cfg.STRAT_A_RSI_HIGH

    # Additional confirmations for momentum stocks:
    above_sma50    = price > sma50      # uptrend structurally intact
    # RSI rising = momentum returning after the dip
    rsi_rising     = (len(df) >= 2 and not pd.isna(df["rsi"].iloc[-2])
                      and rsi > df["rsi"].iloc[-2])

    if in_pullback and bullish_candle and rsi_ok and above_sma50 and rsi_rising:
        recent_low  = df["low"].iloc[-10:].min()
        recent_high = df["high"].iloc[-20:].max()

        # Confidence: higher when RSI is in the sweet spot (45) and candle has range
        rsi_mid    = (cfg.STRAT_A_RSI_LOW + cfg.STRAT_A_RSI_HIGH) / 2
        rsi_score  = 1.0 - abs(rsi - rsi_mid) / (cfg.STRAT_A_RSI_HIGH - rsi_mid)
        confidence = max(0.5, min(1.0, rsi_score))

        result.update({
            "signal"    : "BUY",
            "confidence": round(confidence, 2),
            "entry"     : round(price, 2),
            "stop"      : round(recent_low * 0.99, 2),
            "target"    : round(recent_high, 2),
            "reason"    : f"Pullback zone [{zone_low:.2f}-{zone_high:.2f}] | RSI={rsi:.1f} | bullish candle",
        })
    else:
        reasons = []
        if not in_pullback:    reasons.append(f"price {price:.2f} not in [{zone_low:.2f}, {zone_high:.2f}]")
        if not bullish_candle: reasons.append("no bullish candle")
        if not rsi_ok:         reasons.append(f"RSI={rsi:.1f} outside [{cfg.STRAT_A_RSI_LOW},{cfg.STRAT_A_RSI_HIGH}]")
        if not above_sma50:    reasons.append(f"price below SMA50 ({sma50:.2f}) — trend broken")
        if not rsi_rising:     reasons.append("RSI not rising")
        result["reason"] = " | ".join(reasons)

    return result


# ── Strategy B: Breakout ─────────────────────────────────────────────────────

def strategy_b(df: pd.DataFrame, regime: Regime, cfg) -> dict:
    """
    BUY when:
      - Regime is BULL (or CHOPPY — breakouts can start new trends)
      - Recent consolidation: range < 1.5× ATR
      - Price breaks above the consolidation high
      - Volume on breakout bar >= 1.5× 20-day avg (computed EXCLUDING today)
    """
    result = {"signal": None, "strategy": "B", "confidence": 0.0,
              "entry": None, "stop": None, "target": None, "reason": ""}

    if regime == Regime.BEAR:
        result["reason"] = "regime=BEAR, skipping breakout"
        return result

    last = df.iloc[-1]
    n    = cfg.STRAT_B_CONSOLIDATION_BARS

    if len(df) < n + 25:
        result["reason"] = "not enough bars"
        return result

    consol_window = df.iloc[-(n + 1):-1]
    consol_high   = consol_window["high"].max()
    consol_low    = consol_window["low"].min()
    consol_range  = consol_high - consol_low

    atr_val  = last["atr"]
    tight    = consol_range < atr_val * 2.5   # was 1.5 — looser for momentum
    breakout = last["close"] > consol_high

    # Volume average EXCLUDING today — so today's surge isn't in its own denominator
    vol_avg_prior = df["volume"].iloc[-21:-1].mean()
    vol_ratio     = last["volume"] / vol_avg_prior if vol_avg_prior > 0 else 0
    vol_spike     = vol_ratio >= cfg.STRAT_B_VOLUME_MULT

    if tight and breakout and vol_spike:
        measured_move = consol_high + consol_range

        # Confidence: more volume = higher conviction; tighter range = better
        vol_score   = min(1.0, vol_ratio / (cfg.STRAT_B_VOLUME_MULT * 2))
        tight_score = max(0.0, 1.0 - (consol_range / (atr_val * 1.5)))
        confidence  = max(0.5, min(1.0, (vol_score + tight_score) / 2))

        # Volume on IEX is ~3% of total tape — surge signals are unreliable.
        # Down-weight Strategy B unless this specific symbol's volume was
        # actually patched from yfinance (full SIP tape) or we're on SIP feed.
        feed_is_iex      = getattr(cfg, "DATA_FEED", "iex").lower() == "iex"
        symbol_patched   = bool(df.attrs.get("volume_patched", False))
        if feed_is_iex and not symbol_patched:
            confidence *= 0.7

        result.update({
            "signal"    : "BUY",
            "confidence": round(confidence, 2),
            "entry"     : round(last["close"], 2),
            "stop"      : round(consol_high * 0.99, 2),
            "target"    : round(measured_move, 2),
            "reason"    : (f"Breakout above {consol_high:.2f} | "
                           f"vol {vol_ratio:.1f}x prior 20d avg"),
        })
    else:
        reasons = []
        if not tight:     reasons.append(f"range {consol_range:.2f} not tight vs ATR {atr_val:.2f}")
        if not breakout:  reasons.append(f"no breakout above {consol_high:.2f}")
        if not vol_spike: reasons.append(f"vol {vol_ratio:.1f}x < {cfg.STRAT_B_VOLUME_MULT}x")
        result["reason"] = " | ".join(reasons)

    return result


# ── Strategy C: Mean Reversion (CHOPPY only) ─────────────────────────────────

def strategy_c(df: pd.DataFrame, regime: Regime, cfg) -> dict:
    """
    BUY when RSI < 30 near support, ONLY in CHOPPY regime.
    Mean reversion in a strong trend is dangerous — a stock with RSI 25 in a
    downtrend usually keeps falling.
    """
    result = {"signal": None, "strategy": "C", "confidence": 0.0,
              "entry": None, "stop": None, "target": None, "reason": ""}

    if regime != Regime.CHOPPY:
        result["reason"] = f"regime={regime.value}, need CHOPPY for mean reversion"
        return result

    last  = df.iloc[-1]
    price = last["close"]
    rsi   = last["rsi"]

    support      = df["low"].iloc[-30:].min()
    resistance   = df["high"].iloc[-30:].max()
    near_support = price <= support * 1.03

    if rsi < cfg.STRAT_C_RSI_OVERSOLD and near_support:
        # Confidence: deeper RSI = higher conviction (capped)
        rsi_score  = (cfg.STRAT_C_RSI_OVERSOLD - rsi) / cfg.STRAT_C_RSI_OVERSOLD
        confidence = max(0.5, min(1.0, 0.5 + rsi_score))

        result.update({
            "signal"    : "BUY",
            "confidence": round(confidence, 2),
            "entry"     : round(price, 2),
            "stop"      : round(support * 0.98, 2),
            "target"    : round(resistance, 2),
            "reason"    : f"Mean reversion BUY | RSI={rsi:.1f} near support {support:.2f}",
        })
    else:
        result["reason"] = f"RSI={rsi:.1f} not oversold or price not near support"

    return result


# ── Run all strategies and pick the highest-confidence signal ────────────────

def scan(df: pd.DataFrame, regime: Regime, cfg) -> dict | None:
    """
    Run all 3 strategies and return the highest-confidence valid signal.
    """
    candidates = []
    for fn in (strategy_a, strategy_b, strategy_c):
        result = fn(df, regime, cfg)
        if result["signal"]:
            candidates.append(result)

    if not candidates:
        return None

    candidates.sort(key=lambda r: -r["confidence"])
    return candidates[0]
