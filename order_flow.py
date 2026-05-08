"""
Volume / order-flow signals for swing trading confirmation.

Three checks:
  1. VWAP position  — is price above or below the session VWAP?
  2. Volume surge   — is today's volume elevated vs 20-day average?
  3. CMF (Chaikin Money Flow) — is smart money accumulating or distributing?

Each returns a score: +1 (bullish), 0 (neutral), -1 (bearish)
Combined score range: -3 to +3
"""

import pandas as pd


def vwap_signal(df: pd.DataFrame) -> tuple[int, str]:
    """
    Alpaca daily bars include a 'vwap' column.
    Price above VWAP = buyers in control = bullish.
    """
    last = df.iloc[-1]

    if "vwap" not in df.columns or pd.isna(last.get("vwap")):
        return 0, "VWAP: no data"

    price = last["close"]
    vwap  = last["vwap"]

    if price > vwap * 1.005:
        return 1, f"VWAP: price {price:.2f} above {vwap:.2f} (bullish)"
    elif price < vwap * 0.995:
        return -1, f"VWAP: price {price:.2f} below {vwap:.2f} (bearish)"
    return 0, f"VWAP: price {price:.2f} at {vwap:.2f} (neutral)"


def volume_surge(df: pd.DataFrame, multiplier: float = 1.3) -> tuple[int, str]:
    """
    Today's volume vs 20-day average.
    Surge on an up-day = accumulation (bullish).
    Surge on a down-day = distribution (bearish).
    """
    last     = df.iloc[-1]
    vol      = last["volume"]
    vol_avg  = last.get("vol_ma20", df["volume"].rolling(20).mean().iloc[-1])

    if pd.isna(vol_avg) or vol_avg == 0:
        return 0, "Volume: no avg data"

    ratio    = vol / vol_avg
    up_day   = last["close"] >= last["open"]

    if ratio >= multiplier and up_day:
        return 1, f"Volume: {ratio:.1f}x avg on UP day (accumulation)"
    elif ratio >= multiplier and not up_day:
        return -1, f"Volume: {ratio:.1f}x avg on DOWN day (distribution)"
    return 0, f"Volume: {ratio:.1f}x avg (no surge)"


def chaikin_money_flow(df: pd.DataFrame, period: int = 20) -> tuple[int, str]:
    """
    CMF measures buying/selling pressure over N bars.
    Positive CMF = money flowing in = bullish.
    Negative CMF = money flowing out = bearish.
    """
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    vol   = df["volume"]

    denom = (high - low).replace(0, float("nan"))
    mfm   = ((close - low) - (high - close)) / denom   # money flow multiplier
    mfv   = mfm * vol                                    # money flow volume
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
    """
    Run all three checks and return combined score + detail lines.
    Score: -3 (very bearish) to +3 (very bullish).
    For a BUY trade, require score >= 0.
    """
    v_score, v_note   = vwap_signal(df)
    vs_score, vs_note = volume_surge(df)
    c_score, c_note   = chaikin_money_flow(df)

    total = v_score + vs_score + c_score
    notes = [v_note, vs_note, c_note]
    return total, notes
