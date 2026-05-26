"""
Historical backtest of the AI Trader swing strategy.

Replays each trading day from START_DATE ->END_DATE through the full pipeline:
   filters ->market regime ->sector rotation ->strategies A/B/C ->conviction
   ->position management with trailing stops

Tracks every simulated trade and outputs return / win-rate / drawdown stats.

LIMITATIONS (what we cannot backtest with free data):
  - News sentiment      ->treated as 0 (neutral)
  - Catalyst mode       ->skipped entirely (needs intraday data)
  - Earnings blackout   ->skipped (yfinance .calendar only shows CURRENT)
  - VIX gate            ->uses historical ^VIX from yfinance (works)
  - Macro blackout      ->uses our hardcoded calendar (works)
  - Spread / intraday gap check ->skipped (no historical bid/ask data)
  - Trailing stops      ->use end-of-day update (not intraday like live)

This means the backtest is OPTIMISTIC vs. live: it ignores some friction.
But it gives a real number for the technical edge of the strategies.

Usage:
  python backtest.py
  python backtest.py 2023-01-01 2024-12-31
"""

import sys
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

# UTF-8 stdout on Windows
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd
import yfinance as yf

import os
import time

# Load .env for Alpaca API keys (needed for historical news)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config as cfg
import indicators
from market_structure import classify, Regime
from filters import passes_filters, relative_strength
from strategies import scan as strategy_scan
from order_flow import order_flow_score
from sector_rotation import analyze as sector_analyze
from conviction import score as conviction_score
from event_gates import in_macro_blackout, assess_vix
from analyst_ratings import analyst_score as get_analyst_score


START_DATE      = "2024-01-01"
END_DATE        = "2025-12-31"
INITIAL_CAPITAL = 5000.0
COST_PER_TRADE  = 0.0   # set non-zero to model spreads/fees

# Options backtest config
OPTIONS_BACKTEST        = False   # disabled — stocks compound better at $5k
OPTIONS_DTE             = 30      # longer DTE = less theta decay per day
OPTIONS_OTM_PCT         = 0.02    # strike 2% OTM (higher delta, more $ per move)
OPTIONS_RISK_PCT_BT     = 0.05    # 5% of account per option trade
OPTIONS_PREMIUM_STOP    = 0.50    # cut at 50% loss
OPTIONS_PROFIT_TARGET   = 1.00    # take profits at 100% gain
OPTIONS_MAX_HOLD        = 10      # max 10 days
OPTIONS_MAX_CONTRACTS   = 2       # max contracts per trade
OPTIONS_MIN_CONVICTION  = 4       # only use options on high-conviction breakouts
RISK_FREE_RATE          = 0.045   # approximate risk-free rate


# ── Black-Scholes option pricing ────────────────────────────────────────────

from scipy.stats import norm as _norm

def _bs_call_price(S, K, T, r, sigma):
    """
    Black-Scholes call option price.
    S: stock price, K: strike, T: time to expiry (years),
    r: risk-free rate, sigma: annualized volatility.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(S - K, 0)  # intrinsic only
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm.cdf(d1) - K * math.exp(-r * T) * _norm.cdf(d2)


def _bs_delta(S, K, T, r, sigma):
    """Black-Scholes call delta."""
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return _norm.cdf(d1)


def _historical_vol(df, window=20):
    """Annualized historical volatility from daily close prices."""
    if len(df) < window + 1:
        return 0.30  # default 30%
    returns = df["close"].pct_change().dropna().tail(window)
    if len(returns) < 5:
        return 0.30
    return float(returns.std() * math.sqrt(252))


# ── Option position for backtest ────────────────────────────────────────────

class OptionPosition:
    """Simulated call option position for backtesting."""
    def __init__(self, symbol, entry_date, stock_entry, strike, expiry_date,
                 entry_premium, contracts, conviction, strategy, sigma):
        self.symbol         = symbol
        self.entry_date     = entry_date
        self.stock_entry    = stock_entry
        self.strike         = strike
        self.expiry_date    = expiry_date
        self.entry_premium  = entry_premium   # per-share premium
        self.contracts      = contracts
        self.total_cost     = entry_premium * 100 * contracts
        self.conviction     = conviction
        self.strategy       = strategy
        self.sigma          = sigma           # entry vol for repricing
        self.exit_date      = None
        self.exit_premium   = None
        self.exit_reason    = None
        self.asset_type     = "OPTION"

    @property
    def is_open(self):
        return self.exit_date is None

    def current_premium(self, stock_price, today):
        """Reprice the option using Black-Scholes with entry vol."""
        dte = (self.expiry_date - today).days if hasattr(today, 'date') else \
              (self.expiry_date - today.date()).days if hasattr(today, 'date') else 0
        T = max(dte, 0) / 365.0
        return _bs_call_price(stock_price, self.strike, T, RISK_FREE_RATE, self.sigma)

    def current_value(self, stock_price, today):
        """Total market value of the position."""
        return self.current_premium(stock_price, today) * 100 * self.contracts

    def close(self, date, stock_price, reason):
        self.exit_date    = date
        self.exit_premium = self.current_premium(stock_price, date)
        self.exit_reason  = reason

    @property
    def pnl_dollars(self):
        if not self.exit_date:
            return 0.0
        return (self.exit_premium - self.entry_premium) * 100 * self.contracts

    @property
    def r_multiple(self):
        """R-multiple where 1R = total premium paid (max loss)."""
        if not self.exit_date or self.total_cost <= 0:
            return 0.0
        return self.pnl_dollars / self.total_cost

    @property
    def hold_days(self):
        if not self.exit_date:
            return 0
        return (self.exit_date - self.entry_date).days

    # Compatibility with stock Position for reporting
    @property
    def entry_price(self):
        return self.stock_entry

    @property
    def exit_price(self):
        return self.stock_entry  # not directly meaningful for options

    @property
    def shares(self):
        return self.contracts  # for trade count reporting


# ── Position state ───────────────────────────────────────────────────────────

class Position:
    def __init__(self, symbol, entry_date, entry_price, stop, target,
                 shares, conviction, target_R, strategy):
        self.symbol        = symbol
        self.entry_date    = entry_date
        self.entry_price   = entry_price
        self.original_stop = stop
        self.current_stop  = stop
        self.target        = target
        self.shares        = shares
        self.conviction    = conviction
        self.target_R      = target_R
        self.strategy      = strategy
        self.exit_date     = None
        self.exit_price    = None
        self.exit_reason   = None

    @property
    def is_open(self):
        return self.exit_date is None

    @property
    def stop_distance(self):
        return self.entry_price - self.original_stop

    def update_trailing_stop(self, current_price, atr):
        """
        End-of-day trailing stop update.

        FIX #1: Move to break-even at +1R instead of +2R.
        52% of trades were hitting full -1R stops because they never reached
        +2R. Moving to break-even at +1R turns many -1R losses into ~0R exits.

        Tiers:
          R < 1.5        : hold — give trade room to develop
          1.5 <= R < 3.0 : move stop to break-even
          3.0 <= R < 5.0 : trail to entry + 1R (lock in +1R profit)
          R >= 5.0       : trail at max(current - 1.5*ATR, entry + 2.5R)
        """
        if self.stop_distance <= 0:
            return
        r_mult = (current_price - self.entry_price) / self.stop_distance
        new_stop = self.current_stop
        if 1.5 <= r_mult < 3.0:
            # Break-even at +1.5R — gives winners room to develop
            new_stop = self.entry_price * 1.001
        elif 3.0 <= r_mult < 5.0:
            # Lock in +1R profit
            new_stop = self.entry_price + self.stop_distance * 1.0
        elif r_mult >= 5.0:
            # Tighter ATR trail: 1.5x ATR for trend continuation
            atr_stop = current_price - 1.5 * atr if atr and atr > 0 else 0
            r2_floor = self.entry_price + self.stop_distance * 2.5
            new_stop = max(self.current_stop, atr_stop, r2_floor)
        self.current_stop = max(self.current_stop, new_stop)

    def close(self, date, price, reason):
        self.exit_date   = date
        self.exit_price  = price
        self.exit_reason = reason

    @property
    def pnl_dollars(self):
        if not self.exit_date:
            return 0.0
        return self.shares * (self.exit_price - self.entry_price) - COST_PER_TRADE

    @property
    def r_multiple(self):
        if not self.exit_date or self.stop_distance <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.stop_distance

    @property
    def hold_days(self):
        if not self.exit_date:
            return 0
        return (self.exit_date - self.entry_date).days


# ── Data loading via yfinance (full SIP volume, historical) ──────────────────

def fetch_history(symbols, start, end):
    """yfinance batch fetch — returns dict of symbol ->DataFrame."""
    print(f"  Fetching {len(symbols)} symbols from {start} to {end}...")
    df = yf.download(symbols, start=start, end=end, group_by="ticker",
                     auto_adjust=False, progress=False, threads=True)
    out = {}
    for sym in symbols:
        try:
            if len(symbols) == 1:
                sub = df.copy()
            else:
                sub = df[sym].copy() if sym in df.columns.get_level_values(0) else None
            if sub is None or sub.empty:
                continue
            sub.columns = [c.lower() for c in sub.columns]
            if not all(c in sub.columns for c in ["open", "high", "low", "close", "volume"]):
                continue
            sub = sub.dropna()
            sub.attrs["volume_patched"] = True   # yfinance = full SIP volume
            out[sym] = sub
        except Exception:
            continue
    print(f"  Got data for {len(out)} symbols")
    return out


# ── Historical news pre-fetch & sentiment cache ─────────────────────────────

NEWS_CACHE_FILE = Path(__file__).parent / "backtest_news_cache.json"


def _load_news_cache() -> dict:
    if NEWS_CACHE_FILE.exists():
        try:
            return json.loads(NEWS_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_news_cache(cache: dict):
    NEWS_CACHE_FILE.write_text(json.dumps(cache, default=str))


def fetch_historical_news(symbols: list[str], start: str, end: str) -> dict:
    """
    Pre-fetch all news headlines from Alpaca for each symbol across the date
    range. Returns {symbol: {YYYY-MM-DD: [headline, ...]}} dict.

    Caches results to disk so subsequent runs don't re-fetch.
    Rate-limited: ~3 calls/sec to stay within Alpaca free tier limits.
    """
    api_key    = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_API_SECRET")
    if not api_key or not api_secret:
        print("  ⚠ No Alpaca API keys — skipping historical news (sentiment=0)")
        return {}

    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    cache = _load_news_cache()
    news_client = NewsClient(api_key, api_secret)
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt   = datetime.strptime(end, "%Y-%m-%d")

    fetched = 0
    skipped = 0
    for sym in symbols:
        if sym in (cfg.BENCHMARK, "SPY", "QQQ"):
            continue  # skip index proxies
        if sym not in cache:
            cache[sym] = {}

        # Fetch in monthly chunks
        chunk_start = start_dt
        while chunk_start < end_dt:
            chunk_end = min(chunk_start + timedelta(days=30), end_dt)
            cache_key = f"{chunk_start.strftime('%Y-%m')}"

            if cache_key in cache[sym]:
                skipped += 1
                chunk_start = chunk_end
                continue

            try:
                req = NewsRequest(
                    symbols=sym,
                    start=chunk_start.strftime("%Y-%m-%dT00:00:00Z"),
                    end=chunk_end.strftime("%Y-%m-%dT23:59:59Z"),
                    limit=50,
                )
                response = news_client.get_news(req)
                headlines_by_date = {}
                for key, data in response:
                    if key == "data" and isinstance(data, dict):
                        for article in data.get("news", []):
                            if isinstance(article, dict):
                                headline = article.get("headline", "")
                                created = article.get("created_at", "")
                            else:
                                headline = getattr(article, "headline", "")
                                created = getattr(article, "created_at", "")
                            if headline and created:
                                # created_at can be datetime or string
                                if hasattr(created, "strftime"):
                                    day_str = created.strftime("%Y-%m-%d")
                                else:
                                    day_str = str(created)[:10]
                                headlines_by_date.setdefault(day_str, []).append(headline)

                cache[sym][cache_key] = headlines_by_date
                fetched += 1

                # Rate limit: ~3 calls/sec
                if fetched % 3 == 0:
                    time.sleep(1.1)
                # Save every 50 fetches
                if fetched % 50 == 0:
                    _save_news_cache(cache)
                    print(f"    ... fetched {fetched} chunks, {skipped} cached")
            except Exception as e:
                # On error, store empty dict so we don't retry
                cache[sym][cache_key] = {}
                fetched += 1
                if "too many" in str(e).lower() or "429" in str(e):
                    time.sleep(5)  # back off on rate limit

            chunk_start = chunk_end

    _save_news_cache(cache)
    print(f"  News: {fetched} new API calls, {skipped} from cache")
    return cache


def _get_cached_sentiment(news_cache: dict, symbol: str, date) -> tuple[int, int]:
    """
    Look up cached news for symbol around the given date (3-day window).
    Returns (sent_score, analyst_score) using the same scoring as live.
    """
    if not news_cache or symbol not in news_cache:
        return 0, 0

    # Collect headlines from date and 2 days prior (same as live 3-day window)
    headlines = []
    for offset in range(3):
        check_date = date - timedelta(days=offset)
        day_str = check_date.strftime("%Y-%m-%d") if hasattr(check_date, 'strftime') else str(check_date)[:10]
        month_key = day_str[:7]
        month_data = news_cache.get(symbol, {}).get(month_key, {})
        if isinstance(month_data, dict):
            headlines.extend(month_data.get(day_str, []))

    if not headlines:
        return 0, 0

    # Score using FinBERT/keyword (same as live)
    try:
        from sentiment import _get_finbert, _score_with_finbert, _score_text
        pipe = _get_finbert()
        base_total = 0
        for h in headlines:
            base_total += _score_with_finbert(h, pipe) if pipe else _score_text(h)
        base_clamped = max(-3, min(3, base_total))
    except Exception:
        base_clamped = 0

    # Analyst score
    a_score, _ = get_analyst_score(headlines)
    sent_score = max(-5, min(5, base_clamped + a_score))

    return sent_score, a_score


# ── Main backtest loop ───────────────────────────────────────────────────────

def run_backtest(start_date=START_DATE, end_date=END_DATE):
    print(f"\n{'='*60}\n  AI TRADER BACKTEST\n{'='*60}\n")
    print(f"  Period   : {start_date}  to{end_date}")
    print(f"  Capital  : ${INITIAL_CAPITAL:,.2f}")
    print(f"  Watchlist: {len(cfg.WATCHLIST)} symbols\n")

    # Fetch enough history to compute indicators from day 1
    symbols   = list(set(cfg.WATCHLIST + [cfg.BENCHMARK] + cfg.ROTATION_ETFS))
    fetch_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=300)).strftime("%Y-%m-%d")
    bars      = fetch_history(symbols, fetch_start, end_date)

    # Fetch VIX
    print(f"  Fetching VIX history...")
    vix_df = yf.download("^VIX", start=fetch_start, end=end_date,
                         auto_adjust=False, progress=False)
    if isinstance(vix_df.columns, pd.MultiIndex):
        vix_df.columns = [c[0] for c in vix_df.columns]
    vix_close = vix_df["Close"]

    # Build list of all trading days from the SPY data
    if cfg.BENCHMARK not in bars:
        print(f"  ERROR: no data for {cfg.BENCHMARK}")
        return
    all_dates = bars[cfg.BENCHMARK].index
    start_dt  = pd.Timestamp(start_date)
    end_dt    = pd.Timestamp(end_date)
    trade_dates = [d for d in all_dates if start_dt <= d.tz_localize(None) <= end_dt] \
                  if all_dates.tz else [d for d in all_dates if start_dt <= d <= end_dt]

    print(f"  Trading days: {len(trade_dates)}\n")

    # Fetch historical news for sentiment scoring
    print(f"  Fetching historical news...")
    news_cache = fetch_historical_news(cfg.WATCHLIST, start_date, end_date)

    # State
    capital            = INITIAL_CAPITAL
    open_positions     = []   # list of Position
    closed_positions   = []
    equity_curve       = []   # list of (date, equity, num_positions)
    pending_signals    = []   # signals generated yesterday, to fill at today's open

    # FIX #2: Bear market cooldown — after SPY flips to BEAR, require N
    # consecutive BULL days before re-entering. Prevents whipsawing in
    # choppy bear markets (2022: 15 consecutive losses, 10 whipsaws).
    BEAR_COOLDOWN_DAYS  = 5
    bear_cooldown_left  = 0     # days remaining before new entries allowed
    last_regime         = None  # track regime transitions

    # FIX #3: Losing streak throttle — after N consecutive losses, halve
    # position size. After 2N, pause entirely. Prevents the -15 streak.
    STREAK_HALVE     = 3   # halve size after 3 consecutive losses
    STREAK_PAUSE     = 6   # pause new entries after 6 consecutive losses
    PAUSE_DAYS       = 3   # stay paused for 3 trading days
    consec_losses    = 0
    pause_days_left  = 0

    # FIX #4: Time stop — close trades that go nowhere in N days.
    # Dead money in sideways markets. Frees capital for better setups.
    MAX_HOLD_DAYS    = 20

    # Precompute indicators on the full history for each symbol
    print(f"  Precomputing indicators...")
    bars_ind = {}
    for sym, df in bars.items():
        if len(df) >= 60:
            bars_ind[sym] = indicators.add_all(df, cfg)
    print(f"  Indicators ready for {len(bars_ind)} symbols\n")

    print(f"{'Date':<12} {'Equity':>10} {'Open':>5} {'Action':<60}")
    print("-" * 100)

    # ── Per-day replay ───────────────────────────────────────────────────────
    for day_idx, today in enumerate(trade_dates):
        # 1. Fill any pending signals (orders generated yesterday at today's open)
        for sig in pending_signals:
            sym = sig["symbol"]
            if sym not in bars_ind:
                continue
            df = bars_ind[sym]
            if today not in df.index:
                continue
            today_open = float(df.loc[today, "open"])
            # Sanity: if open gapped >2% from signal price, skip (bidirectional gap check)
            gap_pct = (today_open - sig["entry"]) / sig["entry"] * 100
            if abs(gap_pct) > 2.0:
                continue

            # HYBRID: Strategy B with high conviction → try options, fall back to stock
            if OPTIONS_BACKTEST and sig["strategy"] == "B" and sig["conviction"] >= OPTIONS_MIN_CONVICTION:
                # Calculate historical volatility for BS pricing
                df_slice = bars_ind[sym].loc[:today]
                sigma = _historical_vol(df_slice)
                strike = round(today_open * (1 + OPTIONS_OTM_PCT), 2)
                expiry_date = today + timedelta(days=OPTIONS_DTE)
                T = OPTIONS_DTE / 365.0
                premium = _bs_call_price(today_open, strike, T, RISK_FREE_RATE, sigma)

                if premium > 0:
                    account_value = capital + sum(
                        _position_value(p, bars_ind, today) if isinstance(p, Position)
                        else p.current_value(float(bars_ind[p.symbol].loc[today, "close"]), today)
                            if p.symbol in bars_ind and today in bars_ind[p.symbol].index
                            else p.total_cost
                        for p in open_positions
                    )
                    max_risk = account_value * OPTIONS_RISK_PCT_BT * sig.get("risk_mult", 1.0)
                    cost_per = premium * 100
                    contracts = min(math.floor(max_risk / cost_per), OPTIONS_MAX_CONTRACTS) if cost_per > 0 else 0

                    if contracts > 0 and cost_per * contracts <= capital:
                        opt = OptionPosition(
                            symbol=sym, entry_date=today, stock_entry=today_open,
                            strike=strike, expiry_date=expiry_date,
                            entry_premium=premium, contracts=contracts,
                            conviction=sig["conviction"], strategy="B", sigma=sigma)
                        open_positions.append(opt)
                        capital -= opt.total_cost
                        continue  # skip stock fallback

            # Stock position (default, or options fallback)
            shares = sig["shares"]
            if shares <= 0 or shares * today_open > capital:
                continue
            pos = Position(symbol=sym, entry_date=today, entry_price=today_open,
                          stop=sig["stop"], target=sig["target"], shares=shares,
                          conviction=sig["conviction"], target_R=sig["target_R"],
                          strategy=sig["strategy"])
            open_positions.append(pos)
            capital -= shares * today_open
        pending_signals = []

        # 2. Manage open positions (check stops/targets/trailing using today's bar)
        for pos in list(open_positions):
            sym = pos.symbol
            if sym not in bars_ind or today not in bars_ind[sym].index:
                continue
            bar = bars_ind[sym].loc[today]
            today_low   = float(bar["low"])
            today_high  = float(bar["high"])
            today_close = float(bar["close"])
            atr         = float(bar.get("atr", 0) or 0)

            # ── Option position management ──────────────────────────────────
            if isinstance(pos, OptionPosition):
                current_val = pos.current_value(today_close, today)
                pct_change  = (current_val - pos.total_cost) / pos.total_cost if pos.total_cost > 0 else 0
                hold_days   = (today - pos.entry_date).days
                dte         = (pos.expiry_date - today).days if hasattr(today, 'date') else 0

                exit_reason = None
                # Premium stop: cut at 50% loss
                if pct_change <= -OPTIONS_PREMIUM_STOP:
                    exit_reason = "option_premium_stop"
                # Profit target: take at 100% gain
                elif pct_change >= OPTIONS_PROFIT_TARGET:
                    exit_reason = "option_target"
                # Time stop for options (shorter than stocks)
                elif hold_days >= OPTIONS_MAX_HOLD:
                    exit_reason = "option_time_stop"
                # Expiry risk: exit if <= 3 DTE
                elif dte <= 3:
                    exit_reason = "option_expiry_risk"

                if exit_reason:
                    pos.close(today, today_close, exit_reason)
                    capital += current_val  # return whatever value remains
                    open_positions.remove(pos)
                    closed_positions.append(pos)
                    if pos.pnl_dollars <= 0:
                        consec_losses += 1
                    else:
                        consec_losses = 0
                continue  # skip stock management for option positions

            # ── Stock position management ───────────────────────────────────
            # FIX #4: Time stop — close positions that go nowhere in N days
            hold_days = (today - pos.entry_date).days
            if hold_days >= MAX_HOLD_DAYS:
                pos.close(today, today_close, "time_stop")
                capital += pos.shares * today_close
                open_positions.remove(pos)
                closed_positions.append(pos)
                if pos.pnl_dollars <= 0:
                    consec_losses += 1
                else:
                    consec_losses = 0
                continue

            # Check stop hit (uses current_stop after any trailing)
            if today_low <= pos.current_stop:
                exit_price = pos.current_stop
                pos.close(today, exit_price, "stop")
                capital += pos.shares * exit_price
                open_positions.remove(pos)
                closed_positions.append(pos)
                # FIX #3: Track consecutive losses for streak throttle
                if pos.pnl_dollars <= 0:
                    consec_losses += 1
                    if consec_losses >= STREAK_PAUSE:
                        pause_days_left = PAUSE_DAYS
                else:
                    consec_losses = 0
                continue
            # No hard target — trail stop up and let winners run.
            # position_manager.py does this live; here we replicate EOD.
            pos.update_trailing_stop(today_close, atr)

        # 3. Check regime — if SPY went BEAR, exit all positions at close
        spy_slice = bars_ind[cfg.BENCHMARK].loc[:today]
        spy_regime = classify(spy_slice)
        if spy_regime == Regime.BEAR:
            for pos in list(open_positions):
                if pos.symbol in bars_ind and today in bars_ind[pos.symbol].index:
                    exit_price = float(bars_ind[pos.symbol].loc[today, "close"])
                    if isinstance(pos, OptionPosition):
                        pos.close(today, exit_price, "regime_bear")
                        capital += pos.current_value(exit_price, today)
                    else:
                        pos.close(today, exit_price, "regime_bear")
                        capital += pos.shares * exit_price
                    open_positions.remove(pos)
                    closed_positions.append(pos)
                    # FIX #3: Track consecutive losses
                    if pos.pnl_dollars <= 0:
                        consec_losses += 1
                    else:
                        consec_losses = 0

        # FIX #2: Bear cooldown — when regime flips to BEAR, set cooldown.
        # Decrement each day. Block new entries while cooldown > 0.
        if spy_regime == Regime.BEAR and last_regime != Regime.BEAR:
            bear_cooldown_left = BEAR_COOLDOWN_DAYS   # just entered bear
        elif spy_regime != Regime.BEAR and bear_cooldown_left > 0:
            bear_cooldown_left -= 1   # recovering — count down
        last_regime = spy_regime

        # 4. Skip new entries if macro blackout for this date
        macro_blocked, _ = in_macro_blackout(
            now_et=datetime.combine(today.date() if hasattr(today, 'date') else today,
                                     datetime.min.time()).replace(tzinfo=None),
            hours_before=cfg.MACRO_BLACKOUT_HOURS_BEFORE,
            post_open_buffer_min=0,   # backtest is EOD, no intraday
        )
        if macro_blocked:
            # Compute equity and skip new entries
            equity = capital + sum(_position_value(p, bars_ind, today) for p in open_positions)
            equity_curve.append((today, equity, len(open_positions)))
            continue

        # 5. VIX gate
        try:
            vix_val = float(vix_close.loc[:today].iloc[-1]) if not vix_close.loc[:today].empty else None
        except Exception:
            vix_val = None
        vix_assessment = assess_vix(vix_val, cfg)
        if vix_assessment["block"]:
            equity = capital + sum(_position_value(p, bars_ind, today) for p in open_positions)
            equity_curve.append((today, equity, len(open_positions)))
            continue

        # FIX #2: Bear cooldown — skip new entries while in cooldown
        if bear_cooldown_left > 0:
            equity = capital + sum(_position_value(p, bars_ind, today) for p in open_positions)
            equity_curve.append((today, equity, len(open_positions)))
            continue

        # FIX #3: Losing streak pause — skip new entries entirely
        if pause_days_left > 0:
            pause_days_left -= 1
            equity = capital + sum(_position_value(p, bars_ind, today) for p in open_positions)
            equity_curve.append((today, equity, len(open_positions)))
            continue

        # 6. Look for new signals — skip if at concurrent cap
        if len(open_positions) >= cfg.MAX_CONCURRENT_POSITIONS:
            equity = capital + sum(_position_value(p, bars_ind, today) for p in open_positions)
            equity_curve.append((today, equity, len(open_positions)))
            continue

        # Sector rotation on snapshot
        bars_today = {s: bars_ind[s].loc[:today] for s in bars_ind
                      if today in bars_ind[s].index and len(bars_ind[s].loc[:today]) >= 25}
        if cfg.BENCHMARK not in bars_today:
            continue
        try:
            rotation = sector_analyze(bars_today, bars_today[cfg.BENCHMARK])
        except Exception:
            rotation = {"sectors": [], "posture": "MIXED"}

        candidates_today = []
        for sym in cfg.WATCHLIST:
            if sym == cfg.BENCHMARK or sym not in bars_ind:
                continue
            if any(p.symbol == sym for p in open_positions):
                continue
            df_slice = bars_ind[sym].loc[:today]
            if len(df_slice) < 60 or today not in df_slice.index:
                continue
            passed, _ = passes_filters(df_slice, cfg)
            if not passed:
                continue
            regime  = classify(df_slice)
            rs      = relative_strength(df_slice, bars_today[cfg.BENCHMARK])
            obv     = indicators.obv_trend(df_slice)
            if obv == "STEALTH_SELL":
                continue
            signal = strategy_scan(df_slice, regime, cfg)
            if signal is None:
                continue
            of, _ = order_flow_score(df_slice)
            if of < 0:
                continue
            sec_score = next((r["score"] for r in rotation["sectors"] if r["ticker"] == sym), 999.0)
            # Historical sentiment — use cached news if available, else 0
            # (keyword scorer without FinBERT hurts more than helps —
            #  set USE_NEWS_IN_BACKTEST=True to enable once FinBERT installed)
            USE_NEWS_IN_BACKTEST = False
            if USE_NEWS_IN_BACKTEST:
                sent_score, a_score = _get_cached_sentiment(news_cache, sym, today)
            else:
                sent_score, a_score = 0, 0
            conv = conviction_score(
                signal=signal,
                context={"rs": rs, "obv": obv, "of_score": of,
                         "sent_score": sent_score, "analyst_score": a_score,
                         "sector_score": sec_score,
                         "vix": vix_val or 0, "regime": regime.value},
                cfg=cfg,
            )
            if not conv["should_trade"]:
                continue
            # Position sizing (account = capital + open value)
            account_value = capital + sum(_position_value(p, bars_ind, today) for p in open_positions)
            from executor import position_size
            pos_info = position_size(account_value, signal["entry"], signal["stop"],
                                     cfg, risk_mult=conv["risk_mult"],
                                     target_R=conv["target_R"])
            if pos_info["shares"] <= 0:
                continue
            # FIX #3: Losing streak throttle — halve size after N consecutive losses
            shares_adj = pos_info["shares"]
            if consec_losses >= STREAK_HALVE:
                shares_adj = max(1, shares_adj // 2)
            candidates_today.append({
                "symbol": sym, "entry": signal["entry"], "stop": signal["stop"],
                "target": pos_info["r_target"], "shares": shares_adj,
                "conviction": conv["score"], "target_R": conv["target_R"],
                "strategy": signal["strategy"],
            })

        # Take top N candidates (by conviction)
        candidates_today.sort(key=lambda c: -c["conviction"])
        slots_left = min(cfg.MAX_NEW_PER_DAY,
                         cfg.MAX_CONCURRENT_POSITIONS - len(open_positions))
        for sig in candidates_today[:slots_left]:
            pending_signals.append(sig)   # will fill at next day's open

        # 7. Snapshot equity at end of day
        equity = capital + sum(_position_value(p, bars_ind, today) for p in open_positions)
        equity_curve.append((today, equity, len(open_positions)))

        # 8. Periodic progress print
        if day_idx % 20 == 0 or day_idx == len(trade_dates) - 1:
            print(f"{today.strftime('%Y-%m-%d'):<12} ${equity:>9,.0f} "
                  f"{len(open_positions):>5} {len(closed_positions)} trades closed")

    # ── Force-close any remaining positions at last close ─────────────────────
    for pos in list(open_positions):
        sym = pos.symbol
        if sym in bars_ind and len(bars_ind[sym]) > 0:
            exit_price = float(bars_ind[sym].iloc[-1]["close"])
            pos.close(trade_dates[-1], exit_price, "end_of_test")
            if isinstance(pos, OptionPosition):
                capital += pos.current_value(exit_price, trade_dates[-1])
            else:
                capital += pos.shares * exit_price
            closed_positions.append(pos)
    open_positions = []

    # ── Report ────────────────────────────────────────────────────────────────
    _print_results(closed_positions, equity_curve, INITIAL_CAPITAL)
    _save_results(closed_positions, equity_curve)


def _position_value(pos, bars_ind, today):
    """Current market value of an open position (stock or option)."""
    if isinstance(pos, OptionPosition):
        if pos.symbol in bars_ind and today in bars_ind[pos.symbol].index:
            price = float(bars_ind[pos.symbol].loc[today, "close"])
            return pos.current_value(price, today)
        return pos.total_cost  # fallback: assume entry value
    # Stock position
    if pos.symbol in bars_ind and today in bars_ind[pos.symbol].index:
        price = float(bars_ind[pos.symbol].loc[today, "close"])
        return pos.shares * price
    return pos.shares * pos.entry_price


def _print_results(trades, equity_curve, initial):
    if not trades:
        print("\n  No trades placed during backtest period.")
        return
    final_equity = equity_curve[-1][1] if equity_curve else initial
    total_return = (final_equity / initial - 1) * 100
    days         = (equity_curve[-1][0] - equity_curve[0][0]).days if len(equity_curve) >= 2 else 1
    years        = days / 365.25
    annual       = ((final_equity / initial) ** (1/years) - 1) * 100 if years > 0 else 0

    wins   = [t for t in trades if t.pnl_dollars > 0]
    losses = [t for t in trades if t.pnl_dollars <= 0]
    win_rate = 100 * len(wins) / len(trades)
    avg_win  = sum(t.pnl_dollars for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_dollars for t in losses) / len(losses) if losses else 0
    avg_R    = sum(t.r_multiple for t in trades) / len(trades)
    profit_factor = (sum(t.pnl_dollars for t in wins)
                     / abs(sum(t.pnl_dollars for t in losses))) if losses else float('inf')

    # Max drawdown
    peak = initial
    max_dd = 0.0
    for d, eq, _ in equity_curve:
        if eq > peak: peak = eq
        dd = (eq - peak) / peak * 100
        if dd < max_dd: max_dd = dd

    print(f"\n{'='*60}\n  RESULTS\n{'='*60}\n")
    print(f"  Total trades        : {len(trades)}")
    print(f"  Win rate            : {win_rate:.1f}%  ({len(wins)} wins / {len(losses)} losses)")
    print(f"  Average win         : ${avg_win:+,.2f}")
    print(f"  Average loss        : ${avg_loss:+,.2f}")
    print(f"  Average R-multiple  : {avg_R:+.2f}R")
    print(f"  Profit factor       : {profit_factor:.2f}")
    print()
    print(f"  Starting capital    : ${initial:,.2f}")
    print(f"  Final capital       : ${final_equity:,.2f}")
    print(f"  Total return        : {total_return:+.2f}%")
    print(f"  Annual return       : {annual:+.2f}%")
    print(f"  Max drawdown        : {max_dd:.2f}%")
    print()
    print(f"  Period              : {equity_curve[0][0].strftime('%Y-%m-%d')}"
          f" ->{equity_curve[-1][0].strftime('%Y-%m-%d')}  ({years:.1f} years)\n")

    # Exit reason breakdown
    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    print(f"  Exit reasons:")
    for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"    {r:<25} {n}")

    # Stock vs Option breakdown
    stock_trades  = [t for t in trades if isinstance(t, Position)]
    option_trades = [t for t in trades if isinstance(t, OptionPosition)]
    if option_trades:
        print(f"\n  {'─'*40}")
        print(f"  STOCK vs OPTION breakdown:")
        for label, subset in [("Stocks", stock_trades), ("Options", option_trades)]:
            if not subset:
                continue
            sw = [t for t in subset if t.pnl_dollars > 0]
            sl = [t for t in subset if t.pnl_dollars <= 0]
            wr = 100 * len(sw) / len(subset)
            pnl = sum(t.pnl_dollars for t in subset)
            avg_r = sum(t.r_multiple for t in subset) / len(subset)
            print(f"    {label:8}: {len(subset):>3} trades, WR={wr:.0f}%, "
                  f"avg R={avg_r:+.2f}, total P&L=${pnl:+,.0f}")
    print()


def _save_results(trades, equity_curve):
    trade_records = []
    for t in trades:
        rec = {
            "symbol": t.symbol, "strategy": t.strategy, "conviction": t.conviction,
            "entry_date": t.entry_date.strftime("%Y-%m-%d"),
            "exit_date": t.exit_date.strftime("%Y-%m-%d") if t.exit_date else None,
            "pnl": round(t.pnl_dollars, 2),
            "r_multiple": round(t.r_multiple, 2), "hold_days": t.hold_days,
            "exit_reason": t.exit_reason,
        }
        if isinstance(t, OptionPosition):
            rec["asset_type"]     = "OPTION"
            rec["strike"]         = t.strike
            rec["entry_premium"]  = round(t.entry_premium, 2)
            rec["exit_premium"]   = round(t.exit_premium, 2) if t.exit_premium else None
            rec["contracts"]      = t.contracts
            rec["total_cost"]     = round(t.total_cost, 2)
        else:
            rec["asset_type"]     = "STOCK"
            rec["entry"]          = round(t.entry_price, 2)
            rec["exit"]           = round(t.exit_price, 2) if t.exit_price else None
            rec["stop"]           = round(t.original_stop, 2)
            rec["target"]         = round(t.target, 2)
            rec["shares"]         = t.shares
        trade_records.append(rec)

    out = {
        "trades": trade_records,
        "equity_curve": [(d.strftime("%Y-%m-%d"), round(eq, 2), n) for d, eq, n in equity_curve],
    }
    Path("backtest_results.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"  Full results saved to backtest_results.json\n")


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else START_DATE
    end   = sys.argv[2] if len(sys.argv) > 2 else END_DATE
    run_backtest(start, end)
