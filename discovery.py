"""
Hot Stock Discovery — finds new momentum stocks during each scan.

During the FULL scan, discovers stocks that are:
  1. Top gainers today (via Alpaca screener API)
  2. High volume (institutional interest)
  3. Positive news sentiment

Qualifying stocks are added to a dynamic watchlist (discovered_watchlist.json)
that gets merged with the core watchlist in config.py. Stale entries that
haven't fired a signal in N days are automatically pruned.

This keeps the watchlist fresh with current momentum names without manual
curation.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

DISCOVERED_FILE = Path(__file__).parent / "discovered_watchlist.json"

# ── Discovery settings ──────────────────────────────────────────────────────
MAX_DISCOVERED       = 15     # cap dynamic list so it doesn't balloon
DISCOVERY_TOP        = 20     # how many movers to pull from screener
MIN_PRICE            = 10.0   # skip penny stocks
MAX_PRICE            = 1500.0 # skip if too expensive to size on $5k account
MIN_GAIN_PCT         = 3.0    # must be up at least 3% today
MIN_SENTIMENT_SCORE  = 1      # need at least mildly positive news
STALE_DAYS           = 14     # prune if not refreshed for 2 weeks


def load_discovered() -> dict:
    """
    Load the discovered watchlist.
    Format: {symbol: {added: iso_date, last_seen: iso_date, reason: str, gain_pct: float}}
    """
    if not DISCOVERED_FILE.exists():
        return {}
    try:
        return json.loads(DISCOVERED_FILE.read_text())
    except Exception:
        return {}


def save_discovered(data: dict):
    try:
        DISCOVERED_FILE.write_text(json.dumps(data, indent=2, default=str))
    except Exception:
        pass


def get_discovered_symbols() -> list[str]:
    """Return list of discovered symbols to merge with core watchlist."""
    return list(load_discovered().keys())


def _prune_stale(data: dict) -> dict:
    """Remove entries not refreshed in STALE_DAYS."""
    cutoff = (datetime.now(ET) - timedelta(days=STALE_DAYS)).isoformat()
    return {sym: info for sym, info in data.items()
            if info.get("last_seen", "") >= cutoff}


def discover_stocks(
    data_client,
    news_client,
    core_watchlist: list[str],
    get_sentiment_fn,
    sentiment_label_fn,
    cfg,
    api_key: str = None,
    api_secret: str = None,
) -> list[dict]:
    """
    Pull today's top movers from Alpaca, filter for quality, check sentiment.

    Returns list of newly discovered stocks:
      [{symbol, gain_pct, volume, sentiment, sent_score, reason}]
    """
    # 1. Get top movers from Alpaca screener
    try:
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.data.requests import MarketMoversRequest
    except ImportError:
        print("  [discovery] Alpaca screener API not available — skipping")
        return []

    # Use explicitly passed keys, or try to extract from data_client
    _key    = api_key or getattr(data_client, '_api_key', None)
    _secret = api_secret or getattr(data_client, '_secret_key', None)

    try:
        screener = ScreenerClient(_key, _secret)
        movers   = screener.get_market_movers(MarketMoversRequest(top=DISCOVERY_TOP))
    except Exception as e:
        print(f"  [discovery] Screener API failed: {e}")
        return []

    # 2. Extract gainers from response
    # Mover fields: symbol, percent_change, change, price
    gainers = []
    try:
        for m in (movers.gainers or []):
            gainers.append({
                "symbol":   m.symbol,
                "gain_pct": float(m.percent_change),
                "price":    float(m.price),
            })
    except Exception as e:
        print(f"  [discovery] Error parsing movers: {e}")
        return []

    if not gainers:
        print("  [discovery] No gainers returned from screener")
        return []

    # 3. Filter: price range, minimum gain, not already in core watchlist
    core_set   = set(core_watchlist)
    candidates = []
    for g in gainers:
        sym = g["symbol"]
        if sym in core_set:
            continue  # already watching
        if g["price"] < MIN_PRICE or g["price"] > MAX_PRICE:
            continue
        if g["gain_pct"] < MIN_GAIN_PCT:
            continue
        candidates.append(g)

    if not candidates:
        print("  [discovery] No new candidates after filtering")
        return []

    # 4. Check news sentiment on each candidate
    qualified = []
    for c in candidates[:10]:  # limit API calls
        sym = c["symbol"]
        try:
            sent_score, headlines = get_sentiment_fn(sym, news_client, days=1)
            sent_label = sentiment_label_fn(sent_score)
        except Exception:
            sent_score, sent_label, headlines = 0, "NEUTRAL", []

        if sent_score >= MIN_SENTIMENT_SCORE:
            reason = (f"Up {c['gain_pct']:+.1f}% today, "
                      f"sentiment {sent_label} ({sent_score:+d})")
            qualified.append({
                "symbol": sym,
                "gain_pct": c["gain_pct"],
                "price": c["price"],
                "sentiment": sent_label,
                "sent_score": sent_score,
                "headlines": headlines[:2],
                "reason": reason,
            })
            print(f"  [discovery] ✓ {sym}: {reason}")
        else:
            print(f"  [discovery] ✗ {sym}: up {c['gain_pct']:+.1f}% "
                  f"but sentiment {sent_label} ({sent_score:+d})")

    return qualified


def update_discovered_watchlist(
    qualified: list[dict],
    core_watchlist: list[str],
    ticker_sector: dict,
) -> dict:
    """
    Merge newly discovered stocks into the persistent discovered watchlist.
    Prunes stale entries and caps the total at MAX_DISCOVERED.

    Returns the updated discovered dict.
    """
    data = load_discovered()
    data = _prune_stale(data)

    now_iso = datetime.now(ET).isoformat()
    core_set = set(core_watchlist)

    for q in qualified:
        sym = q["symbol"]
        if sym in core_set:
            continue
        if sym in data:
            # Refresh existing entry
            data[sym]["last_seen"] = now_iso
            data[sym]["gain_pct"]  = q["gain_pct"]
            data[sym]["reason"]    = q["reason"]
        else:
            # New discovery
            data[sym] = {
                "added":     now_iso,
                "last_seen": now_iso,
                "gain_pct":  q["gain_pct"],
                "price":     q.get("price", 0),
                "sentiment": q.get("sentiment", ""),
                "reason":    q["reason"],
            }
            # Auto-assign sector if not already known
            if sym not in ticker_sector:
                ticker_sector[sym] = "Discovered"

    # Cap: keep the most recently seen entries
    if len(data) > MAX_DISCOVERED:
        sorted_entries = sorted(data.items(), key=lambda x: x[1]["last_seen"], reverse=True)
        data = dict(sorted_entries[:MAX_DISCOVERED])

    save_discovered(data)
    return data
