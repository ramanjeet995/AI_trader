"""
Catalyst event detector + sizing for event-driven gap-and-go trades.

Trade thesis: 80% of the initial pop on earnings/news gaps happens in the
first 30 min after open. By 11 AM, the stock has either (a) held the gap →
continuation likely (PEAD), or (b) faded back → "gap and crap." We only buy
(a).

  - detect():       multi-factor scoring on intraday data
  - build_signal(): tight % stops, smaller position size than swing
"""

import math
from datetime import datetime, timedelta, time as dtime

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

import pandas as pd

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit


ET = ZoneInfo("America/New_York")


# ─── Sizing for catalyst trades ───────────────────────────────────────────────

def build_signal(detection: dict, account_value: float, cfg) -> dict:
    """
    Build a sized trade from a fired catalyst detection.
    Tighter % stops (not ATR), smaller positions, fixed % targets — gap stocks
    are wilder than swing setups.
    """
    entry        = detection["current"]
    pct_stop     = entry * (1 - cfg.CATALYST_STOP_PCT / 100)
    day_low_stop = detection["day_low"] * 0.995   # 0.5% below today's low
    stop         = max(pct_stop, day_low_stop)
    target       = entry * (1 + cfg.CATALYST_TARGET_PCT / 100)

    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return {"valid": False, "reason": "bad risk (stop >= entry)"}

    risk_dollars  = account_value * cfg.ACCOUNT_RISK_PCT * cfg.CATALYST_SIZE_FACTOR
    shares        = math.floor(risk_dollars / risk_per_share)
    max_notional  = account_value * cfg.CATALYST_MAX_POSITION_PCT
    if shares * entry > max_notional:
        shares = math.floor(max_notional / entry)
    if shares <= 0:
        return {"valid": False, "reason": "0 shares after sizing"}

    return {
        "valid"       : True,
        "entry"       : round(entry, 2),
        "stop"        : round(stop, 2),
        "target"      : round(target, 2),
        "shares"      : shares,
        "notional"    : round(shares * entry, 2),
        "risk_dollars": round(shares * risk_per_share, 2),
        "target_risk" : round(risk_dollars, 2),
    }


# ─── Intraday data fetch ──────────────────────────────────────────────────────


def fetch_today_intraday(symbol: str, data_client) -> pd.DataFrame | None:
    """Fetch today's 15-min bars from market open through current time."""
    try:
        # Use today's ET date, market open at 9:30 AM ET
        now_et   = datetime.now(ET)
        open_et  = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        if now_et < open_et:
            return None
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=open_et.astimezone(ZoneInfo("UTC")),
            end=now_et.astimezone(ZoneInfo("UTC")),
            feed="iex",
        )
        bars = data_client.get_stock_bars(req).df
        if bars is None or bars.empty:
            return None
        if isinstance(bars.index, pd.MultiIndex):
            return bars.loc[symbol].copy()
        return bars.copy()
    except Exception:
        return None


def detect(
    symbol: str,
    daily_df: pd.DataFrame,        # daily bars with indicators (last row = today's daily so far is incomplete; -1 = yesterday)
    intraday_df: pd.DataFrame,     # today's 15-min bars from open through now
    news_score: int,
    earnings_days_ago: int | None, # days since last earnings (None if unknown)
    cfg,
) -> dict:
    """
    Returns a catalyst assessment dict:
      {fires: bool, score: int, factors: list[str], gap_pct, current, day_high,
       day_low, day_avg_volume_now, reasons_against: list}
    """
    result = {"fires": False, "score": 0, "factors": [], "reasons_against": [],
              "gap_pct": 0.0, "current": 0.0, "day_high": 0.0, "day_low": 0.0,
              "vol_pace_x": 0.0}

    # Need enough data
    if intraday_df is None or len(intraday_df) < 1 or len(daily_df) < 21:
        result["reasons_against"].append("insufficient intraday/daily data")
        return result

    # Find yesterday's close defensively — use the most recent daily bar that
    # is NOT today's date (in ET), to avoid mis-identifying the partial-today
    # bar (which may or may not exist in Alpaca's response).
    today_et = datetime.now(ET).date()
    yesterday_close = None
    for i in range(len(daily_df) - 1, -1, -1):
        try:
            bar_date = pd.to_datetime(daily_df.index[i]).date()
            if bar_date < today_et:
                yesterday_close = float(daily_df["close"].iloc[i])
                break
        except Exception:
            continue
    if yesterday_close is None:
        # Fallback: assume index[-1] is the prior session
        yesterday_close = float(daily_df["close"].iloc[-1])

    current = float(intraday_df["close"].iloc[-1])
    day_high        = float(intraday_df["high"].max())
    day_low         = float(intraday_df["low"].min())
    gap_pct         = (current - yesterday_close) / yesterday_close * 100

    # Today's volume so far vs normal full-day average — scaled to where we are in the session
    bars_today        = len(intraday_df)
    bars_full_session = 26   # 6.5 hours × 4 fifteen-min bars
    expected_pace     = bars_today / bars_full_session
    today_vol_sofar   = float(intraday_df["volume"].sum())
    avg_daily_vol     = float(daily_df["volume"].iloc[-21:-1].mean())  # 20d avg, excluding today
    vol_pace_x        = (today_vol_sofar / max(avg_daily_vol * expected_pace, 1))

    result.update({"gap_pct": round(gap_pct, 2), "current": round(current, 2),
                   "day_high": round(day_high, 2), "day_low": round(day_low, 2),
                   "vol_pace_x": round(vol_pace_x, 2)})

    factors = []
    against = []

    # Factor 1: gap size in sweet spot
    if cfg.CATALYST_MIN_GAP_PCT <= gap_pct <= cfg.CATALYST_MAX_GAP_PCT:
        factors.append(f"gap {gap_pct:+.1f}% in [{cfg.CATALYST_MIN_GAP_PCT}, {cfg.CATALYST_MAX_GAP_PCT}]")
    elif gap_pct < cfg.CATALYST_MIN_GAP_PCT:
        against.append(f"gap {gap_pct:+.1f}% < {cfg.CATALYST_MIN_GAP_PCT}% (too small)")
    else:
        against.append(f"gap {gap_pct:+.1f}% > {cfg.CATALYST_MAX_GAP_PCT}% (over-extended)")

    # Factor 2: earnings recency
    if earnings_days_ago is not None and 0 <= earnings_days_ago <= cfg.CATALYST_EARNINGS_LOOKBACK_DAYS:
        factors.append(f"earnings {earnings_days_ago}d ago")

    # Factor 3: news sentiment
    if news_score >= cfg.CATALYST_MIN_NEWS_SCORE:
        factors.append(f"news score +{news_score}")

    # Factor 4: volume confirmation (institutional interest)
    if vol_pace_x >= cfg.CATALYST_VOLUME_MULT:
        factors.append(f"volume pace {vol_pace_x:.1f}x normal")
    else:
        against.append(f"weak volume pace ({vol_pace_x:.1f}x < {cfg.CATALYST_VOLUME_MULT}x)")

    # Factor 5: holding the gap — current price in upper half of day's range
    day_range = day_high - day_low
    if day_range > 0:
        position_in_range = (current - day_low) / day_range
        if position_in_range >= 0.5:
            factors.append(f"in upper {(position_in_range*100):.0f}% of day's range")
        else:
            against.append(f"fading — price in lower {(position_in_range*100):.0f}% of range")

    # Need at least N factors to fire AND the gap-size factor must be satisfied
    has_gap_factor = any("gap" in f and "in [" in f for f in factors)
    score          = len(factors)
    fires          = (score >= cfg.CATALYST_MIN_FACTORS) and has_gap_factor

    result.update({"fires": fires, "score": score, "factors": factors,
                   "reasons_against": against})
    return result
