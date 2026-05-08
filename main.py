"""
Swing Trading Scanner — full pipeline:
  1. Fetch daily bars for watchlist
  2. Apply technical indicators
  3. Classify market regime (Bull/Bear/Choppy)
  4. Screen by volume + ATR filters
  5. Run Strategy A / B / C signal logic
  6. Confirm with order-flow (VWAP, volume surge, CMF)
  7. Filter by news sentiment (no negative news)
  8. Size position with 1% risk rule
  9. Print actionable setups

Usage:
  python main.py
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
from market_structure import classify, Regime
from screener import passes_filters, relative_strength
from strategies import scan
from order_flow import order_flow_score
from sentiment import get_sentiment, sentiment_label
from risk import position_size

load_dotenv(Path(__file__).parent / ".env")

API_KEY    = os.environ["ALPACA_API_KEY"]
API_SECRET = os.environ["ALPACA_API_SECRET"]
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_bars(symbols: list[str], data_client) -> dict[str, pd.DataFrame]:
    end   = datetime.utcnow()
    start = end - timedelta(days=cfg.LOOKBACK_DAYS)

    req  = StockBarsRequest(
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
        sym = symbols[0]
        bars.index = pd.to_datetime(bars.index)
        result[sym] = bars

    return result


# ── News summary printer ─────────────────────────────────────────────────────

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


# ── Main scan ─────────────────────────────────────────────────────────────────

def run_scan():
    print(f"\n{'='*60}")
    print(f"  Swing Trading Scanner  -  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    data_client  = StockHistoricalDataClient(API_KEY, API_SECRET)
    news_client  = NewsClient(API_KEY, API_SECRET)
    trade_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)

    account       = trade_client.get_account()
    account_value = float(account.portfolio_value)

    print(f"  Account value : ${account_value:,.2f}")
    print(f"  Buying power  : ${float(account.buying_power):,.2f}\n")

    # Fetch all bars
    all_symbols = list(set(cfg.WATCHLIST + [cfg.BENCHMARK]))
    print(f"  Fetching bars for {len(all_symbols)} symbols...")
    all_bars = fetch_bars(all_symbols, data_client)

    benchmark_raw = all_bars.get(cfg.BENCHMARK)
    if benchmark_raw is None:
        print(f"  ERROR: could not fetch {cfg.BENCHMARK}")
        return

    benchmark_df = indicators.add_all(benchmark_raw, cfg)
    spy_regime   = classify(benchmark_df)
    print(f"  SPY regime    : {spy_regime.value}")
    print(f"  Scanning {len(cfg.WATCHLIST)} stocks...\n")

    signals      = []
    news_summary = []   # always collected regardless of technical signal

    for symbol in cfg.WATCHLIST:
        if symbol == cfg.BENCHMARK:
            continue

        raw = all_bars.get(symbol)
        if raw is None or len(raw) < 60:
            continue

        df = indicators.add_all(raw, cfg)

        # ── Always fetch news for every stock ────────────────────────────
        sent_score, sent_headlines = get_sentiment(symbol, news_client)
        sent_label = sentiment_label(sent_score)
        last = df.iloc[-1]
        news_summary.append({
            "symbol"    : symbol,
            "sentiment" : sent_label,
            "score"     : sent_score,
            "rsi"       : round(last["rsi"], 1) if not pd.isna(last["rsi"]) else None,
            "headlines" : sent_headlines[:2],
        })

        # ── 1. Liquidity / movement filter ───────────────────────────────
        passed, reason = passes_filters(df, cfg)
        if not passed:
            continue

        # ── 2. Market structure for this stock ───────────────────────────
        regime = classify(df)

        # ── 3. Relative strength vs SPY ──────────────────────────────────
        rs = relative_strength(df, benchmark_df)

        # ── 4. Strategy signal ───────────────────────────────────────────
        signal = scan(df, regime, cfg)
        if signal is None:
            continue

        # ── 5. Order-flow confirmation ───────────────────────────────────
        of_score, of_notes = order_flow_score(df)
        if of_score < 0:
            continue   # distribution pressure — skip

        # ── 6. Skip if negative news ──────────────────────────────────────
        if sent_score <= -2:
            continue

        # ── 7. Position sizing ───────────────────────────────────────────
        pos = position_size(account_value, signal["entry"], signal["stop"], cfg)

        signals.append({
            "symbol"     : symbol,
            "regime"     : regime.value,
            "rs"         : round(rs, 2),
            "strategy"   : signal["strategy"],
            "signal"     : signal["signal"],
            "entry"      : signal["entry"],
            "stop"       : signal["stop"],
            "target"     : signal["target"],
            "shares"     : pos["shares"],
            "notional"   : pos["notional"],
            "risk_$"     : pos["risk_dollars"],
            "1R"         : pos["r1_target"],
            "2R"         : pos["r2_target"],
            "of_score"   : of_score,
            "of_notes"   : of_notes,
            "sentiment"  : sent_label,
            "sent_score" : sent_score,
            "headlines"  : sent_headlines[:3],
            "reason"     : signal["reason"],
        })

    # ── Print results ─────────────────────────────────────────────────────────
    print(f"{'-'*60}")

    if not signals:
        print("  No setups found. Stay in cash — wait for better conditions.")
        print(f"{'-'*60}\n")
        _print_news_summary(news_summary)
        return

    signals.sort(key=lambda x: (-x["of_score"], -x["rs"]))

    print(f"  {len(signals)} SETUP(S) FOUND")
    print(f"{'-'*60}\n")

    for s in signals:
        print(f"  {s['symbol']:<6}  Strategy {s['strategy']}  |  Regime: {s['regime']:<7}  |  RS vs SPY: {s['rs']:>5.2f}")
        print(f"  {'':6}  Sentiment: {s['sentiment']:<10}  |  Order Flow: {s['of_score']:+d}/+3")
        print(f"  {'':6}  Entry: ${s['entry']:<9.2f}  Stop: ${s['stop']:<9.2f}  Target: ${s['target']:.2f}")
        print(f"  {'':6}  Shares: {s['shares']}   Notional: ${s['notional']:,.2f}   Risk: ${s['risk_$']:.2f}")
        print(f"  {'':6}  1R: ${s['1R']:.2f}   2R: ${s['2R']:.2f}")
        print(f"  {'':6}  Signal: {s['reason']}")

        print(f"  {'':6}  Order flow:")
        for note in s["of_notes"]:
            print(f"  {'':10}  {note}")

        if s["headlines"]:
            print(f"  {'':6}  Recent news:")
            for h in s["headlines"]:
                print(f"  {'':10}  {h}")
        print()

    print(f"{'-'*60}")
    print(f"  Risk per trade : {cfg.ACCOUNT_RISK_PCT*100:.0f}% = ${account_value * cfg.ACCOUNT_RISK_PCT:,.2f}")
    print(f"  Max position   : {cfg.MAX_POSITION_PCT*100:.0f}% = ${account_value * cfg.MAX_POSITION_PCT:,.2f}")
    print(f"{'-'*60}\n")

    _print_news_summary(news_summary)


if __name__ == "__main__":
    run_scan()
