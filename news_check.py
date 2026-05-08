"""
Mid-day news check — runs every 2 hours during market hours.
Lightweight: no technical indicators, no order placement.

What it does:
  1. Checks news sentiment for all watchlist stocks
  2. Checks open positions — alerts if held stock turns negative
  3. Flags any strong positive news on watchlist stocks worth watching
  4. Prints a compact summary (no email unless something important changed)
"""

import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from alpaca.data.historical.news import NewsClient
from alpaca.trading.client import TradingClient

import config as cfg
from sentiment import get_sentiment, sentiment_label
from notifier import send

load_dotenv(Path(__file__).parent / ".env")

API_KEY    = os.environ["ALPACA_API_KEY"]
API_SECRET = os.environ["ALPACA_API_SECRET"]
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"


def run_news_check():
    now = datetime.now()
    print(f"\n{'='*60}")
    print(f"  Mid-Day News Check  -  {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    news_client  = NewsClient(API_KEY, API_SECRET)
    trade_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)

    # Get currently held positions
    try:
        positions     = trade_client.get_all_positions()
        held_symbols  = {p.symbol: float(p.unrealized_pl) for p in positions}
    except Exception:
        held_symbols = {}

    if held_symbols:
        print(f"  Open positions: {', '.join(held_symbols.keys())}\n")
    else:
        print(f"  No open positions.\n")

    alerts       = []   # important changes — will trigger email
    all_results  = []

    for symbol in cfg.WATCHLIST:
        score, headlines = get_sentiment(symbol, news_client, days=1)
        label = sentiment_label(score)

        result = {
            "symbol"   : symbol,
            "score"    : score,
            "label"    : label,
            "headlines": headlines[:2],
            "held"     : symbol in held_symbols,
            "pnl"      : held_symbols.get(symbol),
        }
        all_results.append(result)

        # Alert conditions
        if symbol in held_symbols and score <= -2:
            alerts.append({"type": "DANGER", "symbol": symbol,
                           "msg": f"HELD position {symbol} has NEGATIVE news — consider exit",
                           "headlines": headlines[:2]})

        if symbol not in held_symbols and score >= 2:
            alerts.append({"type": "OPPORTUNITY", "symbol": symbol,
                           "msg": f"{symbol} has strong POSITIVE news — watch for setup",
                           "headlines": headlines[:2]})

    # ── Print results ─────────────────────────────────────────────────────────
    if alerts:
        print(f"  {'!'*3} ALERTS {'!'*3}\n")
        for a in alerts:
            icon = "DANGER" if a["type"] == "DANGER" else "WATCH"
            print(f"  [{icon}] {a['symbol']}: {a['msg']}")
            for h in a["headlines"]:
                print(f"          {h}")
            print()

    print(f"  {'Symbol':<7} {'Sentiment':<12} {'Score'}")
    print(f"  {'-'*35}")

    all_results.sort(key=lambda x: -x["score"])
    for r in all_results:
        held_tag = " [HOLDING]" if r["held"] else ""
        pnl_tag  = f"  P&L: ${r['pnl']:+.2f}" if r["pnl"] is not None else ""
        icon     = "+" if r["label"] == "POSITIVE" else ("-" if r["label"] == "NEGATIVE" else "~")
        print(f"  [{icon}] {r['symbol']:<6} {r['label']:<12} {r['score']:+d}{held_tag}{pnl_tag}")

    print()

    # ── Send email only if there are alerts ───────────────────────────────────
    if alerts:
        _send_alert_email(alerts, now)


def _send_alert_email(alerts: list, now: datetime):
    gmail_user = os.getenv("GMAIL_USER", "")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD", "")
    if not gmail_user or not gmail_pass:
        print("  [email] Skipping — no Gmail credentials set.")
        return

    subject = f"AI Trader ALERT - {len(alerts)} News Alert(s) ({now.strftime('%b %d %H:%M')})"

    cards = ""
    for a in alerts:
        color = "#e74c3c" if a["type"] == "DANGER" else "#f39c12"
        headlines_html = "".join(f"<li>{h}</li>" for h in a["headlines"])
        cards += f"""
        <div style='border-left:4px solid {color};padding:10px;margin-bottom:10px;background:#fafafa'>
          <b style='color:{color}'>[{a["type"]}] {a["symbol"]}</b><br>
          {a["msg"]}<br>
          <ul style='margin:6px 0;font-size:12px'>{headlines_html}</ul>
        </div>
        """

    body = f"""
    <h2 style='color:#333'>AI Trader — Mid-Day News Alert</h2>
    <p><b>Time:</b> {now.strftime('%Y-%m-%d %H:%M UTC')}</p>
    {cards}
    <p style='color:#888;font-size:11px'>Paper account — not financial advice.</p>
    """

    send(subject, body)


if __name__ == "__main__":
    run_news_check()
