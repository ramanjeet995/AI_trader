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

import config as cfg
import indicators
from market_structure import classify, Regime
from filters import passes_filters, relative_strength
from strategies import scan as strategy_scan
from order_flow import order_flow_score
from sector_rotation import analyze as sector_analyze
from conviction import score as conviction_score
from event_gates import in_macro_blackout, assess_vix


START_DATE      = "2024-01-01"
END_DATE        = "2025-12-31"
INITIAL_CAPITAL = 5000.0
COST_PER_TRADE  = 0.0   # set non-zero to model spreads/fees


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
        """End-of-day trailing stop update (matches live position_manager.py logic)."""
        if self.stop_distance <= 0:
            return
        r_mult = (current_price - self.entry_price) / self.stop_distance
        new_stop = self.current_stop
        if 1.0 <= r_mult < 2.0:
            new_stop = self.entry_price * 1.001
        elif 2.0 <= r_mult < 3.0:
            new_stop = self.entry_price + self.stop_distance * 1.0
        elif r_mult >= 3.0:
            atr_stop = current_price - 2 * atr if atr and atr > 0 else 0
            r2_floor = self.entry_price + self.stop_distance * 2.0
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

    # State
    capital            = INITIAL_CAPITAL
    open_positions     = []   # list of Position
    closed_positions   = []
    equity_curve       = []   # list of (date, equity, num_positions)
    pending_signals    = []   # signals generated yesterday, to fill at today's open

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
            # Open position at today's open
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

            # Check stop hit (uses current_stop after any trailing)
            if today_low <= pos.current_stop:
                exit_price = pos.current_stop
                pos.close(today, exit_price, "stop")
                capital += pos.shares * exit_price
                open_positions.remove(pos)
                closed_positions.append(pos)
                continue
            # Check target hit
            if today_high >= pos.target:
                exit_price = pos.target
                pos.close(today, exit_price, "target")
                capital += pos.shares * exit_price
                open_positions.remove(pos)
                closed_positions.append(pos)
                continue
            # No exit — update trailing stop using today's close
            pos.update_trailing_stop(today_close, atr)

        # 3. Check regime — if SPY went BEAR, exit all positions at close
        spy_slice = bars_ind[cfg.BENCHMARK].loc[:today]
        spy_regime = classify(spy_slice)
        if spy_regime == Regime.BEAR:
            for pos in list(open_positions):
                if pos.symbol in bars_ind and today in bars_ind[pos.symbol].index:
                    exit_price = float(bars_ind[pos.symbol].loc[today, "close"])
                    pos.close(today, exit_price, "regime_bear")
                    capital += pos.shares * exit_price
                    open_positions.remove(pos)
                    closed_positions.append(pos)

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
            if obv in ("DISTRIBUTION", "STEALTH_SELL"):
                continue
            signal = strategy_scan(df_slice, regime, cfg)
            if signal is None:
                continue
            of, _ = order_flow_score(df_slice)
            if of < 0:
                continue
            sec_score = next((r["score"] for r in rotation["sectors"] if r["ticker"] == sym), 999.0)
            # Skip sentiment factor (no historical data) — pass 0
            conv = conviction_score(
                signal=signal,
                context={"rs": rs, "obv": obv, "of_score": of, "sent_score": 0,
                         "sector_score": sec_score, "vix": vix_val or 0,
                         "regime": regime.value},
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
            candidates_today.append({
                "symbol": sym, "entry": signal["entry"], "stop": signal["stop"],
                "target": pos_info["r_target"], "shares": pos_info["shares"],
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
            capital += pos.shares * exit_price
            closed_positions.append(pos)
    open_positions = []

    # ── Report ────────────────────────────────────────────────────────────────
    _print_results(closed_positions, equity_curve, INITIAL_CAPITAL)
    _save_results(closed_positions, equity_curve)


def _position_value(pos, bars_ind, today):
    """Current market value of an open position."""
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
        print(f"    {r:<15} {n}")
    print()


def _save_results(trades, equity_curve):
    out = {
        "trades": [{
            "symbol": t.symbol, "strategy": t.strategy, "conviction": t.conviction,
            "entry_date": t.entry_date.strftime("%Y-%m-%d"),
            "exit_date": t.exit_date.strftime("%Y-%m-%d") if t.exit_date else None,
            "entry": round(t.entry_price, 2), "exit": round(t.exit_price, 2) if t.exit_price else None,
            "stop": round(t.original_stop, 2), "target": round(t.target, 2),
            "shares": t.shares, "pnl": round(t.pnl_dollars, 2),
            "r_multiple": round(t.r_multiple, 2), "hold_days": t.hold_days,
            "exit_reason": t.exit_reason,
        } for t in trades],
        "equity_curve": [(d.strftime("%Y-%m-%d"), round(eq, 2), n) for d, eq, n in equity_curve],
    }
    Path("backtest_results.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"  Full results saved to backtest_results.json\n")


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else START_DATE
    end   = sys.argv[2] if len(sys.argv) > 2 else END_DATE
    run_backtest(start, end)
