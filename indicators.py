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
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ema   = ema(series, fast)
    slow_ema   = ema(series, slow)
    macd_line  = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram


def add_all(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """Attach all indicator columns to df in-place and return it."""
    df = df.copy()
    df["ema20"]  = ema(df["close"], cfg.EMA_FAST)
    df["sma50"]  = sma(df["close"], cfg.SMA_MID)
    df["sma200"] = sma(df["close"], cfg.SMA_SLOW)
    df["rsi"]    = rsi(df["close"], cfg.RSI_PERIOD)
    df["atr"]    = atr(df, cfg.ATR_PERIOD)
    df["atr_pct"] = df["atr"] / df["close"] * 100
    df["vol_ma20"] = sma(df["volume"], 20)
    df["macd"], df["macd_signal"], df["macd_hist"] = macd(df["close"])
    return df
