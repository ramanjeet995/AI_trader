"""
Classify the market (or any stock) as BULL, BEAR, or CHOPPY.

Rules (from the strategy doc):
  BULL  → price > 50 SMA  AND  higher-highs + higher-lows over last N swings
  BEAR  → price < 50 SMA  AND  lower-highs  + lower-lows  over last N swings
  CHOPPY → everything else (range-bound)
"""

from enum import Enum
import pandas as pd


class Regime(str, Enum):
    BULL   = "BULL"
    BEAR   = "BEAR"
    CHOPPY = "CHOPPY"


def _swing_structure(close: pd.Series, window: int = 10) -> str:
    """
    Detect HH/HL (bull) or LH/LL (bear) using rolling highs and lows
    over the last 3 non-overlapping windows.
    """
    pivots = []
    for i in range(3):
        end   = len(close) - i * window
        start = end - window
        if start < 0:
            break
        segment = close.iloc[start:end]
        pivots.append((segment.max(), segment.min()))

    if len(pivots) < 2:
        return "CHOPPY"

    # pivots[0] = most recent, pivots[-1] = oldest
    highs = [p[0] for p in pivots]
    lows  = [p[1] for p in pivots]

    higher_highs = all(highs[i] > highs[i + 1] for i in range(len(highs) - 1))
    higher_lows  = all(lows[i]  > lows[i + 1]  for i in range(len(lows)  - 1))
    lower_highs  = all(highs[i] < highs[i + 1] for i in range(len(highs) - 1))
    lower_lows   = all(lows[i]  < lows[i + 1]  for i in range(len(lows)  - 1))

    if higher_highs and higher_lows:
        return "BULL"
    if lower_highs and lower_lows:
        return "BEAR"
    return "CHOPPY"


def classify(df: pd.DataFrame) -> Regime:
    """
    df must already have indicator columns (add_all applied).
    Uses the last row for price-vs-SMA and recent bars for swing structure.
    """
    last     = df.iloc[-1]
    price    = last["close"]
    sma50    = last["sma50"]
    structure = _swing_structure(df["close"])

    if price > sma50 and structure == "BULL":
        return Regime.BULL
    if price < sma50 and structure == "BEAR":
        return Regime.BEAR
    return Regime.CHOPPY
