"""
Swing Trading Scanner + Auto-Executor

Two modes controlled by MODE env var:
  FULL (default) — 9 AM + 4:30 PM ET — full pipeline
  NEWS           — 11 AM, 1 PM, 3 PM ET — news + safety-checked auto-buys

DST-aware: a workflow can fire at the wrong UTC hour for half the year.
We compute the actual current ET time and skip if outside the target window.
"""

import os
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path

# Force UTF-8 stdout on Windows so news headlines with unicode (hyphens,
# em-dashes, etc.) don't crash the scan. Linux/GitHub Actions is UTF-8 already.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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
from executor import execute, get_open_positions, get_buying_power, is_market_open, \
    list_catalyst_positions, close_position
import catalyst_detector
import strategy_catalyst
from notifier import send_no_setup, send_signals, send
import volume_source
import earnings_calendar
import vix_gate
import pretrade_check

load_dotenv(Path(__file__).parent / ".env")

API_KEY    = os.environ["ALPACA_API_KEY"]
API_SECRET = os.environ["ALPACA_API_SECRET"]
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"
MODE       = os.getenv("MODE", "FULL").upper()
SKIP_TIME_GUARD = os.getenv("SKIP_TIME_GUARD", "false").lower() == "true"

ET                 = ZoneInfo("America/New_York")
MAX_MARKET_ATR_PCT = 3.0
LOG_FILE           = Path(__file__).parent / "scan_log.json"
LOG_MD_FILE        = Path(__file__).parent / "scan_log.md"
MAX_LOG_ENTRIES    = 60
MAX_MD_ENTRIES     = 100   # readable file, keep more history

# Target ET hours for each mode (with ±45-min tolerance)
FULL_TARGET_ET_HOURS     = [9, 16]            # 9:30 AM (post-open) and 4:30 PM (use 9 + 16, tolerance covers :30)
NEWS_TARGET_ET_HOURS     = [11, 13, 15]
CATALYST_TARGET_ET_HOURS = [11]               # 11 AM ET (after initial volatility settles)
TIME_TOLERANCE_MIN       = 60


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

    # 9 → 9:30 AM (market open), 16 → 4:30 PM (market close wind-down), else → :00
    for hour in target_hours:
        minute = 30 if hour in (9, 16) else 0
        target = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
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
    _save_human_log(logs[-MAX_MD_ENTRIES:])
    print(f"  [log] Saved to {LOG_FILE.name} + {LOG_MD_FILE.name}")


def _save_human_log(logs: list):
    """Render the JSON log as a human-readable markdown file. Newest first."""
    lines = ["# AI Trader — Scan Log", ""]
    lines.append("Newest scans first. Each entry shows what the scanner found and what it did.")
    lines.append(f"_Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}_")
    lines.append("")

    for entry in reversed(logs):
        ts      = entry.get("timestamp", "")[:16].replace("T", " ")
        mode    = entry.get("mode", "?")
        regime  = entry.get("spy_regime", "?")
        posture = entry.get("posture", "")
        safety  = entry.get("safety_block", "")
        vix     = entry.get("vix")
        signals = entry.get("signals") or []
        forced  = entry.get("force_closed") or []
        alerts  = entry.get("alerts") or []
        news    = entry.get("top_news") or entry.get("news_snapshot") or []

        lines.append(f"## {ts} UTC — {mode} scan")
        # Header line: regime + extras
        bits = [f"SPY: **{regime}**"]
        if posture: bits.append(f"Posture: {posture}")
        if vix is not None: bits.append(f"VIX: {vix:.1f}")
        atr  = entry.get("spy_atr_pct")
        if atr is not None: bits.append(f"SPY ATR%: {atr}")
        if safety: bits.append(f"SAFETY: {safety}")
        lines.append(" | ".join(bits))
        lines.append("")

        # Run-context (data sources, gates, account state)
        in_win   = entry.get("in_window")
        mkt_open = entry.get("market_open")
        feed     = entry.get("data_feed")
        patched  = entry.get("yfinance_patched")
        sent_bk  = entry.get("sentiment_backend")
        earn_n   = entry.get("earnings_cache_size")
        holds    = entry.get("open_positions") or []
        new_today = entry.get("new_today")
        ctx = []
        if in_win is not None:   ctx.append(f"Time window: {'IN' if in_win else 'OUT'}")
        if mkt_open is not None: ctx.append(f"Market: {'OPEN' if mkt_open else 'CLOSED'}")
        if feed:                 ctx.append(f"Data feed: {feed.upper()}")
        if patched:              ctx.append(f"yfinance vol: {patched}")
        if sent_bk:              ctx.append(f"Sentiment: {sent_bk}")
        if earn_n is not None:   ctx.append(f"Earnings dates: {earn_n} cached")
        if ctx:
            lines.append("**Run context:** " + " · ".join(ctx))
        if holds:
            lines.append(f"**Holding** ({len(holds)}): {', '.join(holds)}")
        if new_today is not None:
            lines.append(f"**New positions today:** {new_today}")
        lines.append("")

        # Sector rotation snapshot
        top3 = entry.get("sectors_top3") or []
        bot3 = entry.get("sectors_bottom3") or []
        if top3 or bot3:
            lines.append("**Sector rotation:**")
            if top3:
                lines.append("  - Hottest: " + ", ".join(
                    f"{s['sector']} ({s['ticker']}, {s['ret_20d']:+.1f}%)" for s in top3))
            if bot3:
                lines.append("  - Coldest: " + ", ".join(
                    f"{s['sector']} ({s['ticker']}, {s['ret_20d']:+.1f}%)" for s in bot3))
            lines.append("")

        # Filter funnel (where the 60 symbols got rejected)
        fs = entry.get("filter_stats") or {}
        if fs:
            lines.append("**Scan funnel:**")
            funnel_order = [
                ("scanned",            "Scanned"),
                ("no_data",            "Insufficient data"),
                ("held",               "Already holding"),
                ("market_volatile",    "Market too volatile"),
                ("earnings_blackout",  "Earnings within 5 days"),
                ("failed_filters",    "Failed price/volume/ATR filter"),
                ("obv_distribution",   "OBV in distribution"),
                ("no_strategy_signal", "No strategy fired"),
                ("of_or_sent_reject",  "Order flow / sentiment block"),
                ("sector_misalign",    "Sector misaligned"),
                ("passed_all",         "Passed all gates"),
            ]
            for k, label in funnel_order:
                v = fs.get(k)
                if v is not None and (k == "scanned" or v > 0):
                    lines.append(f"  - {label}: {v}")
            lines.append("")

        # Force-closed catalyst positions (CATALYST mode)
        if forced:
            lines.append("**Force-closed catalyst positions:**")
            for fc in forced:
                age = fc.get("age_days", "?")
                lines.append(f"- {fc.get('symbol')}: held {age}d → closed at market")
            lines.append("")

        # Signals
        if not signals:
            lines.append("_No setups found._")
        else:
            lines.append(f"**{len(signals)} signal(s):**")
            for s in signals:
                sym    = s.get("symbol", "?")
                strat  = s.get("strategy", "?")
                conf   = s.get("confidence", "")
                entry  = s.get("entry", 0)
                stop   = s.get("stop", 0)
                target = s.get("target") or s.get("2R") or 0
                shares = s.get("shares", 0)
                risk   = s.get("risk_$", 0)
                status = s.get("order_status", "")
                reason = s.get("reason", "")
                gap    = s.get("gap_pct")
                factors = s.get("factors", [])

                # Header line per signal
                head = f"- **{sym}** ({strat})"
                if conf != "": head += f" conf={conf}"
                if gap is not None: head += f" gap={gap:+.1f}%"
                lines.append(head)

                # Trade levels
                lines.append(f"    - Entry ${entry} | Stop ${stop} | Target ${target} | "
                             f"{shares} shares | Risk ${risk:.2f}")

                # Strategy reason or catalyst factors
                if factors:
                    lines.append(f"    - Factors: {', '.join(factors)}")
                if reason:
                    lines.append(f"    - {reason}")
                if status:
                    lines.append(f"    - Order: {status}")
            lines.append("")

        # Alerts (NEWS mode danger flags)
        if alerts:
            lines.append("**Alerts:**")
            for a in alerts:
                lines.append(f"- {a}")
            lines.append("")

        # Top news (FULL mode)
        if news:
            top_pos = [n for n in news if n.get("sentiment") == "POSITIVE"][:3]
            top_neg = [n for n in news if n.get("sentiment") == "NEGATIVE"][:3]
            if top_pos or top_neg:
                lines.append("<details><summary>Notable news</summary>")
                lines.append("")
                for n in top_pos:
                    lines.append(f"- ✓ **{n['symbol']}** ({n['sentiment']}, {n['score']:+d})")
                    for h in n.get("headlines", [])[:1]:
                        lines.append(f"    - {h}")
                for n in top_neg:
                    lines.append(f"- ✗ **{n['symbol']}** ({n['sentiment']}, {n['score']:+d})")
                    for h in n.get("headlines", [])[:1]:
                        lines.append(f"    - {h}")
                lines.append("")
                lines.append("</details>")
                lines.append("")

        lines.append("---")
        lines.append("")

    LOG_MD_FILE.write_text("\n".join(lines), encoding="utf-8")


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

    # Patch volume from yfinance (full SIP tape) if enabled — Alpaca's IEX
    # volume is ~3% of tape and unreliable for breakout detection.
    patched_count = 0
    if getattr(cfg, "USE_YFINANCE_VOLUME", False) and result:
        patched_count = volume_source.patch_volume(result, cfg.LOOKBACK_DAYS)
        print(f"  [yfinance] patched volume for {patched_count}/{len(result)} symbols")
    # Expose patch count via a module-level variable so save_log can record it
    globals()["_last_patch_count"] = patched_count
    globals()["_last_patch_total"] = len(result)
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
    print(f"\n{'='*60}")
    print(f"  FULL SCAN  -  {et_str}")
    print(f"{'='*60}\n")
    if not in_window:
        print(f"  [!] Out of target window (target: {FULL_TARGET_ET_HOURS} ET ±{TIME_TOLERANCE_MIN}min)")
        print(f"  Will scan and log normally, but NO ORDERS will be placed.\n")

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

    # VIX-based volatility gate
    vix_assessment = vix_gate.assess(vix_gate.get_vix(), cfg)
    print(f"  VIX assessment : {vix_assessment['reason']}\n")

    # Earnings calendar (cached weekly)
    earnings_cache = earnings_calendar.refresh_cache(cfg.WATCHLIST)

    open_positions   = get_open_positions(trade_client)
    new_today_count  = _today_new_position_count(trade_client)
    market_open, mkt_reason = is_market_open(trade_client)
    if open_positions:
        print(f"  Holding ({len(open_positions)}/{cfg.MAX_CONCURRENT_POSITIONS}): {', '.join(open_positions)}")
    print(f"  New positions today: {new_today_count}/{cfg.MAX_NEW_PER_DAY}")
    print(f"  Market status      : {mkt_reason}\n")

    # Hard caps — time-window check is now a trading gate, not a scan gate
    can_open_more = (
        in_window and
        market_open and
        len(open_positions) < cfg.MAX_CONCURRENT_POSITIONS and
        new_today_count    < cfg.MAX_NEW_PER_DAY
    )
    if not market_open:
        print(f"  Market closed — scanning for awareness only, no orders.\n")
    elif not in_window:
        print(f"  Outside target window — scanning for awareness only, no orders.\n")
    if not can_open_more:
        print(f"  Position/daily cap reached — scanning for awareness only, no new orders.\n")

    print(f"  Scanning {len(cfg.WATCHLIST)} stocks...")
    signals      = []
    news_summary = []
    # Track WHY symbols got rejected (so log shows the funnel)
    filter_stats = {
        "scanned": 0, "no_data": 0, "held": 0, "market_volatile": 0,
        "earnings_blackout": 0, "failed_filters": 0,
        "obv_distribution": 0, "no_strategy_signal": 0,
        "of_or_sent_reject": 0, "sector_misalign": 0,
        "passed_all": 0,
    }

    for symbol in cfg.WATCHLIST:
        if symbol == cfg.BENCHMARK:
            continue
        filter_stats["scanned"] += 1
        raw = all_bars.get(symbol)
        if raw is None or len(raw) < 60:
            filter_stats["no_data"] += 1
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

        if symbol in open_positions:
            filter_stats["held"] += 1
            continue
        if market_too_volatile:
            filter_stats["market_volatile"] += 1
            continue
        # Earnings blackout
        in_bo, bo_reason = earnings_calendar.in_blackout(
            symbol, earnings_cache, cfg.EARNINGS_BLACKOUT_DAYS)
        if in_bo:
            filter_stats["earnings_blackout"] += 1
            continue
        passed, _ = passes_filters(df, cfg)
        if not passed:
            filter_stats["failed_filters"] += 1
            continue

        regime     = classify(df)
        rs         = relative_strength(df, benchmark_df)
        obv_status = indicators.obv_trend(df)
        if obv_status in ("DISTRIBUTION", "STEALTH_SELL"):
            filter_stats["obv_distribution"] += 1
            continue

        signal = scan(df, regime, cfg)
        if signal is None:
            filter_stats["no_strategy_signal"] += 1
            continue
        signal["symbol"] = symbol

        of_score, of_notes = order_flow_score(df)
        if of_score < 0 or sent_score <= -2:
            filter_stats["of_or_sent_reject"] += 1
            continue

        # Sector rotation alignment — symbol's sector must have a non-negative score
        sector_score   = _sector_score(symbol, rotation)
        sector_aligned = sector_score >= 0 or sector_score == 999.0
        if cfg.REQUIRE_SECTOR_ALIGNMENT and not sector_aligned:
            filter_stats["sector_misalign"] += 1
            continue
        filter_stats["passed_all"] += 1

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
            "posture": rotation["posture"], "vix": vix_assessment.get("vix"),
            "spy_atr_pct": round(spy_atr_pct, 2),
            "in_window": in_window, "market_open": market_open,
            "data_feed": cfg.DATA_FEED,
            "yfinance_patched": f"{globals().get('_last_patch_count', 0)}/{globals().get('_last_patch_total', 0)}",
            "sentiment_backend": "finbert" if getattr(cfg, "USE_FINBERT", False) else "keyword",
            "earnings_cache_size": sum(1 for v in earnings_cache.values() if v.get("next_earnings")),
            "open_positions": sorted(open_positions),
            "new_today": new_today_count,
            "filter_stats": filter_stats,
            "sectors_top3": rotation["sectors"][:3],
            "sectors_bottom3": rotation["sectors"][-3:],
            "signals": [],
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
        # VIX gate
        if vix_assessment["block"]:
            print(f"         SKIPPED: {vix_assessment['reason']}")
            s["order_status"] = f"SKIPPED: VIX gate ({vix_assessment['reason']})"
            print()
            continue
        # Pre-trade: bid-ask spread
        ok_spread, spread_reason, _ = pretrade_check.check_spread(
            s["symbol"], data_client, cfg.MAX_BID_ASK_SPREAD_PCT)
        if not ok_spread:
            print(f"         SKIPPED: {spread_reason}")
            s["order_status"] = f"SKIPPED: {spread_reason}"
            print()
            continue
        # Pre-trade: intraday confirmation (didn't gap below signal)
        ok_gap, gap_reason, _ = pretrade_check.check_intraday_confirmation(
            s["symbol"], data_client, s["entry"], s["entry"],
            tolerance_pct=cfg.INTRADAY_GAP_TOLERANCE_PCT)
        if not ok_gap:
            print(f"         SKIPPED: {gap_reason}")
            s["order_status"] = f"SKIPPED: {gap_reason}"
            print()
            continue
        # Apply VIX size factor (halve in elevated vol)
        if vix_assessment["size_factor"] < 1.0:
            s["pos"]["shares"]   = max(1, int(s["pos"]["shares"] * vix_assessment["size_factor"]))
            s["pos"]["notional"] = s["pos"]["shares"] * s["entry"]
            print(f"         VIX size factor {vix_assessment['size_factor']} applied "
                  f"-> {s['pos']['shares']} shares")

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
        "posture": rotation["posture"], "vix": vix_assessment.get("vix"),
        "spy_atr_pct": round(spy_atr_pct, 2),
        "in_window": in_window, "market_open": market_open,
        "data_feed": cfg.DATA_FEED,
        "yfinance_patched": f"{globals().get('_last_patch_count', 0)}/{globals().get('_last_patch_total', 0)}",
        "sentiment_backend": "finbert" if getattr(cfg, "USE_FINBERT", False) else "keyword",
        "earnings_cache_size": sum(1 for v in earnings_cache.values() if v.get("next_earnings")),
        "open_positions": sorted(open_positions),
        "new_today": new_today_count,
        "filter_stats": filter_stats,
        "sectors_top3": rotation["sectors"][:3],
        "sectors_bottom3": rotation["sectors"][-3:],
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
    print(f"\n{'='*60}")
    print(f"  NEWS SCAN  -  {et_str}")
    print(f"{'='*60}\n")
    if not in_window:
        print(f"  [!] Out of target window (target: {NEWS_TARGET_ET_HOURS} ET ±{TIME_TOLERANCE_MIN}min)")
        print(f"  Will scan and log normally, but NO ORDERS will be placed.\n")

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

    # VIX gate + earnings cache
    vix_assessment = vix_gate.assess(vix_gate.get_vix(), cfg)
    print(f"  VIX: {vix_assessment['reason']}")
    if vix_assessment["block"]:
        # If VIX blocks, NEWS mode also blocks new buys
        safety_block_reason = (safety_block_reason or "") + " | " + vix_assessment["reason"]
        safety_block_reason = safety_block_reason.strip(" |")
        print(f"  SAFETY BLOCK (VIX): {vix_assessment['reason']}")
    earnings_cache = earnings_calendar.refresh_cache(cfg.WATCHLIST)

    open_positions = get_open_positions(trade_client)
    held_pnl = {}
    try:
        for p in trade_client.get_all_positions():
            held_pnl[p.symbol] = float(p.unrealized_pl)
    except Exception:
        pass

    new_today_count = _today_new_position_count(trade_client)
    market_open, mkt_reason = is_market_open(trade_client)
    print(f"  Market status: {mkt_reason}")
    can_open_more = (
        in_window and
        market_open and
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
        # Earnings blackout
        in_bo, _ = earnings_calendar.in_blackout(
            symbol, earnings_cache, cfg.EARNINGS_BLACKOUT_DAYS)
        if in_bo:
            continue
        # Pre-trade: bid-ask spread + intraday gap
        ok_spread, _, _ = pretrade_check.check_spread(
            symbol, data_client, cfg.MAX_BID_ASK_SPREAD_PCT)
        if not ok_spread:
            continue
        ok_gap, _, _ = pretrade_check.check_intraday_confirmation(
            symbol, data_client, entry, entry,
            tolerance_pct=cfg.INTRADAY_GAP_TOLERANCE_PCT)
        if not ok_gap:
            continue
        # Apply VIX size factor
        if vix_assessment["size_factor"] < 1.0:
            pos["shares"]   = max(1, int(pos["shares"] * vix_assessment["size_factor"]))
            pos["notional"] = pos["shares"] * entry

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


# ── CATALYST SCAN (event-driven, runs at 11 AM ET) ───────────────────────────

def _days_since_earnings(symbol: str, earnings_cache: dict) -> int | None:
    """Days since last earnings (0 = today/yesterday). None if unknown."""
    entry = earnings_cache.get(symbol)
    if not entry or not entry.get("next_earnings"):
        return None
    try:
        next_date = datetime.fromisoformat(entry["next_earnings"])
        if next_date.tzinfo:
            next_date = next_date.replace(tzinfo=None)
        delta = (next_date - datetime.utcnow()).days
        # If next earnings is in past, delta is negative — that's days_since
        if delta < 0:
            return -delta
        return None
    except Exception:
        return None


def _force_exit_stale_catalyst_positions(trade_client: TradingClient, force_days: int) -> list:
    """Close catalyst positions older than N trading days."""
    closed = []
    cats   = list_catalyst_positions(trade_client, cfg.CATALYST_ORDER_PREFIX)
    today  = datetime.now(ET).date()
    for c in cats:
        try:
            filled = c["filled_at"]
            if hasattr(filled, "date"):
                age_days = (today - filled.date()).days
            else:
                age_days = 0
        except Exception:
            age_days = 0
        if age_days >= force_days:
            r = close_position(c["symbol"], trade_client)
            closed.append({**c, "age_days": age_days, "result": r})
            print(f"  [force-exit] {c['symbol']} age={age_days}d -> {r['status']}")
    return closed


def run_catalyst_scan():
    in_window, et_str = in_target_window(CATALYST_TARGET_ET_HOURS)
    print(f"\n{'='*60}")
    print(f"  CATALYST SCAN  -  {et_str}")
    print(f"{'='*60}\n")
    if not in_window:
        print(f"  [!] Out of target window (target: {CATALYST_TARGET_ET_HOURS} ET ±{TIME_TOLERANCE_MIN}min)")
        print(f"  Will scan and log normally, but NO ORDERS will be placed.\n")

    data_client  = StockHistoricalDataClient(API_KEY, API_SECRET)
    news_client  = NewsClient(API_KEY, API_SECRET)
    trade_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)

    account       = trade_client.get_account()
    account_value = float(account.portfolio_value)
    buying_power  = float(account.buying_power)
    print(f"  Account value : ${account_value:,.2f}")
    print(f"  Buying power  : ${buying_power:,.2f}\n")

    # Force-exit stale catalyst positions FIRST so we free up slots & BP
    print(f"  Checking for stale catalyst positions to force-close...")
    closed = _force_exit_stale_catalyst_positions(trade_client, cfg.CATALYST_FORCE_EXIT_DAYS)
    if closed:
        print(f"  Force-closed {len(closed)} catalyst position(s)\n")

    market_open, mkt_reason = is_market_open(trade_client)
    print(f"  Market status : {mkt_reason}")
    can_trade_now = market_open and in_window
    if not market_open:
        print(f"  Market closed — will scan for awareness, no orders\n")
    elif not in_window:
        print(f"  Outside target window — will scan for awareness, no orders\n")

    # Safety gates
    vix_assessment = vix_gate.assess(vix_gate.get_vix(), cfg)
    print(f"  VIX           : {vix_assessment['reason']}")
    if vix_assessment["block"]:
        print(f"  SAFETY BLOCK — no catalyst entries\n")
        return

    # Fetch daily bars (for gap calc + volume baseline)
    all_symbols = list(set(cfg.WATCHLIST + [cfg.BENCHMARK]))
    print(f"  Fetching daily bars for {len(all_symbols)} symbols...")
    all_bars = fetch_bars(all_symbols, data_client)

    benchmark_raw = all_bars.get(cfg.BENCHMARK)
    if benchmark_raw is None:
        print("  ERROR: could not fetch SPY"); return
    benchmark_df = indicators.add_all(benchmark_raw, cfg)
    spy_regime   = classify(benchmark_df)
    print(f"  SPY regime    : {spy_regime.value}")
    if spy_regime == Regime.BEAR:
        print(f"  Skipping catalyst scan in BEAR regime\n")
        return

    earnings_cache = earnings_calendar.refresh_cache(cfg.WATCHLIST)

    open_positions  = get_open_positions(trade_client)
    new_today_count = _today_new_position_count(trade_client)
    print(f"  Holding ({len(open_positions)}/{cfg.MAX_CONCURRENT_POSITIONS}): "
          f"{', '.join(sorted(open_positions)) if open_positions else 'nothing'}")
    print(f"  New positions today: {new_today_count}/{cfg.MAX_NEW_PER_DAY}\n")

    candidates          = []
    cat_orders_this_run = 0
    remaining_bp        = buying_power

    print(f"  Scanning {len(cfg.WATCHLIST)} symbols for catalysts...\n")
    for symbol in cfg.WATCHLIST:
        if symbol in open_positions:
            continue
        if symbol == cfg.BENCHMARK:
            continue
        daily_raw = all_bars.get(symbol)
        if daily_raw is None or len(daily_raw) < 25:
            continue
        daily_df = indicators.add_all(daily_raw, cfg)

        # Fetch today's 15-min bars
        intraday_df = catalyst_detector.fetch_today_intraday(symbol, data_client)
        if intraday_df is None or intraday_df.empty:
            continue

        # News sentiment (24h window)
        news_score, _ = get_sentiment(symbol, news_client, days=1)
        earn_days_ago = _days_since_earnings(symbol, earnings_cache)

        detection = catalyst_detector.detect(
            symbol, daily_df, intraday_df, news_score, earn_days_ago, cfg)

        if detection["fires"]:
            detection["symbol"]     = symbol
            detection["news_score"] = news_score
            detection["earn_days"]  = earn_days_ago
            candidates.append(detection)
            print(f"  [CATALYST] {symbol}: {', '.join(detection['factors'])}")

    if not candidates:
        print("\n  No catalyst setups found.")
        save_log({
            "timestamp": datetime.utcnow().isoformat(),
            "mode": "CATALYST", "spy_regime": spy_regime.value,
            "signals": [], "force_closed": [{"symbol": c["symbol"]} for c in closed],
        })
        return

    # Sort by score desc, then by news_score desc
    candidates.sort(key=lambda c: (-c["score"], -c["news_score"]))

    print(f"\n  {len(candidates)} catalyst setup(s) found"
          f"{' — placing orders' if can_trade_now else ' (logging only, no orders)'}\n")
    placed   = []

    for det in candidates:
        if not can_trade_now:
            # Log the signal but don't trade
            placed.append({
                "symbol": det["symbol"], "strategy": "CATALYST", "signal": "BUY",
                "factors": det["factors"], "gap_pct": det["gap_pct"],
                "news_score": det["news_score"], "earn_days_ago": det["earn_days"],
                "order_status": "NOT_PLACED — outside target window or market closed",
            })
            continue
        if cat_orders_this_run >= cfg.CATALYST_MAX_NEW_PER_DAY:
            print(f"  Hit catalyst daily cap ({cfg.CATALYST_MAX_NEW_PER_DAY}) — stopping")
            break
        if (len(open_positions) + cat_orders_this_run) >= cfg.MAX_CONCURRENT_POSITIONS:
            print(f"  Concurrent position cap reached — stopping")
            break

        sig = strategy_catalyst.build_signal(det, account_value, cfg)
        if not sig.get("valid"):
            print(f"  {det['symbol']}: SKIP — {sig.get('reason', '')}")
            continue

        # Apply VIX size factor
        if vix_assessment["size_factor"] < 1.0:
            sig["shares"]   = max(1, int(sig["shares"] * vix_assessment["size_factor"]))
            sig["notional"] = sig["shares"] * sig["entry"]

        notional = sig["notional"]
        if notional > remaining_bp:
            print(f"  {det['symbol']}: SKIP — insufficient BP (need ${notional:,.0f}, have ${remaining_bp:,.0f})")
            continue

        symbol     = det["symbol"]
        signal_raw = {"symbol": symbol, "signal": "BUY",
                      "entry": sig["entry"], "stop": sig["stop"],
                      "target": sig["target"]}
        pos        = {"shares": sig["shares"], "notional": sig["notional"],
                      "risk_dollars": sig["risk_dollars"],
                      "target_risk": sig["target_risk"],
                      "r1_target": sig["entry"] + (sig["entry"] - sig["stop"]),
                      "r2_target": sig["target"],
                      "capped": False}
        client_id  = f"{cfg.CATALYST_ORDER_PREFIX}-{symbol}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

        result = execute(signal_raw, pos, trade_client,
                         remaining_bp=remaining_bp, client_order_id=client_id)
        print(f"  {symbol}: entry=${sig['entry']} stop=${sig['stop']} "
              f"target=${sig['target']} shares={sig['shares']} "
              f"risk=${sig['risk_dollars']:.2f} -> {result['status']}")

        placed.append({
            "symbol": symbol, "strategy": "CATALYST", "signal": "BUY",
            "entry": sig["entry"], "stop": sig["stop"], "target": sig["target"],
            "shares": sig["shares"], "notional": sig["notional"],
            "risk_$": sig["risk_dollars"], "factors": det["factors"],
            "gap_pct": det["gap_pct"], "news_score": det["news_score"],
            "earn_days_ago": det["earn_days"],
            "order_status": f"{result['status']} {result.get('order_id') or result.get('reason','')}".strip(),
            "client_order_id": client_id,
        })

        if result["status"] == "PLACED":
            remaining_bp        -= notional
            cat_orders_this_run += 1

    print(f"\n  Catalyst scan complete — {cat_orders_this_run} new order(s) placed")
    save_log({
        "timestamp": datetime.utcnow().isoformat(),
        "mode": "CATALYST", "spy_regime": spy_regime.value,
        "vix": vix_assessment.get("vix"),
        "signals": placed,
        "force_closed": [{"symbol": c["symbol"], "age_days": c["age_days"]} for c in closed],
    })


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if MODE == "NEWS":
        run_news_scan()
    elif MODE == "CATALYST":
        run_catalyst_scan()
    else:
        run_full_scan()
