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

    # 1. Volume filter — adapt threshold to data feed reality
    #    IEX feed shows ~3% of consolidated tape. If yfinance volume patching
    #    failed (Yahoo blocked the CI runner, etc.), our volume column is raw
    #    IEX which is ~33x lower than reality. Scale the threshold accordingly
    #    so we don't reject every liquid name on data-source nonsense.
    avg_vol      = last["vol_ma20"]
    is_patched   = bool(df.attrs.get("volume_patched", False))
    feed_is_iex  = getattr(cfg, "DATA_FEED", "iex").lower() == "iex"
    if feed_is_iex and not is_patched:
        # IEX ≈ 3% of tape, so a 1M-shares-real ticker shows ~30K on IEX
        effective_min = cfg.MIN_AVG_VOLUME / 33.0
    else:
        effective_min = cfg.MIN_AVG_VOLUME

    if pd.isna(avg_vol) or avg_vol < effective_min:
        return False, f"volume {avg_vol:,.0f} < {effective_min:,.0f}"

    # 2. ATR% filter
    atr_pct = last["atr_pct"]
    if pd.isna(atr_pct) or atr_pct < cfg.MIN_ATR_PCT:
        return False, f"ATR% {atr_pct:.2f} < {cfg.MIN_ATR_PCT}"

    return True, "OK"


def relative_strength(df: pd.DataFrame, benchmark_df: pd.DataFrame, period: int = 20) -> float:
    """
    RS = (1 + stock_return) / (1 + benchmark_return)
    > 1.0 → outperforming (strong RS)
    < 1.0 → underperforming
    Works correctly in any market direction:
      - SPY -2%, stock -1% → RS = 0.99/0.98 = 1.01  (correctly flagged outperforming)
      - SPY +5%, stock +8% → RS = 1.08/1.05 = 1.029 (outperforming)
      - SPY +5%, stock +2% → RS = 1.02/1.05 = 0.971 (underperforming)
    """
    if len(df) < period + 1 or len(benchmark_df) < period + 1:
        return 1.0

    stock_ret = df["close"].iloc[-1] / df["close"].iloc[-period] - 1
    bench_ret = benchmark_df["close"].iloc[-1] / benchmark_df["close"].iloc[-period] - 1

    return (1 + stock_ret) / (1 + bench_ret)
