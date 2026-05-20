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
    # AI infrastructure / data center / cooling
    "DELL", "HPE", "VRT", "CLS", "CEG",
    # Mega-cap growth
    "META", "GOOGL", "AMZN", "TSLA", "NFLX", "MSFT", "AAPL",
    # Software / Cloud / Networking / Voice AI high-beta
    "CRM", "NOW", "CRWD", "PLTR", "SHOP", "SNOW", "CSCO", "PANW", "ANET", "SOUN",
    # Energy momentum
    "XOM", "OXY", "FANG", "SLB",
    # Precious metals / commodities
    "NEM", "GOLD", "FCX", "GDX",
    # Space / SpaceX supply chain (IPO hype play — June 12, 2026)
    "RDW", "RKLB", "ASTS", "LUNR", "PL",
    # Quantum computing (extreme volatility, 10%+ daily swings)
    "IONQ", "RGTI", "QBTS",
    # Defense / drones
    "KTOS",
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
ACCOUNT_RISK_PCT          = 0.02    # 2% of account per trade
MAX_POSITION_PCT          = 0.40    # cap any single position at 40% — allow bigger bets
MAX_CONCURRENT_POSITIONS  = 3       # max 3 positions at once — focus on quality
MAX_NEW_PER_DAY           = 2       # max 2 new per day — quality not quantity
MAX_POSITIONS_PER_SECTOR  = 2       # max 2 per sector (was 3) — avoid theme concentration
MAX_PORTFOLIO_HEAT_PCT    = 0.08    # max 8% aggregate open risk — let winners ride
REQUIRE_SECTOR_ALIGNMENT  = False   # don't require — sometimes best setup is in a cold sector
REPLACE_WORST_LOSER       = True    # when at max positions, close worst loser for better setup

# ── Conviction-based position sizing ─────────────────────────────────────────
# Multi-factor confluence required to fire. Each factor passed = 1 point.
# Only trade when score >= MIN_CONVICTION_TO_TRADE.
# Position size and target scale with conviction.
MIN_CONVICTION_TO_TRADE   = 3    # was 4 — lowered, since backtest with sentiment=0 caps at 6/7
# Target R values are STRETCH targets — most trades exit earlier via trailing
# stop (position_manager.py). These act as a hard ceiling for parabolic moves.
CONVICTION_RISK_TIERS = {
    3: {"risk_mult": 0.75, "target_R": 5.0},   # marginal — 1.5% risk
    4: {"risk_mult": 1.0,  "target_R": 6.0},   # base — 2% risk
    5: {"risk_mult": 1.25, "target_R": 7.0},   # solid — 2.5% risk
    6: {"risk_mult": 1.5,  "target_R": 9.0},   # strong — 3% risk
    7: {"risk_mult": 2.0,  "target_R": 12.0},  # exceptional — 4% risk, big bet
    8: {"risk_mult": 2.0,  "target_R": 12.0},  # perfect storm — same as 7
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
    "CSCO": "Tech", "PANW": "Tech", "ANET": "Tech",
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
    # AI Infrastructure / Data Center
    "DELL": "Tech", "HPE": "Tech", "VRT": "Tech", "CLS": "Tech", "SOUN": "Tech",
    # Nuclear / Energy
    "CEG": "Energy",
    # Quantum Computing
    "IONQ": "Quantum", "RGTI": "Quantum", "QBTS": "Quantum",
    # Defense
    "KTOS": "Defense",
    # Space
    "RDW": "Space", "RKLB": "Space", "ASTS": "Space", "LUNR": "Space", "PL": "Space",
    # Industrials / Utilities / Real Estate
    "XLI": "Industrials", "XLU": "Utilities", "XLRE": "Real Estate",
    # Broad index
    "SPY": "Index", "IWM": "Index", "DIA": "Index",
}

# ── Options trading (hybrid mode) ────────────────────────────────────────────
# Strategy B (breakout) and Catalyst mode use call options for leverage.
# Strategy A (pullback) stays as stocks — needs time, theta kills options.
OPTIONS_ENABLED            = False    # master switch (disabled — stocks compound better at $5k)
OPTIONS_STRATEGIES         = ["B"]    # which strategies route to options
OPTIONS_USE_FOR_CATALYST   = True     # catalyst mode uses options too
OPTIONS_MAX_CONTRACTS      = 2        # hard cap per trade on $5k account
OPTIONS_EXPIRY_MIN_DAYS    = 10       # min DTE for breakout options
OPTIONS_EXPIRY_MAX_DAYS    = 45       # max DTE — wide enough to always catch a monthly expiry
OPTIONS_CATALYST_EXPIRY_MIN = 3       # min DTE for catalyst (shorter hold)
OPTIONS_CATALYST_EXPIRY_MAX = 21      # max DTE for catalyst — catches nearest weekly or monthly
OPTIONS_TARGET_DELTA       = 0.60     # target delta for breakout calls
OPTIONS_CATALYST_DELTA     = 0.70     # higher delta for catalyst (less time)
OPTIONS_MIN_OPEN_INTEREST  = 50       # skip illiquid contracts (weeklies have lower OI)
OPTIONS_MAX_SPREAD_PCT     = 0.10     # max bid-ask spread as % of mid price
OPTIONS_RISK_PCT           = 0.05     # 5% of account per option trade (higher than stock 2% because max loss = premium)
OPTIONS_PREMIUM_STOP_PCT   = 0.50     # sell if premium drops to 50% of entry
OPTIONS_BREAKOUT_TARGET_PCT = 1.00    # sell at 100% gain for breakouts
OPTIONS_CATALYST_TARGET_PCT = 0.80    # sell at 80% gain for catalyst
OPTIONS_EXPIRY_WARN_DAYS   = 5        # exit before this many DTE (gamma risk)

# ── Strategy A thresholds (Trend Pullback) ───────────────────────────────────
# Momentum stocks pull back shallower than slow large-caps. RSI 50-65 catches
# "buy the dip in a strong uptrend" instead of "catch a falling knife at 40".
STRAT_A_RSI_LOW  = 50
STRAT_A_RSI_HIGH = 65

# ── Strategy B thresholds (Breakout) ────────────────────────────────────────
# Relaxed for momentum stocks — they rarely consolidate as tightly as slow caps.
STRAT_B_CONSOLIDATION_BARS = 3    # was 5 — momentum stocks barely consolidate
STRAT_B_VOLUME_MULT        = 1.2  # was 1.3 — catch more breakouts

# ── Strategy C thresholds (Mean Reversion) ──────────────────────────────────
STRAT_C_RSI_OVERSOLD  = 30
STRAT_C_RSI_OVERBOUGHT = 70
