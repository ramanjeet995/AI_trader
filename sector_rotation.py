"""
Sector Rotation Detector — tracks where big money is flowing.

Compares all sector ETFs by:
  1. Relative strength vs SPY (20-day return ratio)
  2. OBV trend (ACCUMULATION / DISTRIBUTION)
  3. Price vs 20 EMA (above = inflow, below = outflow)

Output: ranked list of sectors from hottest to coldest,
        plus an overall market risk posture (RISK-ON / RISK-OFF / MIXED).
"""

import pandas as pd
from indicators import obv_trend, obv

SECTOR_ETFS = {
    "XLK" : "Technology",
    "XLF" : "Financials",
    "XLE" : "Energy",
    "XLV" : "Healthcare",
    "XLI" : "Industrials",
    "XLB" : "Materials",
    "XLU" : "Utilities",
    "XLRE": "Real Estate",
    "GLD" : "Gold",
    "SLV" : "Silver",
    "USO" : "Crude Oil",
    "UNG" : "Natural Gas",
}

# Risk-on sectors: money flowing here = market is confident
RISK_ON_SECTORS  = {"XLK", "XLF", "XLI", "XLB"}
# Risk-off / defensive: money flowing here = market is fearful
RISK_OFF_SECTORS = {"XLU", "XLRE", "XLV", "GLD", "SLV"}


def analyze(all_bars: dict, benchmark_df: pd.DataFrame) -> dict:
    """
    all_bars: dict of symbol -> DataFrame (with indicators already applied)
    Returns a result dict with ranked sectors and risk posture.
    """
    results = []

    bench_ret_20 = (
        benchmark_df["close"].iloc[-1] / benchmark_df["close"].iloc[-20] - 1
        if len(benchmark_df) >= 20 else 0
    )

    for ticker, sector_name in SECTOR_ETFS.items():
        df = all_bars.get(ticker)
        if df is None or len(df) < 25:
            continue

        last       = df.iloc[-1]
        ret_20     = df["close"].iloc[-1] / df["close"].iloc[-20] - 1
        rs         = ret_20 / abs(bench_ret_20) if bench_ret_20 != 0 else 1.0
        above_ema  = last["close"] > last["ema20"]
        obv_status = obv_trend(df)

        # Score: RS + OBV + EMA position
        score = 0
        score += rs * 10
        score += 2 if obv_status in ("ACCUMULATION", "STEALTH_BUY") else (
                -2 if obv_status in ("DISTRIBUTION", "STEALTH_SELL") else 0)
        score += 1 if above_ema else -1

        results.append({
            "ticker"    : ticker,
            "sector"    : sector_name,
            "ret_20d"   : round(ret_20 * 100, 2),
            "rs"        : round(rs, 2),
            "obv"       : obv_status,
            "above_ema" : above_ema,
            "score"     : round(score, 2),
        })

    results.sort(key=lambda x: -x["score"])

    # Risk posture: compare total score of risk-on vs risk-off sectors
    risk_on_score  = sum(r["score"] for r in results if r["ticker"] in RISK_ON_SECTORS)
    risk_off_score = sum(r["score"] for r in results if r["ticker"] in RISK_OFF_SECTORS)

    if risk_on_score > risk_off_score + 3:
        posture = "RISK-ON"    # money in tech/finance/industrials — bullish
    elif risk_off_score > risk_on_score + 3:
        posture = "RISK-OFF"   # money in gold/utilities/healthcare — fearful
    else:
        posture = "MIXED"

    return {"sectors": results, "posture": posture}


def print_rotation(rotation: dict):
    posture = rotation["posture"]
    icon    = "+" if posture == "RISK-ON" else ("-" if posture == "RISK-OFF" else "~")
    print(f"  Market posture : [{icon}] {posture}\n")
    print(f"  {'Sector':<18} {'Ticker':<6} {'20d Ret':>8} {'RS':>6} {'OBV':<18} {'EMA'}")
    print(f"  {'-'*70}")
    for r in rotation["sectors"]:
        ema_str = "above" if r["above_ema"] else "below"
        print(f"  {r['sector']:<18} {r['ticker']:<6} {r['ret_20d']:>7.1f}% {r['rs']:>6.2f} {r['obv']:<18} {ema_str}")
    print()
