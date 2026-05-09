"""
Pure-Python indicator calculations on a pandas DataFrame of OHLCV bars.
Expected columns: open, high, low, close, volume
"""

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder's RSI — exponential smoothing with alpha = 1/period.
    Matches the values shown by TradingView, Yahoo Finance, and most charting tools.
    """
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)

    # Wilder's smoothing = EMA with alpha = 1/period (i.e., span = 2*period - 1)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    # Wilder's smoothing for ATR too
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ema    = ema(series, fast)
    slow_ema    = ema(series, slow)
    macd_line   = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram


def obv(df: pd.DataFrame) -> pd.Series:
    """
    On-Balance Volume — cumulative institutional pressure.
    Up day → add volume; down day → subtract; flat → 0.
    """
    direction = df["close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    return (direction * df["volume"]).cumsum()


def obv_trend(df: pd.DataFrame, period: int = 20) -> str:
    """ACCUMULATION / DISTRIBUTION / STEALTH_BUY / STEALTH_SELL / NEUTRAL"""
    o = obv(df)
    if len(o) < period:
        return "NEUTRAL"
    obv_slope   = o.iloc[-1] - o.iloc[-period]
    price_slope = df["close"].iloc[-1] - df["close"].iloc[-period]

    if obv_slope > 0 and price_slope > 0:
        return "ACCUMULATION"
    if obv_slope > 0 and price_slope <= 0:
        return "STEALTH_BUY"
    if obv_slope < 0 and price_slope < 0:
        return "DISTRIBUTION"
    if obv_slope < 0 and price_slope >= 0:
        return "STEALTH_SELL"
    return "NEUTRAL"


def rolling_vwap(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """
    Volume-weighted average price over a rolling window of N bars.
    Computed from OHLCV (always available) — does not depend on Alpaca's
    per-bar `vwap` field which is unreliable on the IEX feed.

    Typical price = (high + low + close) / 3
    VWAP = sum(typical_price * volume) / sum(volume) over `period` bars
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv      = typical * df["volume"]
    return pv.rolling(period).sum() / df["volume"].rolling(period).sum()


def add_all(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """Attach all indicator columns to df and return it."""
    df = df.copy()
    df["ema20"]       = ema(df["close"], cfg.EMA_FAST)
    df["sma50"]       = sma(df["close"], cfg.SMA_MID)
    df["sma200"]      = sma(df["close"], cfg.SMA_SLOW)
    df["rsi"]         = rsi(df["close"], cfg.RSI_PERIOD)
    df["atr"]         = atr(df, cfg.ATR_PERIOD)
    df["atr_pct"]     = df["atr"] / df["close"] * 100
    df["vol_ma20"]    = sma(df["volume"], 20)
    df["macd"], df["macd_signal"], df["macd_hist"] = macd(df["close"])
    df["obv"]         = obv(df)
    df["rolling_vwap"] = rolling_vwap(df, period=20)
    return df
