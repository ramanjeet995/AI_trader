"""
Swing Trading Scanner + Auto-Executor

Two modes controlled by MODE env var:

  FULL (default) — 9 AM + 4:30 PM ET
    Full pipeline: bars, sector rotation, OBV, strategy signals, auto-trade

  NEWS — 11 AM, 1 PM, 3 PM ET
    Lightweight: news sentiment only.
    Buys stocks with strong positive news (score >= 2) using ATR-based stop.
    Alerts if a held position turns negative.

Usage:
  python main.py              # FULL mode
  MODE=NEWS python main.py   # NEWS mode
"""

import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient

import config as cfg
import indicators
from market_structure import classify
from screener import passes_filters, relative_strength
from strategies import scan
from order_flow import order_flow_score
from sentiment import get_sentiment, sentiment_label
from sector_rotation import analyze as sector_analyze, print_rotation
from risk import position_size
from executor import execute, get_open_positions
from notifier import send_no_setup, send_signals, send

load_dotenv(Path(__file__).parent / ".env")

API_KEY    = os.environ["ALPACA_API_KEY"]
API_SECRET = os.environ["ALPACA_API_SECRET"]
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"
MODE       = os.getenv("MODE", "FULL").upper()

MAX_MARKET_ATR_PCT = 3.0   # pause all trading above this SPY ATR%


# ── Shared helpers ────────────────────────────────────────────────────────────

def fetch_bars(symbols: list[str], data_client) -> dict[str, pd.DataFrame]:
    end   = datetime.utcnow()
    start = end - timedelta(days=cfg.LOOKBACK_DAYS)
    req   = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed="iex",
    )
    bars = data_client.get_stock_bars(req).df
    result = {}
    if bars.empty:
        return result
    if isinstance(bars.index, pd.MultiIndex):
        for sym in bars.index.get_level_values(0).unique():
            df = bars.loc[sym].copy()
            df.index = pd.to_datetime(df.index)
            result[sym] = df
    else:
        bars.index = pd.to_datetime(bars.index)
        result[symbols[0]] = bars
    return result


def _print_news_summary(news_summary: list):
    print(f"{'='*60}")
    print(f"  MARKET NEWS SENTIMENT SUMMARY")
    print(f"{'='*60}\n")
    order = {"POSITIVE": 0, "NEUTRAL": 1, "NEGATIVE": 2}
    news_summary.sort(key=lambda x: (order.get(x["sentiment"], 1), -x["score"]))
    for n in news_summary:
        icon    = "+" if n["sentiment"] == "POSITIVE" else ("-" if n["sentiment"] == "NEGATIVE" else "~")
        rsi_str = f"  RSI {n['rsi']}" if n["rsi"] else ""
        print(f"  [{icon}] {n['symbol']:<6}  {n['sentiment']:<10}  score: {n['score']:+d}{rsi_str}")
        for h in n["headlines"]:
            print(f"        {h}")
        print()
    print(f"{'='*60}\n")


# ── FULL SCAN (9 AM + 4:30 PM) ───────────────────────────────────────────────

def run_full_scan():
    print(f"\n{'='*60}")
    print(f"  FULL SCAN  -  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    data_client  = StockHistoricalDataClient(API_KEY, API_SECRET)
    news_client  = NewsClient(API_KEY, API_SECRET)
    trade_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)

    account       = trade_client.get_account()
    account_value = float(account.portfolio_value)
    print(f"  Account value  : ${account_value:,.2f}")
    print(f"  Buying power   : ${float(account.buying_power):,.2f}")
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

    open_positions = get_open_positions(trade_client)
    if open_positions:
        print(f"  Currently holding: {', '.join(open_positions)}\n")

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
        last = df.iloc[-1]
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

        pos = position_size(account_value, signal["entry"], signal["stop"], cfg)
        signals.append({
            "symbol": symbol, "regime": regime.value, "rs": round(rs, 2),
            "obv": obv_status, "strategy": signal["strategy"], "signal": signal["signal"],
            "entry": signal["entry"], "stop": signal["stop"], "target": signal["target"],
            "shares": pos["shares"], "notional": pos["notional"], "risk_$": pos["risk_dollars"],
            "1R": pos["r1_target"], "2R": pos["r2_target"],
            "of_score": of_score, "of_notes": of_notes,
            "sentiment": sent_label, "sent_score": sent_score,
            "headlines": sent_headlines[:2], "reason": signal["reason"],
            "pos": pos, "signal_raw": signal,
        })

    print(f"\n{'-'*60}")
    if not signals:
        print("  No setups found. Stay in cash.")
        print(f"{'-'*60}\n")
        _print_news_summary(news_summary)
        send_no_setup(spy_regime.value, rotation["posture"], rotation["sectors"])
        return

    signals.sort(key=lambda x: (-x["of_score"], -x["rs"]))
    print(f"  {len(signals)} SETUP(S) FOUND — AUTO-TRADING (PAPER)")
    print(f"{'-'*60}\n")

    for s in signals:
        print(f"  {s['symbol']:<6}  Strategy {s['strategy']}  |  Regime: {s['regime']:<7}  |  RS: {s['rs']:>5.2f}  |  OBV: {s['obv']}")
        print(f"         Sentiment: {s['sentiment']:<10}  |  Order Flow: {s['of_score']:+d}/+3")
        print(f"         Entry: ${s['entry']:<9.2f} Stop: ${s['stop']:<9.2f} Target (2R): ${s['2R']:.2f}")
        print(f"         Shares: {s['shares']}   Notional: ${s['notional']:,.2f}   Risk: ${s['risk_$']:.2f}")
        print(f"         Signal: {s['reason']}")
        result = execute(s["signal_raw"], s["pos"], trade_client)
        if result["status"] == "PLACED":
            print(f"         ORDER PLACED  id={result['order_id']}")
            s["order_status"] = f"PLACED — id={result['order_id']}"
        elif result["status"] == "SKIPPED":
            print(f"         SKIPPED: {result['reason']}")
            s["order_status"] = f"SKIPPED: {result['reason']}"
        else:
            print(f"         ERROR: {result['reason']}")
            s["order_status"] = f"ERROR: {result['reason']}"
        print()

    print(f"{'-'*60}")
    print(f"  Risk per trade : {cfg.ACCOUNT_RISK_PCT*100:.0f}% = ${account_value * cfg.ACCOUNT_RISK_PCT:,.2f}")
    print(f"  Max position   : {cfg.MAX_POSITION_PCT*100:.0f}% = ${account_value * cfg.MAX_POSITION_PCT:,.2f}")
    print(f"{'-'*60}\n")

    send_signals(signals, spy_regime.value, rotation["posture"], account_value)
    _print_news_summary(news_summary)


# ── NEWS CHECK (11 AM, 1 PM, 3 PM) ───────────────────────────────────────────

def run_news_scan():
    now = datetime.now()
    print(f"\n{'='*60}")
    print(f"  NEWS SCAN  -  {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    data_client  = StockHistoricalDataClient(API_KEY, API_SECRET)
    news_client  = NewsClient(API_KEY, API_SECRET)
    trade_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)

    account       = trade_client.get_account()
    account_value = float(account.portfolio_value)
    print(f"  Account value : ${account_value:,.2f}")
    print(f"  Buying power  : ${float(account.buying_power):,.2f}\n")

    # Fetch bars for price + ATR-based stop calculation
    all_symbols = list(set(cfg.WATCHLIST + [cfg.BENCHMARK]))
    print(f"  Fetching bars for {len(all_symbols)} symbols...")
    all_bars = fetch_bars(all_symbols, data_client)

    open_positions = get_open_positions(trade_client)
    held_pnl = {}
    try:
        for p in trade_client.get_all_positions():
            held_pnl[p.symbol] = float(p.unrealized_pl)
    except Exception:
        pass

    if open_positions:
        print(f"  Holding: {', '.join(open_positions)}\n")

    bought  = []
    alerts  = []
    results = []

    for symbol in cfg.WATCHLIST:
        raw = all_bars.get(symbol)
        if raw is None or len(raw) < 20:
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

        # Alert: held stock turns negative
        if symbol in open_positions and sent_score <= -2:
            alerts.append(f"DANGER — {symbol} held position has NEGATIVE news!")
            for h in headlines[:2]:
                alerts.append(f"  {h}")

        # Buy: strong positive news, not already holding, market not closed
        if sent_score >= 2 and symbol not in open_positions:
            passed, _ = passes_filters(df, cfg)
            if not passed:
                continue

            # ATR-based stop (1.5x ATR below last close)
            entry = round(last["close"], 2)
            stop  = round(entry - last["atr"] * 1.5, 2)
            if stop <= 0 or stop >= entry:
                continue

            pos    = position_size(account_value, entry, stop, cfg)
            target = pos["r2_target"]

            signal_data = {
                "symbol": symbol, "signal": "BUY",
                "entry": entry, "stop": stop, "target": target,
            }
            result = execute(signal_data, pos, trade_client)

            status = result.get("order_id", result.get("reason", ""))
            bought.append({
                "symbol": symbol, "entry": entry, "stop": stop,
                "target": target, "shares": pos["shares"],
                "notional": pos["notional"], "risk_$": pos["risk_dollars"],
                "sentiment": label, "score": sent_score,
                "headlines": headlines[:2], "order_status": f"{result['status']} {status}".strip(),
                "regime": "N/A", "rs": 0, "obv": "N/A", "strategy": "NEWS",
                "1R": pos["r1_target"], "2R": pos["r2_target"],
                "of_score": 0, "of_notes": [], "sent_score": sent_score,
                "reason": f"News sentiment score {sent_score:+d} — strong positive headlines",
                "pos": pos, "signal_raw": signal_data,
            })
            print(f"  [BUY] {symbol}  score={sent_score:+d}  entry=${entry}  stop=${stop}  target=${target}  -> {result['status']}")

    # Print alerts
    if alerts:
        print(f"\n  {'!'*3} ALERTS {'!'*3}")
        for a in alerts:
            print(f"  {a}")
        print()

    # Print news table
    print(f"\n  {'Symbol':<7} {'Sentiment':<12} {'Score'}  {'Holding'}")
    print(f"  {'-'*40}")
    results.sort(key=lambda x: -x["score"])
    for r in results:
        icon     = "+" if r["label"] == "POSITIVE" else ("-" if r["label"] == "NEGATIVE" else "~")
        held_tag = "  [HOLDING]" if r["held"] else ""
        pnl_tag  = f"  P&L: ${r['pnl']:+.2f}" if r["pnl"] is not None else ""
        print(f"  [{icon}] {r['symbol']:<6} {r['label']:<12} {r['score']:+d}{held_tag}{pnl_tag}")
    print()

    # Email if anything happened
    if bought:
        send_signals(bought, "N/A", "N/A", account_value)
    elif alerts:
        body = "<h2>AI Trader — News Alert</h2><ul>" + \
               "".join(f"<li>{a}</li>" for a in alerts) + "</ul>"
        send(f"AI Trader ALERT ({now.strftime('%b %d %H:%M')})", body)
    else:
        print("  No significant news changes. No trades placed.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if MODE == "NEWS":
        run_news_scan()
    else:
        run_full_scan()
