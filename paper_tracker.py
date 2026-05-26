"""
Paper Tracker — virtual portfolio for sentiment-driven stock picks.

Watches for stocks that *might* blow up based on news/sentiment, records
a virtual entry, and tracks what would have happened if we bought.

This does NOT place real orders. It validates whether sentiment-driven
signals have edge before we risk real money.

Detection criteria (any ONE is enough to flag):
  1. Strong positive sentiment (FinBERT score >= +2)
  2. Hype news detected (buy-the-rumor anticipatory headlines)
  3. M&A target (takeover = premium over market price)
  4. Analyst upgrade from reputable firm (analyst_score >= +2)
  5. Momentum surge: 5-day return > 8% with positive sentiment

Tracking:
  - Records entry price at detection
  - Tracks peak price, current price, days held
  - After 5 trading days, marks as "expired" with final P&L
  - Keeps history of all picks for hit-rate analysis

Data file: paper_portfolio.json
"""

import json
from datetime import datetime
from pathlib import Path

PAPER_FILE = Path(__file__).parent / "paper_portfolio.json"
MAX_TRACK_DAYS = 5       # track for 5 trading days then expire
MAX_HISTORY    = 200     # keep last 200 expired picks for analysis


def _load() -> dict:
    if not PAPER_FILE.exists():
        return {"active": [], "history": []}
    try:
        data = json.loads(PAPER_FILE.read_text())
        if "active" not in data:
            data["active"] = []
        if "history" not in data:
            data["history"] = []
        return data
    except Exception:
        return {"active": [], "history": []}


def _save(data: dict):
    data["history"] = data["history"][-MAX_HISTORY:]
    PAPER_FILE.write_text(json.dumps(data, indent=2, default=str))


def detect_candidates(
    watchlist_data: list[dict],
    held_symbols: set,
    active_symbols: set,
) -> list[dict]:
    """
    Scan sentiment/news data and flag stocks that might blow up.

    watchlist_data: list of dicts with keys:
      symbol, sent_score, sent_label, analyst_score, news_phase,
      ma_role, ret_5d (optional), close_price, headlines

    Returns list of new candidates to track.
    """
    candidates = []

    for stock in watchlist_data:
        symbol = stock.get("symbol", "")
        if not symbol:
            continue
        # Skip if already holding or already tracking
        if symbol in held_symbols or symbol in active_symbols:
            continue

        sent_score = stock.get("sent_score", 0)
        analyst_score = stock.get("analyst_score", 0)
        news_phase = stock.get("news_phase", "neutral")
        ma_role = stock.get("ma_role", "none")
        ret_5d = stock.get("ret_5d", 0)
        close_price = stock.get("close_price", 0)

        if close_price <= 0:
            continue

        reasons = []

        # Criterion 1: Strong positive sentiment
        if sent_score >= 2:
            reasons.append(f"strong sentiment (+{sent_score})")

        # Criterion 2: Hype news (anticipatory catalyst)
        if news_phase == "hype":
            reasons.append("hype news detected (buy-the-rumor)")

        # Criterion 3: M&A target
        if ma_role == "target":
            reasons.append("M&A takeover target")

        # Criterion 4: Analyst upgrade
        if analyst_score >= 2:
            reasons.append(f"analyst upgrade (+{analyst_score})")

        # Criterion 5: Momentum surge with positive sentiment
        if ret_5d > 8 and sent_score >= 1:
            reasons.append(f"momentum surge (+{ret_5d:.1f}% in 5d)")

        if reasons:
            candidates.append({
                "symbol": symbol,
                "entry_price": round(close_price, 2),
                "entry_date": datetime.utcnow().isoformat(),
                "reasons": reasons,
                "sent_score": sent_score,
                "analyst_score": analyst_score,
                "news_phase": news_phase,
                "ma_role": ma_role,
                "ret_5d": round(ret_5d, 1) if ret_5d else 0,
                "peak_price": round(close_price, 2),
                "peak_pct": 0.0,
                "current_price": round(close_price, 2),
                "current_pct": 0.0,
                "days_tracked": 0,
                "headlines": stock.get("headlines", [])[:2],
            })

    return candidates


def update_tracking(price_lookup: dict) -> dict:
    """
    Update all active paper picks with current prices.
    Expire picks older than MAX_TRACK_DAYS.

    price_lookup: {symbol: current_price}

    Returns summary dict for logging.
    """
    data = _load()
    expired = []
    still_active = []

    for pick in data["active"]:
        symbol = pick["symbol"]
        current = price_lookup.get(symbol)
        if current is None or current <= 0:
            still_active.append(pick)
            continue

        pick["days_tracked"] += 1
        pick["current_price"] = round(current, 2)
        pick["current_pct"] = round(((current / pick["entry_price"]) - 1) * 100, 2)

        if current > pick["peak_price"]:
            pick["peak_price"] = round(current, 2)
            pick["peak_pct"] = round(((current / pick["entry_price"]) - 1) * 100, 2)

        if pick["days_tracked"] >= MAX_TRACK_DAYS:
            pick["status"] = "expired"
            pick["final_pct"] = pick["current_pct"]
            expired.append(pick)
        else:
            still_active.append(pick)

    # Move expired to history
    data["history"].extend(expired)
    data["active"] = still_active
    _save(data)

    return {
        "active_count": len(still_active),
        "expired_count": len(expired),
        "expired": expired,
    }


def add_candidates(candidates: list[dict]) -> int:
    """Add new candidates to active tracking. Returns count added."""
    if not candidates:
        return 0
    data = _load()
    active_symbols = {p["symbol"] for p in data["active"]}
    added = 0
    for c in candidates:
        if c["symbol"] not in active_symbols:
            data["active"].append(c)
            active_symbols.add(c["symbol"])
            added += 1
    _save(data)
    return added


def get_active_symbols() -> set:
    data = _load()
    return {p["symbol"] for p in data["active"]}


def get_active() -> list[dict]:
    data = _load()
    return data["active"]


def get_stats() -> dict:
    """Compute hit rate and average return from history."""
    data = _load()
    history = data["history"]
    if not history:
        return {
            "total_picks": 0, "winners": 0, "losers": 0,
            "hit_rate": 0, "avg_return": 0, "avg_peak": 0,
            "best_pick": None, "worst_pick": None,
        }

    winners = [h for h in history if h.get("final_pct", 0) > 0]
    losers = [h for h in history if h.get("final_pct", 0) <= 0]
    avg_return = sum(h.get("final_pct", 0) for h in history) / len(history)
    avg_peak = sum(h.get("peak_pct", 0) for h in history) / len(history)

    best = max(history, key=lambda h: h.get("peak_pct", 0))
    worst = min(history, key=lambda h: h.get("final_pct", 0))

    return {
        "total_picks": len(history),
        "winners": len(winners),
        "losers": len(losers),
        "hit_rate": round(len(winners) / len(history) * 100, 1),
        "avg_return": round(avg_return, 2),
        "avg_peak": round(avg_peak, 2),
        "best_pick": {"symbol": best["symbol"], "peak_pct": best.get("peak_pct", 0)},
        "worst_pick": {"symbol": worst["symbol"], "final_pct": worst.get("final_pct", 0)},
    }
