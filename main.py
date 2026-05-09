"""
Swing Trading Scanner + Auto-Executor

Two modes controlled by MODE env var:
  FULL (default) — 9 AM + 4:30 PM ET — full pipeline
  NEWS           — 11 AM, 1 PM, 3 PM ET — news + safety-checked auto-buys

DST-aware: a workflow can fire at the wrong UTC hour for half the year.
We compute the actual current ET time and skip if outside the target window.
"""

import os
import json
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python 3.8 fallback

import pandas as pd
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient

import config as cfg
import indicators
from market_structure import classify, Regime
from screener import passes_filters, relative_strength
from strategies import scan
from order_flow import order_flow_score
from sentiment import get_sentiment, sentiment_label
from sector_rotation import analyze as sector_analyze, print_rotation
from risk import position_size
from executor import execute, get_open_positions, get_buying_power
from notifier import send_no_setup, send_signals, send

load_dotenv(Path(__file__).parent / ".env")

API_KEY    = os.environ["ALPACA_API_KEY"]
API_SECRET = os.environ["ALPACA_API_SECRET"]
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"
MODE       = os.getenv("MODE", "FULL").upper()
SKIP_TIME_GUARD = os.getenv("SKIP_TIME_GUARD", "false").lower() == "true"

ET                 = ZoneInfo("America/New_York")
MAX_MARKET_ATR_PCT = 3.0
LOG_FILE           = Path(__file__).parent / "scan_log.json"
MAX_LOG_ENTRIES    = 60

# Target ET hours for each mode (with ±45-min tolerance)
FULL_TARGET_ET_HOURS = [9, 16]            # 9 AM and 4:30 PM (use 16, tolerance covers 16:30)
NEWS_TARGET_ET_HOURS = [11, 13, 15]
TIME_TOLERANCE_MIN   = 45


# ── DST-aware schedule guard ─────────────────────────────────────────────────

def in_target_window(target_hours: list[int]) -> tuple[bool, str]:
    """
    Returns (in_window, et_time_str). Allows manual workflow_dispatch to skip
    via SKIP_TIME_GUARD=true.
    """
    if SKIP_TIME_GUARD:
        return True, "skip-guard enabled"

    now_et = datetime.now(ET)
    et_str = now_et.strftime("%Y-%m-%d %H:%M ET")

    for hour in target_hours:
        target = now_et.replace(hour=hour, minute=0 if hour != 16 else 30,
                                 second=0, microsecond=0)
        diff_min = abs((now_et - target).total_seconds()) / 60
        if diff_min <= TIME_TOLERANCE_MIN:
            return True, et_str
    return False, et_str


# ── Log persistence ───────────────────────────────────────────────────────────

def save_log(entry: dict):
    logs = []
    if LOG_FILE.exists():
        try:
            logs = json.loads(LOG_FILE.read_text())
        except Exception:
            logs = []
    logs.append(entry)
    logs = logs[-MAX_LOG_ENTRIES:]
    LOG_FILE.write_text(json.dumps(logs, indent=2, default=str))
    print(f"  [log] Saved to {LOG_FILE.name}")


# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_bars(symbols: list[str], data_client) -> dict[str, pd.DataFrame]:
    """
    Fetch in chunks so a single bad symbol can't kill the batch.
    """
    end   = datetime.utcnow()
    start = end - timedelta(days=cfg.LOOKBACK_DAYS)
    result = {}

    chunks = [symbols[i:i+25] for i in range(0, len(symbols), 25)]
    for chunk in chunks:
        try:
            req  = StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=TimeFrame.Day,
                start=start, end=end, feed="iex",
            )
            bars = data_client.get_stock_bars(req).df
            if bars.empty:
                continue
            if isinstance(bars.index, pd.MultiIndex):
                for sym in bars.index.get_level_values(0).unique():
                    df = bars.loc[sym].copy()
                    df.index = pd.to_datetime(df.index)
                    result[sym] = df
            else:
                df = bars.copy()
                df.index = pd.to_datetime(df.index)
                result[chunk[0]] = df
        except Exception as e:
            print(f"  [warn] chunk fetch failed ({chunk[0]}...): {e}")
    return result


def _print_news_summary(news_summary: list):
    print(f"{'='*60}\n  MARKET NEWS SENTIMENT SUMMARY\n{'='*60}\n")
    order_map = {"POSITIVE": 0, "NEUTRAL": 1, "NEGATIVE": 2}
    news_summary.sort(key=lambda x: (order_map.get(x["sentiment"], 1), -x["score"]))
    for n in news_summary:
        icon    = "+" if n["sentiment"] == "POSITIVE" else ("-" if n["sentiment"] == "NEGATIVE" else "~")
        rsi_str = f"  RSI {n['rsi']}" if n["rsi"] else ""
        print(f"  [{icon}] {n['symbol']:<6}  {n['sentiment']:<10}  score: {n['score']:+d}{rsi_str}")
        for h in n["headlines"]:
            print(f"        {h}")
        print()
    print(f"{'='*60}\n")


def _today_new_position_count(trade_client: TradingClient) -> int:
    """Count BUY orders submitted today (UTC)."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        orders = trade_client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.ALL, after=today_start, limit=100,
        ))
        return sum(1 for o in orders if str(o.side).endswith("BUY"))
    except Exception:
        return 0


def _sector_score(symbol: str, rotation: dict) -> float:
    """Look up the symbol's sector rotation score, or 999 if not in any sector."""
    for r in rotation["sectors"]:
        if r["ticker"] == symbol:
            return r["score"]
    return 999.0   # not a sector ETF — no penalty


def _symbol_sector(symbol: str) -> str:
    """Map a ticker to its sector bucket (for per-sector position caps)."""
    return cfg.TICKER_SECTOR.get(symbol, "Other")


def _count_sectors(symbols) -> dict[str, int]:
    counts = {}
    for sym in symbols:
        sec = _symbol_sector(sym)
        counts[sec] = counts.get(sec, 0) + 1
    return counts


# ── FULL SCAN ─────────────────────────────────────────────────────────────────

def run_full_scan():
    in_window, et_str = in_target_window(FULL_TARGET_ET_HOURS)
    if not in_window:
        print(f"  Skipping FULL scan — current ET time {et_str} not in target window "
              f"(target hours: {FULL_TARGET_ET_HOURS} ET ±{TIME_TOLERANCE_MIN}min)")
        return

    print(f"\n{'='*60}")
    print(f"  FULL SCAN  -  {et_str}")
    print(f"{'='*60}\n")

    data_client  = StockHistoricalDataClient(API_KEY, API_SECRET)
    news_client  = NewsClient(API_KEY, API_SECRET)
    trade_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)

    account       = trade_client.get_account()
    account_value = float(account.portfolio_value)
    buying_power  = float(account.buying_power)
    print(f"  Account value  : ${account_value:,.2f}")
    print(f"  Buying power   : ${buying_power:,.2f}")
    print(f"  Mode           : {'PAPER' if PAPER else 'LIVE'}\n")

    all_symbols = list(set(cfg.WATCHLIST + [cfg.BENCHMARK]))
    print(f"  Fetching bars for {len(all_symbols)} symbols...")
    all_bars = fetch_bars(all_symbols, data_client)

    benchmark_raw = all_bars.get(cfg.BENCHMARK)
    if benchmark_raw is None:
        print("  ERROR: could not fetch SPY"); return
    benchmark_df = indicators.add_all(benchmark_raw, cfg)
    spy_regime   = classify(benchmark_df)

    spy_atr_pct          = benchmark_df["atr_pct"].iloc[-1]
    market_too_volatile  = spy_atr_pct > MAX_MARKET_ATR_PCT
    vol_label            = f"ATR% {spy_atr_pct:.2f}% {'(HIGH - paused)' if market_too_volatile else '(normal)'}"
    print(f"  SPY regime       : {spy_regime.value}")
    print(f"  Market volatility: {vol_label}\n")

    all_bars_ind = {s: indicators.add_all(df, cfg) for s, df in all_bars.items() if len(df) >= 25}
    rotation     = sector_analyze(all_bars_ind, benchmark_df)
    print(f"{'='*60}\n  BIG MONEY SECTOR ROTATION\n{'='*60}\n")
    print_rotation(rotation)

    open_positions   = get_open_positions(trade_client)
    new_today_count  = _today_new_position_count(trade_client)
    if open_positions:
        print(f"  Holding ({len(open_positions)}/{cfg.MAX_CONCURRENT_POSITIONS}): {', '.join(open_positions)}")
    print(f"  New positions today: {new_today_count}/{cfg.MAX_NEW_PER_DAY}\n")

    # Hard caps
    can_open_more = (
        len(open_positions) < cfg.MAX_CONCURRENT_POSITIONS and
        new_today_count    < cfg.MAX_NEW_PER_DAY
    )
    if not can_open_more:
        print(f"  Position/daily cap reached — scanning for awareness only, no new orders.\n")

    print(f"  Scanning {len(cfg.WATCHLIST)} stocks...")
    signals      = []
    news_summary = []

    for symbol in cfg.WATCHLIST:
        if symbol == cfg.BENCHMARK:
            continue
        raw = all_bars.get(symbol)
        if raw is None or len(raw) < 60:
            continue
        df = indicators.add_all(raw, cfg)

        sent_score, sent_headlines = get_sentiment(symbol, news_client)
        sent_label = sentiment_label(sent_score)
        last       = df.iloc[-1]
        news_summary.append({
            "symbol": symbol, "sentiment": sent_label, "score": sent_score,
            "rsi": round(last["rsi"], 1) if not pd.isna(last["rsi"]) else None,
            "headlines": sent_headlines[:2],
        })

        if market_too_volatile or symbol in open_positions:
            continue
        passed, _ = passes_filters(df, cfg)
        if not passed:
            continue

        regime     = classify(df)
        rs         = relative_strength(df, benchmark_df)
        obv_status = indicators.obv_trend(df)
        if obv_status in ("DISTRIBUTION", "STEALTH_SELL"):
            continue

        signal = scan(df, regime, cfg)
        if signal is None:
            continue
        signal["symbol"] = symbol

        of_score, of_notes = order_flow_score(df)
        if of_score < 0 or sent_score <= -2:
            continue

        # Sector rotation alignment — symbol's sector must have a non-negative score
        sector_score   = _sector_score(symbol, rotation)
        sector_aligned = sector_score >= 0 or sector_score == 999.0
        if cfg.REQUIRE_SECTOR_ALIGNMENT and not sector_aligned:
            continue

        pos = position_size(account_value, signal["entry"], signal["stop"], cfg)
        signals.append({
            "symbol": symbol, "regime": regime.value, "rs": round(rs, 2),
            "obv": obv_status, "strategy": signal["strategy"],
            "confidence": signal.get("confidence", 0.0), "signal": signal["signal"],
            "entry": signal["entry"], "stop": signal["stop"], "target": signal["target"],
            "shares": pos["shares"], "notional": pos["notional"],
            "risk_$": pos["risk_dollars"], "target_risk": pos["target_risk"],
            "1R": pos["r1_target"], "2R": pos["r2_target"], "capped": pos["capped"],
            "of_score": of_score, "of_notes": of_notes,
            "sentiment": sent_label, "sent_score": sent_score,
            "headlines": sent_headlines[:2], "reason": signal["reason"],
            "sector_aligned": sector_aligned, "sector_score": sector_score,
            "pos": pos, "signal_raw": signal,
        })

    print(f"\n{'-'*60}")
    if not signals:
        print("  No setups found. Stay in cash.")
        print(f"{'-'*60}\n")
        _print_news_summary(news_summary)
        send_no_setup(spy_regime.value, rotation["posture"], rotation["sectors"])
        save_log({
            "timestamp": datetime.utcnow().isoformat(),
            "mode": "FULL", "spy_regime": spy_regime.value,
            "posture": rotation["posture"], "signals": [],
            "top_news": [{"symbol": n["symbol"], "sentiment": n["sentiment"],
                          "score": n["score"], "headlines": n["headlines"]}
                         for n in news_summary if n["score"] != 0][:10],
        })
        return

    # Sort by confidence × order-flow × RS — best setup first
    signals.sort(key=lambda x: (-(x.get("confidence", 0) * (1 + x["of_score"])), -x["rs"]))
    print(f"  {len(signals)} SETUP(S) FOUND — AUTO-TRADING ({'PAPER' if PAPER else 'LIVE'})")
    print(f"{'-'*60}\n")

    new_orders_this_run = 0
    remaining_bp        = buying_power
    sector_counts       = _count_sectors(open_positions)
    # Approximate current heat: each open position was sized to target risk
    open_heat_pct       = len(open_positions) * cfg.ACCOUNT_RISK_PCT

    for s in signals:
        capped_note = "  [position-capped]" if s["capped"] else ""
        sym_sector  = _symbol_sector(s["symbol"])
        print(f"  {s['symbol']:<6}  Strat {s['strategy']} (conf {s['confidence']:.2f})  |  "
              f"Regime: {s['regime']:<7}  |  RS: {s['rs']:>5.2f}  |  OBV: {s['obv']}")
        print(f"         Sentiment: {s['sentiment']:<10}  |  Order Flow: {s['of_score']:+d}/+3"
              f"  |  Sector score: {s['sector_score']:.1f}  |  Sector: {sym_sector}")
        print(f"         Entry: ${s['entry']:<9.2f} Stop: ${s['stop']:<9.2f} Target (2R): ${s['2R']:.2f}")
        print(f"         Shares: {s['shares']}   Notional: ${s['notional']:,.2f}   "
              f"Risk: ${s['risk_$']:.2f} (target ${s['target_risk']:.2f}){capped_note}")
        print(f"         Signal: {s['reason']}")

        # Hard caps
        if not can_open_more:
            print(f"         SKIPPED: position/daily cap reached")
            s["order_status"] = "SKIPPED: position/daily cap"
            print()
            continue
        if (len(open_positions) + new_orders_this_run) >= cfg.MAX_CONCURRENT_POSITIONS:
            print(f"         SKIPPED: would exceed MAX_CONCURRENT_POSITIONS")
            s["order_status"] = "SKIPPED: concurrent cap"
            print()
            continue
        if (new_today_count + new_orders_this_run) >= cfg.MAX_NEW_PER_DAY:
            print(f"         SKIPPED: would exceed MAX_NEW_PER_DAY")
            s["order_status"] = "SKIPPED: daily cap"
            print()
            continue
        # Per-sector cap (avoid correlated tech-heavy concentration)
        if sector_counts.get(sym_sector, 0) >= cfg.MAX_POSITIONS_PER_SECTOR:
            print(f"         SKIPPED: sector cap reached ({sym_sector} has "
                  f"{sector_counts[sym_sector]}/{cfg.MAX_POSITIONS_PER_SECTOR})")
            s["order_status"] = f"SKIPPED: sector cap ({sym_sector})"
            print()
            continue
        # Portfolio heat cap (limit aggregate open risk)
        new_heat_pct = s["risk_$"] / account_value if account_value > 0 else 0
        if open_heat_pct + new_heat_pct > cfg.MAX_PORTFOLIO_HEAT_PCT:
            print(f"         SKIPPED: would exceed portfolio heat cap "
                  f"({(open_heat_pct + new_heat_pct)*100:.2f}% > "
                  f"{cfg.MAX_PORTFOLIO_HEAT_PCT*100:.1f}%)")
            s["order_status"] = "SKIPPED: portfolio heat cap"
            print()
            continue

        result = execute(s["signal_raw"], s["pos"], trade_client, remaining_bp=remaining_bp)
        if result["status"] == "PLACED":
            print(f"         ORDER PLACED  id={result['order_id']}")
            s["order_status"]    = f"PLACED — id={result['order_id']}"
            remaining_bp         -= result["notional"]
            new_orders_this_run  += 1
            sector_counts[sym_sector] = sector_counts.get(sym_sector, 0) + 1
            open_heat_pct        += new_heat_pct
        elif result["status"] == "SKIPPED":
            print(f"         SKIPPED: {result['reason']}")
            s["order_status"] = f"SKIPPED: {result['reason']}"
        else:
            print(f"         ERROR: {result['reason']}")
            s["order_status"] = f"ERROR: {result['reason']}"
        print()

    print(f"{'-'*60}")
    print(f"  Risk per trade : {cfg.ACCOUNT_RISK_PCT*100:.0f}% target = ${account_value * cfg.ACCOUNT_RISK_PCT:,.2f}")
    print(f"  Max position   : {cfg.MAX_POSITION_PCT*100:.0f}% = ${account_value * cfg.MAX_POSITION_PCT:,.2f}")
    print(f"  Concurrent cap : {cfg.MAX_CONCURRENT_POSITIONS}  |  Daily cap: {cfg.MAX_NEW_PER_DAY}  |  "
          f"Per-sector cap: {cfg.MAX_POSITIONS_PER_SECTOR}")
    print(f"  Portfolio heat : {open_heat_pct*100:.2f}% / {cfg.MAX_PORTFOLIO_HEAT_PCT*100:.1f}% max  |  "
          f"Data feed: {cfg.DATA_FEED.upper()}")
    print(f"{'-'*60}\n")

    send_signals(signals, spy_regime.value, rotation["posture"], account_value)
    save_log({
        "timestamp": datetime.utcnow().isoformat(),
        "mode": "FULL", "spy_regime": spy_regime.value,
        "posture": rotation["posture"],
        "signals": [{
            "symbol": s["symbol"], "strategy": s["strategy"], "confidence": s["confidence"],
            "signal": s["signal"], "entry": s["entry"], "stop": s["stop"], "target": s["2R"],
            "shares": s["shares"], "notional": s["notional"], "risk_$": s["risk_$"],
            "sentiment": s["sentiment"], "obv": s["obv"], "of_score": s["of_score"],
            "sector_score": s["sector_score"], "reason": s["reason"],
            "order_status": s.get("order_status", ""), "headlines": s["headlines"],
        } for s in signals],
        "top_news": [{"symbol": n["symbol"], "sentiment": n["sentiment"],
                      "score": n["score"], "headlines": n["headlines"]}
                     for n in news_summary if n["score"] != 0][:10],
    })
    _print_news_summary(news_summary)


# ── NEWS SCAN ─────────────────────────────────────────────────────────────────

def run_news_scan():
    in_window, et_str = in_target_window(NEWS_TARGET_ET_HOURS)
    if not in_window:
        print(f"  Skipping NEWS scan — current ET time {et_str} not in target window "
              f"(target hours: {NEWS_TARGET_ET_HOURS} ET ±{TIME_TOLERANCE_MIN}min)")
        return

    print(f"\n{'='*60}")
    print(f"  NEWS SCAN  -  {et_str}")
    print(f"{'='*60}\n")

    data_client  = StockHistoricalDataClient(API_KEY, API_SECRET)
    news_client  = NewsClient(API_KEY, API_SECRET)
    trade_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)

    account       = trade_client.get_account()
    account_value = float(account.portfolio_value)
    buying_power  = float(account.buying_power)
    print(f"  Account value : ${account_value:,.2f}")
    print(f"  Buying power  : ${buying_power:,.2f}\n")

    all_symbols = list(set(cfg.WATCHLIST + [cfg.BENCHMARK]))
    print(f"  Fetching bars for {len(all_symbols)} symbols...")
    all_bars = fetch_bars(all_symbols, data_client)

    benchmark_raw = all_bars.get(cfg.BENCHMARK)
    if benchmark_raw is None:
        print("  ERROR: could not fetch SPY"); return
    benchmark_df = indicators.add_all(benchmark_raw, cfg)
    spy_regime   = classify(benchmark_df)
    spy_atr_pct  = benchmark_df["atr_pct"].iloc[-1]
    market_too_volatile = spy_atr_pct > MAX_MARKET_ATR_PCT

    print(f"  SPY regime: {spy_regime.value}  |  Volatility: ATR% {spy_atr_pct:.2f}%")

    # Safety gates for ANY auto-buy in NEWS mode
    safety_block_reason = None
    if spy_regime == Regime.BEAR:
        safety_block_reason = "SPY in BEAR regime — no news-based buys"
    elif market_too_volatile:
        safety_block_reason = f"market too volatile (ATR% {spy_atr_pct:.2f}%)"

    if safety_block_reason:
        print(f"  SAFETY BLOCK: {safety_block_reason} — only alerts will fire\n")
    else:
        print(f"  Safety: OK — news buys enabled\n")

    all_bars_ind = {s: indicators.add_all(df, cfg) for s, df in all_bars.items() if len(df) >= 25}
    rotation     = sector_analyze(all_bars_ind, benchmark_df)

    open_positions = get_open_positions(trade_client)
    held_pnl = {}
    try:
        for p in trade_client.get_all_positions():
            held_pnl[p.symbol] = float(p.unrealized_pl)
    except Exception:
        pass

    new_today_count = _today_new_position_count(trade_client)
    can_open_more = (
        not safety_block_reason and
        len(open_positions) < cfg.MAX_CONCURRENT_POSITIONS and
        new_today_count    < cfg.MAX_NEW_PER_DAY
    )

    if open_positions:
        print(f"  Holding ({len(open_positions)}/{cfg.MAX_CONCURRENT_POSITIONS}): {', '.join(open_positions)}")
    print(f"  New positions today: {new_today_count}/{cfg.MAX_NEW_PER_DAY}\n")

    bought              = []
    alerts              = []
    results             = []
    new_orders_this_run = 0
    remaining_bp        = buying_power
    sector_counts       = _count_sectors(open_positions)
    open_heat_pct       = len(open_positions) * cfg.ACCOUNT_RISK_PCT

    for symbol in cfg.WATCHLIST:
        raw = all_bars.get(symbol)
        if raw is None or len(raw) < 25:
            continue
        df = indicators.add_all(raw, cfg)

        sent_score, headlines = get_sentiment(symbol, news_client, days=1)
        label = sentiment_label(sent_score)
        last  = df.iloc[-1]

        results.append({
            "symbol": symbol, "label": label, "score": sent_score,
            "headlines": headlines[:2], "held": symbol in open_positions,
            "pnl": held_pnl.get(symbol),
        })

        # ALERT: held stock with negative news
        if symbol in open_positions and sent_score <= -2:
            alerts.append(f"DANGER — {symbol} held position has NEGATIVE news!")
            for h in headlines[:2]:
                alerts.append(f"  {h}")

        # BUY only with full safety stack
        if not can_open_more:
            continue
        if sent_score < 2 or symbol in open_positions:
            continue
        if (len(open_positions) + new_orders_this_run) >= cfg.MAX_CONCURRENT_POSITIONS:
            continue
        if (new_today_count + new_orders_this_run) >= cfg.MAX_NEW_PER_DAY:
            continue

        # Per-symbol safety: passes_filters + OBV not in distribution + sector aligned
        passed, _ = passes_filters(df, cfg)
        if not passed:
            continue

        obv_status = indicators.obv_trend(df)
        if obv_status in ("DISTRIBUTION", "STEALTH_SELL"):
            continue

        sector_score   = _sector_score(symbol, rotation)
        sector_aligned = sector_score >= 0 or sector_score == 999.0
        if cfg.REQUIRE_SECTOR_ALIGNMENT and not sector_aligned:
            continue

        # ATR-based stop
        entry = round(last["close"], 2)
        stop  = round(entry - last["atr"] * 1.5, 2)
        if stop <= 0 or stop >= entry:
            continue

        pos    = position_size(account_value, entry, stop, cfg)
        target = pos["r2_target"]
        if pos["shares"] <= 0:
            continue

        # Per-sector cap
        sym_sector = _symbol_sector(symbol)
        if sector_counts.get(sym_sector, 0) >= cfg.MAX_POSITIONS_PER_SECTOR:
            continue
        # Portfolio heat cap
        new_heat_pct = pos["risk_dollars"] / account_value if account_value > 0 else 0
        if open_heat_pct + new_heat_pct > cfg.MAX_PORTFOLIO_HEAT_PCT:
            continue

        signal_data = {"symbol": symbol, "signal": "BUY",
                       "entry": entry, "stop": stop, "target": target}
        result = execute(signal_data, pos, trade_client, remaining_bp=remaining_bp)

        if result["status"] == "PLACED":
            remaining_bp        -= result["notional"]
            new_orders_this_run += 1
            sector_counts[sym_sector] = sector_counts.get(sym_sector, 0) + 1
            open_heat_pct        += new_heat_pct

        bought.append({
            "symbol": symbol, "signal": "BUY",
            "entry": entry, "stop": stop, "target": target,
            "shares": pos["shares"], "notional": pos["notional"],
            "risk_$": pos["risk_dollars"], "sentiment": label, "score": sent_score,
            "headlines": headlines[:2],
            "order_status": f"{result['status']} {result.get('order_id') or result.get('reason','')}".strip(),
            "regime": spy_regime.value, "rs": 0, "obv": obv_status, "strategy": "NEWS",
            "1R": pos["r1_target"], "2R": pos["r2_target"],
            "of_score": 0, "of_notes": [], "sent_score": sent_score,
            "reason": f"News sentiment {sent_score:+d} + safety stack passed",
            "pos": pos, "signal_raw": signal_data,
        })
        print(f"  [BUY] {symbol}  score={sent_score:+d}  entry=${entry}  "
              f"stop=${stop}  target=${target}  -> {result['status']}")

    if alerts:
        print(f"\n  {'!'*3} ALERTS {'!'*3}")
        for a in alerts:
            print(f"  {a}")
        print()

    print(f"\n  {'Symbol':<7} {'Sentiment':<12} {'Score'}")
    print(f"  {'-'*40}")
    results.sort(key=lambda x: -x["score"])
    for r in results:
        icon     = "+" if r["label"] == "POSITIVE" else ("-" if r["label"] == "NEGATIVE" else "~")
        held_tag = "  [HOLDING]" if r["held"] else ""
        pnl_tag  = f"  P&L: ${r['pnl']:+.2f}" if r["pnl"] is not None else ""
        print(f"  [{icon}] {r['symbol']:<6} {r['label']:<12} {r['score']:+d}{held_tag}{pnl_tag}")
    print()

    if bought:
        send_signals(bought, spy_regime.value, "N/A", account_value)
    elif alerts:
        body = "<h2>AI Trader — News Alert</h2><ul>" + \
               "".join(f"<li>{a}</li>" for a in alerts) + "</ul>"
        send(f"AI Trader ALERT ({et_str})", body)
    else:
        print("  No significant news changes. No trades placed.")

    save_log({
        "timestamp": datetime.utcnow().isoformat(),
        "mode": "NEWS",
        "spy_regime": spy_regime.value,
        "safety_block": safety_block_reason or "",
        "signals": [{
            "symbol": b["symbol"], "strategy": "NEWS", "signal": "BUY",
            "entry": b["entry"], "stop": b["stop"], "target": b["target"],
            "shares": b["shares"], "notional": b["notional"], "risk_$": b["risk_$"],
            "sentiment": b["sentiment"], "reason": b["reason"],
            "order_status": b.get("order_status", ""), "headlines": b["headlines"],
        } for b in bought],
        "alerts": alerts,
        "news_snapshot": [{"symbol": r["symbol"], "sentiment": r["label"],
                           "score": r["score"]} for r in results],
    })


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if MODE == "NEWS":
        run_news_scan()
    else:
        run_full_scan()
