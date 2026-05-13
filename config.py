"""
Central configuration — watchlist, risk parameters, strategy thresholds.

PROFILE: AGGRESSIVE ($5k account)
  - Concentrated positions (max 3 concurrent, up to 30% each)
  - 2% account risk per trade
  - Dynamic targets scaled by conviction (2R / 3R / 4R)
  - Conviction-based filtering — only trade multi-factor confluence
  - Curated momentum watchlist (semis, AI, energy, metals, growth)
"""

# ── Curated AGGRESSIVE watchlist (high momentum, big movers) ──────────────────
# Focused on stocks that actually move 3-8% per setup. Removed slow names
# (utilities, REITs, defensive blue-chips). Added high-momentum themes.
WATCHLIST = [
    # AI / Semiconductors (the strongest secular trend)
    "NVDA", "AVGO", "AMD", "ARM", "MU", "SMCI", "MRVL",
    # Mega-cap growth
    "META", "GOOGL", "AMZN", "TSLA", "NFLX", "MSFT", "AAPL",
    # Software / Cloud high-beta
    "CRM", "NOW", "CRWD", "PLTR", "SHOP", "SNOW",
    # Energy momentum
    "XOM", "OXY", "FANG", "SLB",
    # Precious metals / commodities
    "NEM", "GOLD", "FCX", "GDX",
    # Speculative / crypto-adjacent (high volatility)
    "COIN", "MARA", "RIOT", "MSTR",
    # Index proxies for regime detection only
    "SPY", "QQQ",
]

# Market context ticker (benchmark for relative strength)
BENCHMARK = "SPY"

# Sector ETFs — fetched for big-money rotation analysis only (NOT traded).
# These tell us which sectors institutions are flowing into vs. out of, even
# though we trade individual momentum stocks not the ETFs themselves.
ROTATION_ETFS = [
    "XLF", "XLE", "XLV", "XLI", "XLB", "XLU", "XLRE",
    "GLD", "SLV", "USO", "UNG",
]

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

# ── Risk management (AGGRESSIVE for $5k) ─────────────────────────────────────
ACCOUNT_RISK_PCT          = 0.02    # 2% of account per trade (was 1%)
MAX_POSITION_PCT          = 0.30    # cap any single position at 30% (was 10%) — concentrated
MAX_CONCURRENT_POSITIONS  = 3       # max 3 positions at once (was 8) — focus
MAX_NEW_PER_DAY           = 2       # max 2 new per day (was 3) — quality not quantity
MAX_POSITIONS_PER_SECTOR  = 2       # max 2 per sector (was 3) — avoid theme concentration
MAX_PORTFOLIO_HEAT_PCT    = 0.06    # max 6% aggregate open risk (was 5%)
REQUIRE_SECTOR_ALIGNMENT  = False   # don't require — sometimes best setup is in a cold sector

# ── Conviction-based position sizing ─────────────────────────────────────────
# Multi-factor confluence required to fire. Each factor passed = 1 point.
# Only trade when score >= MIN_CONVICTION_TO_TRADE.
# Position size and target scale with conviction.
MIN_CONVICTION_TO_TRADE   = 4    # minimum factors needed (out of 7)
# Target R values are STRETCH targets — most trades exit earlier via trailing
# stop (position_manager.py). These act as a hard ceiling for parabolic moves.
CONVICTION_RISK_TIERS = {
    4: {"risk_mult": 1.0,  "target_R": 5.0},   # base — 2% risk, 5R stretch
    5: {"risk_mult": 1.0,  "target_R": 6.0},   # solid — 2% risk, 6R stretch
    6: {"risk_mult": 1.25, "target_R": 8.0},   # strong — 2.5% risk, 8R stretch
    7: {"risk_mult": 1.5,  "target_R": 10.0},  # exceptional — 3% risk, 10R stretch
}

# ── Data feed ────────────────────────────────────────────────────────────────
DATA_FEED = "iex"   # "iex" (free, ~3% of tape) or "sip" (paid, full tape)
# When IEX, volume-dependent strategies (Strategy B) are down-weighted —
# UNLESS we successfully patch volume from yfinance (full SIP tape, free).
USE_YFINANCE_VOLUME = True

# ── Earnings blackout ────────────────────────────────────────────────────────
# Aggressive: only block 2 days pre. Earnings *moves* are how we make money in
# CATALYST mode — pre-event blackout still avoids the worst gap risk.
EARNINGS_BLACKOUT_DAYS = 2   # was 5 — aggressive

# ── VIX-based volatility gate ────────────────────────────────────────────────
MAX_VIX             = 28.0    # block all new entries above this
VIX_HALVE_THRESHOLD = 22.0    # halve position size between this and MAX_VIX

# ── Pre-trade quality checks ─────────────────────────────────────────────────
MAX_BID_ASK_SPREAD_PCT     = 0.15   # skip if bid-ask spread > 0.15% of mid
INTRADAY_GAP_TOLERANCE_PCT = 1.0    # skip if today's price gapped >1% below signal

# ── Sentiment backend ────────────────────────────────────────────────────────
USE_FINBERT = True   # FinBERT (real model) when available; falls back to keyword

# ── Macro event blackout (CPI, FOMC, NFP, etc.) ──────────────────────────────
# Block new entries around scheduled macro releases that cause gap risk.
ENABLE_MACRO_BLACKOUT       = True
MACRO_BLACKOUT_HOURS_BEFORE = 30    # 30h covers both 8:30 AM (CPI/NFP) and 2 PM (FOMC) events from previous day's morning scan
MACRO_POST_EVENT_BUFFER_MIN = 60    # block first 60 min after market open on event day

# ── Catalyst trade mode (event-driven, gap-and-go continuation) ──────────────
# Triggers only when a real catalyst exists. Buys at 11 AM ET on stocks that
# gapped up overnight and held the gap through the first 90 min of trading.
# Held 1-2 days max, then force-closed.
CATALYST_MIN_GAP_PCT             = 3.0    # min gap up % (below = not a real catalyst)
CATALYST_MAX_GAP_PCT             = 12.0   # max gap up % (above = over-extended, fade risk)
CATALYST_MIN_NEWS_SCORE          = 2      # need sent_score >= +2
CATALYST_EARNINGS_LOOKBACK_DAYS  = 2      # earnings within last N days counts as catalyst
CATALYST_VOLUME_MULT             = 2.0    # vol pace today must be N× normal pace by 11 AM
CATALYST_MIN_FACTORS             = 3      # need this many factors to fire
CATALYST_STOP_PCT                = 1.8    # tight stop (% below entry)
CATALYST_TARGET_PCT              = 4.5    # target % above entry
CATALYST_SIZE_FACTOR             = 0.5    # half of normal swing risk
CATALYST_MAX_POSITION_PCT        = 0.05   # cap any catalyst position at 5% of portfolio
CATALYST_MAX_NEW_PER_DAY         = 2      # max new catalyst trades per day
CATALYST_FORCE_EXIT_DAYS         = 2      # force-close catalyst positions after N trading days
CATALYST_ORDER_PREFIX            = "CAT"  # client_order_id prefix to identify catalyst trades

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
