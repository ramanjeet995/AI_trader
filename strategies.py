"""
Strategy signal generators — A, B, C as defined in the strategy doc.

Each function returns a dict with:
  signal   : "BUY" | "SHORT" | None
  strategy : "A" | "B" | "C"
  entry    : suggested entry price
  stop     : stop-loss price
  target   : profit target price
  reason   : human-readable explanation
"""

import pandas as pd
from market_structure import Regime


# ── Strategy A: Trend Pullback ───────────────────────────────────────────────

def strategy_a(df: pd.DataFrame, regime: Regime, cfg) -> dict:
    """
    BUY when:
      - Regime is BULL
      - Price pulled back to between 20 EMA and 50 SMA
      - RSI between 40–55 (healthy pullback, not broken)
      - Last candle is bullish (close > open)
    """
    result = {"signal": None, "strategy": "A", "entry": None,
              "stop": None, "target": None, "reason": ""}

    if regime != Regime.BULL:
        result["reason"] = f"regime={regime.value}, need BULL"
        return result

    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    price = last["close"]
    ema20 = last["ema20"]
    sma50 = last["sma50"]
    rsi   = last["rsi"]

    # Price in pullback zone
    in_pullback = sma50 <= price <= ema20 * 1.02

    # Bullish confirmation candle
    bullish_candle = last["close"] > last["open"]

    # RSI in healthy zone
    rsi_ok = cfg.STRAT_A_RSI_LOW <= rsi <= cfg.STRAT_A_RSI_HIGH

    if in_pullback and bullish_candle and rsi_ok:
        recent_low  = df["low"].iloc[-10:].min()
        recent_high = df["high"].iloc[-20:].max()
        result.update({
            "signal"  : "BUY",
            "entry"   : round(price, 2),
            "stop"    : round(recent_low * 0.99, 2),      # just below pullback low
            "target"  : round(recent_high, 2),            # prior resistance
            "reason"  : f"Pullback to EMA20/SMA50 | RSI={rsi:.1f} | bullish candle",
        })
    else:
        reasons = []
        if not in_pullback:   reasons.append(f"price {price:.2f} not in [{sma50:.2f}, {ema20:.2f}]")
        if not bullish_candle: reasons.append("no bullish candle")
        if not rsi_ok:         reasons.append(f"RSI={rsi:.1f} outside [{cfg.STRAT_A_RSI_LOW},{cfg.STRAT_A_RSI_HIGH}]")
        result["reason"] = " | ".join(reasons)

    return result


# ── Strategy B: Breakout ─────────────────────────────────────────────────────

def strategy_b(df: pd.DataFrame, regime: Regime, cfg) -> dict:
    """
    BUY when:
      - Regime is BULL (or neutral — breakouts can start new trends)
      - Recent consolidation: ATR compressed vs longer avg
      - Price breaks above the consolidation high
      - Volume on breakout bar >= 1.5× 20-day avg volume
    """
    result = {"signal": None, "strategy": "B", "entry": None,
              "stop": None, "target": None, "reason": ""}

    if regime == Regime.BEAR:
        result["reason"] = "regime=BEAR, skipping breakout"
        return result

    last  = df.iloc[-1]
    n     = cfg.STRAT_B_CONSOLIDATION_BARS

    if len(df) < n + 5:
        result["reason"] = "not enough bars"
        return result

    consol_window   = df.iloc[-(n + 1):-1]
    consol_high     = consol_window["high"].max()
    consol_low      = consol_window["low"].min()
    consol_range    = consol_high - consol_low

    # Tight consolidation: range < 1.5× ATR
    atr_val  = last["atr"]
    tight    = consol_range < atr_val * 1.5

    # Breakout above consolidation high
    breakout = last["close"] > consol_high

    # Volume confirmation
    vol_spike = last["volume"] >= last["vol_ma20"] * cfg.STRAT_B_VOLUME_MULT

    if tight and breakout and vol_spike:
        measured_move = consol_high + consol_range   # height projected upward
        result.update({
            "signal"  : "BUY",
            "entry"   : round(last["close"], 2),
            "stop"    : round(consol_high * 0.99, 2),    # just below breakout level
            "target"  : round(measured_move, 2),
            "reason"  : (f"Breakout above {consol_high:.2f} | "
                         f"vol {last['volume']:,.0f} vs avg {last['vol_ma20']:,.0f}"),
        })
    else:
        reasons = []
        if not tight:    reasons.append(f"range {consol_range:.2f} not tight vs ATR {atr_val:.2f}")
        if not breakout: reasons.append(f"no breakout above {consol_high:.2f}")
        if not vol_spike: reasons.append(f"vol {last['volume']:,.0f} < {cfg.STRAT_B_VOLUME_MULT}× avg")
        result["reason"] = " | ".join(reasons)

    return result


# ── Strategy C: Mean Reversion ────────────────────────────────────────────────

def strategy_c(df: pd.DataFrame, regime: Regime, cfg) -> dict:
    """
    BUY  when RSI < 30 near support (range or choppy market)
    SHORT when RSI > 70 near resistance
    Works best on ETFs and large caps in CHOPPY regime.
    """
    result = {"signal": None, "strategy": "C", "entry": None,
              "stop": None, "target": None, "reason": ""}

    last  = df.iloc[-1]
    price = last["close"]
    rsi   = last["rsi"]

    support    = df["low"].iloc[-30:].min()
    resistance = df["high"].iloc[-30:].max()
    near_support    = price <= support * 1.03
    near_resistance = price >= resistance * 0.97

    # BUY only — no short selling
    if rsi < cfg.STRAT_C_RSI_OVERSOLD and near_support:
        result.update({
            "signal"  : "BUY",
            "entry"   : round(price, 2),
            "stop"    : round(support * 0.98, 2),
            "target"  : round(resistance, 2),
            "reason"  : f"Mean reversion BUY | RSI={rsi:.1f} near support {support:.2f}",
        })
    else:
        result["reason"] = f"RSI={rsi:.1f} not oversold or price not near support"

    return result


# ── Run all strategies and return the best signal ────────────────────────────

def scan(df: pd.DataFrame, regime: Regime, cfg) -> dict | None:
    """
    Run A → B → C in priority order. Return first valid signal, or None.
    """
    for fn in (strategy_a, strategy_b, strategy_c):
        result = fn(df, regime, cfg)
        if result["signal"]:
            return result
    return None
