"""
Stock filters — only pass stocks that meet liquidity and movement criteria.

Filters applied:
  1. Average daily volume > MIN_AVG_VOLUME
  2. ATR% > MIN_ATR_PCT  (stock moves enough to be worth trading)
  3. Relative strength vs benchmark (close % change over 20 days > benchmark)
"""

import pandas as pd


def passes_filters(df: pd.DataFrame, cfg) -> tuple[bool, str]:
    """
    Returns (passed: bool, reason: str).
    df must have indicator columns already attached.
    """
    last = df.iloc[-1]

    # 1. Volume filter
    avg_vol = last["vol_ma20"]
    if pd.isna(avg_vol) or avg_vol < cfg.MIN_AVG_VOLUME:
        return False, f"volume {avg_vol:,.0f} < {cfg.MIN_AVG_VOLUME:,}"

    # 2. ATR% filter
    atr_pct = last["atr_pct"]
    if pd.isna(atr_pct) or atr_pct < cfg.MIN_ATR_PCT:
        return False, f"ATR% {atr_pct:.2f} < {cfg.MIN_ATR_PCT}"

    return True, "OK"


def relative_strength(df: pd.DataFrame, benchmark_df: pd.DataFrame, period: int = 20) -> float:
    """
    RS = stock's % return over `period` bars divided by benchmark's % return.
    > 1.0  → outperforming (strong RS)
    < 1.0  → underperforming
    """
    if len(df) < period + 1 or len(benchmark_df) < period + 1:
        return 1.0

    stock_ret = df["close"].iloc[-1] / df["close"].iloc[-period] - 1
    bench_ret = benchmark_df["close"].iloc[-1] / benchmark_df["close"].iloc[-period] - 1

    if bench_ret == 0:
        return 1.0
    return stock_ret / abs(bench_ret)
