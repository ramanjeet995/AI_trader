"""
Volume / order-flow signals for swing trading confirmation.

Three checks:
  1. VWAP position  — price vs the rolling 20-day VWAP (computed from OHLCV)
  2. Volume surge   — today's volume vs PRIOR 20-day average (excluding today)
  3. CMF (Chaikin Money Flow) — accumulation vs distribution

Each returns: +1 (bullish), 0 (neutral), -1 (bearish)
Combined score: -3 to +3
"""

import pandas as pd


def vwap_signal(df: pd.DataFrame) -> tuple[int, str]:
    """
    Compares price to the rolling 20-day VWAP (always available from OHLCV).
    Above VWAP = buyers control the recent tape (bullish).
    """
    last = df.iloc[-1]
    vwap = last.get("rolling_vwap")

    if vwap is None or pd.isna(vwap):
        return 0, "VWAP: not enough data"

    price = last["close"]
    if price > vwap * 1.005:
        return 1, f"VWAP: price {price:.2f} above 20d VWAP {vwap:.2f} (bullish)"
    elif price < vwap * 0.995:
        return -1, f"VWAP: price {price:.2f} below 20d VWAP {vwap:.2f} (bearish)"
    return 0, f"VWAP: price {price:.2f} at 20d VWAP {vwap:.2f} (neutral)"


def volume_surge(df: pd.DataFrame, multiplier: float = 1.3) -> tuple[int, str]:
    """
    Today's volume vs PRIOR 20-day average (excluding today, so today's surge
    isn't deflating its own denominator).
    Surge on up-day = accumulation; surge on down-day = distribution.
    """
    if len(df) < 21:
        return 0, "Volume: not enough bars"

    last        = df.iloc[-1]
    vol         = last["volume"]
    vol_avg_pre = df["volume"].iloc[-21:-1].mean()

    if vol_avg_pre <= 0:
        return 0, "Volume: zero baseline"

    ratio  = vol / vol_avg_pre
    up_day = last["close"] > last["open"]

    if ratio >= multiplier and up_day:
        return 1, f"Volume: {ratio:.1f}x prior avg on UP day (accumulation)"
    elif ratio >= multiplier and not up_day:
        return -1, f"Volume: {ratio:.1f}x prior avg on DOWN day (distribution)"
    return 0, f"Volume: {ratio:.1f}x prior avg (no surge)"


def chaikin_money_flow(df: pd.DataFrame, period: int = 20) -> tuple[int, str]:
    """CMF — buying/selling pressure aggregated over `period` bars."""
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    vol   = df["volume"]

    denom = (high - low).replace(0, float("nan"))
    mfm   = ((close - low) - (high - close)) / denom
    mfv   = mfm * vol
    cmf   = mfv.rolling(period).sum() / vol.rolling(period).sum()

    val = cmf.iloc[-1]
    if pd.isna(val):
        return 0, "CMF: not enough data"

    if val > 0.05:
        return 1, f"CMF: {val:.3f} (accumulation)"
    elif val < -0.05:
        return -1, f"CMF: {val:.3f} (distribution)"
    return 0, f"CMF: {val:.3f} (neutral)"


def order_flow_score(df: pd.DataFrame) -> tuple[int, list[str]]:
    """Combined score (-3 to +3) and detail lines. BUY requires score >= 0."""
    v_score,  v_note  = vwap_signal(df)
    vs_score, vs_note = volume_surge(df)
    c_score,  c_note  = chaikin_money_flow(df)
    return v_score + vs_score + c_score, [v_note, vs_note, c_note]
