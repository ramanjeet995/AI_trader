"""
Central configuration — watchlist, risk parameters, strategy thresholds.
"""

# ── Hardcoded watchlist (liquid, mid/large cap) ────────────────────────────
WATCHLIST = [
    # Tech / Semiconductors
    "AAPL", "MSFT", "NVDA", "AMD", "AVGO", "QCOM", "INTC", "TSM",
    "GOOGL", "META", "AMZN", "TSLA", "CRM", "ORCL", "ADBE", "NOW",
    # Finance
    "JPM", "GS", "BAC", "V", "MA", "AXP",
    # Healthcare
    "UNH", "LLY", "ABBV", "JNJ", "MRK",
    # Consumer / Retail
    "COST", "HD", "NKE", "MCD", "SBUX",
    # Energy (stocks)
    "XOM", "CVX", "OXY", "SLB",
    # Broad market ETFs
    "SPY", "QQQ", "IWM", "DIA",
    # Sector ETFs
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLB", "XLU", "XLRE",
    # Precious metals ETFs
    "GLD",   # Gold
    "SLV",   # Silver
    "GDX",   # Gold miners
    "GDXJ",  # Junior gold miners
    # Commodities / Energy ETFs
    "USO",   # Crude oil
    "UNG",   # Natural gas
    "DBO",   # Oil futures-based
    # Commodity-linked stocks
    "FCX",   # Copper / metals (Freeport-McMoRan)
    "NEM",   # Newmont (gold miner)
    "GOLD",  # Barrick Gold
    "CLF",   # Cleveland-Cliffs (steel)
    "AA",    # Alcoa (aluminum)
]

# Market context ticker (benchmark for relative strength)
BENCHMARK = "SPY"

# ── Data settings ───────────────────────────────────────────────────────────
LOOKBACK_DAYS = 200   # calendar days of daily bars to fetch

# ── Indicator periods ───────────────────────────────────────────────────────
EMA_FAST   = 20
SMA_MID    = 50
SMA_SLOW   = 200
RSI_PERIOD = 14
ATR_PERIOD = 14

# ── Stock filters ────────────────────────────────────────────────────────────
MIN_AVG_VOLUME   = 1_000_000   # shares/day
MIN_ATR_PCT      = 2.0         # ATR as % of price (movement needed)

# ── Risk management ──────────────────────────────────────────────────────────
ACCOUNT_RISK_PCT  = 0.01       # 1% of account per trade
MAX_POSITION_PCT  = 0.10       # cap any single position at 10% of portfolio

# ── Strategy A thresholds (Trend Pullback) ───────────────────────────────────
STRAT_A_RSI_LOW  = 40
STRAT_A_RSI_HIGH = 55

# ── Strategy B thresholds (Breakout) ────────────────────────────────────────
STRAT_B_CONSOLIDATION_BARS = 10   # bars of tight range before breakout
STRAT_B_VOLUME_MULT        = 1.5  # volume must be 1.5× its 20-day avg

# ── Strategy C thresholds (Mean Reversion) ──────────────────────────────────
STRAT_C_RSI_OVERSOLD  = 30
STRAT_C_RSI_OVERBOUGHT = 70
