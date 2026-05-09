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
ACCOUNT_RISK_PCT          = 0.01    # 1% of account per trade
MAX_POSITION_PCT          = 0.10    # cap any single position at 10% of portfolio
MAX_CONCURRENT_POSITIONS  = 8       # never hold more than 8 positions at once
MAX_NEW_PER_DAY           = 3       # never open more than 3 new positions per day
MAX_POSITIONS_PER_SECTOR  = 3       # cap correlated exposure (tech-heavy watchlist)
MAX_PORTFOLIO_HEAT_PCT    = 0.05    # cap aggregate open risk at 5% of account
REQUIRE_SECTOR_ALIGNMENT  = True    # only buy stocks whose sector has positive rotation score

# ── Data feed ────────────────────────────────────────────────────────────────
DATA_FEED = "iex"   # "iex" (free, ~3% of tape) or "sip" (paid, full tape)
# When IEX, volume-dependent strategies (Strategy B) are down-weighted —
# UNLESS we successfully patch volume from yfinance (full SIP tape, free).
USE_YFINANCE_VOLUME = True

# ── Earnings blackout ────────────────────────────────────────────────────────
EARNINGS_BLACKOUT_DAYS = 5   # skip signals if earnings within N days

# ── VIX-based volatility gate ────────────────────────────────────────────────
MAX_VIX             = 28.0    # block all new entries above this
VIX_HALVE_THRESHOLD = 22.0    # halve position size between this and MAX_VIX

# ── Pre-trade quality checks ─────────────────────────────────────────────────
MAX_BID_ASK_SPREAD_PCT     = 0.15   # skip if bid-ask spread > 0.15% of mid
INTRADAY_GAP_TOLERANCE_PCT = 1.0    # skip if today's price gapped >1% below signal

# ── Sentiment backend ────────────────────────────────────────────────────────
USE_FINBERT = True   # FinBERT (real model) when available; falls back to keyword

# ── Per-symbol sector classification (used for per-sector position caps) ─────
TICKER_SECTOR = {
    # Tech
    "AAPL": "Tech", "MSFT": "Tech", "NVDA": "Tech", "AMD": "Tech",
    "AVGO": "Tech", "QCOM": "Tech", "INTC": "Tech", "TSM": "Tech",
    "GOOGL": "Tech", "META": "Tech", "AMZN": "Tech", "TSLA": "Tech",
    "CRM": "Tech", "ORCL": "Tech", "ADBE": "Tech", "NOW": "Tech",
    "XLK": "Tech", "QQQ": "Tech",
    # Financials
    "JPM": "Financials", "GS": "Financials", "BAC": "Financials",
    "V": "Financials", "MA": "Financials", "AXP": "Financials", "XLF": "Financials",
    # Healthcare
    "UNH": "Healthcare", "LLY": "Healthcare", "ABBV": "Healthcare",
    "JNJ": "Healthcare", "MRK": "Healthcare", "XLV": "Healthcare",
    # Consumer
    "COST": "Consumer", "HD": "Consumer", "NKE": "Consumer",
    "MCD": "Consumer", "SBUX": "Consumer",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "OXY": "Energy", "SLB": "Energy",
    "XLE": "Energy", "USO": "Energy", "UNG": "Energy", "DBO": "Energy",
    # Metals / Materials
    "FCX": "Materials", "CLF": "Materials", "AA": "Materials", "XLB": "Materials",
    "NEM": "Metals", "GOLD": "Metals", "GLD": "Metals", "SLV": "Metals",
    "GDX": "Metals", "GDXJ": "Metals",
    # Industrials / Utilities / Real Estate
    "XLI": "Industrials", "XLU": "Utilities", "XLRE": "Real Estate",
    # Broad index
    "SPY": "Index", "IWM": "Index", "DIA": "Index",
}

# ── Strategy A thresholds (Trend Pullback) ───────────────────────────────────
STRAT_A_RSI_LOW  = 40
STRAT_A_RSI_HIGH = 55

# ── Strategy B thresholds (Breakout) ────────────────────────────────────────
STRAT_B_CONSOLIDATION_BARS = 10   # bars of tight range before breakout
STRAT_B_VOLUME_MULT        = 1.5  # volume must be 1.5× its 20-day avg

# ── Strategy C thresholds (Mean Reversion) ──────────────────────────────────
STRAT_C_RSI_OVERSOLD  = 30
STRAT_C_RSI_OVERBOUGHT = 70
