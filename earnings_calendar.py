"""
Earnings blackout filter — skip signals near earnings to avoid gap risk.

Earnings gaps blow through technical stops ~30% of the time. Even a perfect
swing setup is bad EV the day before earnings.

Source: yfinance (free, full coverage). Cached in earnings_cache.json so we
don't re-hit Yahoo on every run; refreshed weekly.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False


CACHE_FILE   = Path(__file__).parent / "earnings_cache.json"
CACHE_TTL    = timedelta(days=7)


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict):
    try:
        CACHE_FILE.write_text(json.dumps(cache, indent=2, default=str))
    except Exception:
        pass


def _is_fresh(entry: dict) -> bool:
    fetched = entry.get("fetched_at")
    if not fetched:
        return False
    try:
        return datetime.utcnow() - datetime.fromisoformat(fetched) < CACHE_TTL
    except Exception:
        return False


def _fetch_next_earnings(symbol: str) -> str | None:
    """Return next earnings date as ISO string, or None if unknown."""
    if not YF_AVAILABLE:
        return None
    try:
        t = yf.Ticker(symbol)
        # .calendar is a dict; 'Earnings Date' is a list of date(s).
        # Falls back gracefully for ETFs (no earnings) — returns None.
        cal = t.calendar
        if not cal or "Earnings Date" not in cal:
            return None
        dates = cal["Earnings Date"]
        if not dates:
            return None
        # Pick the earliest future date
        today = datetime.utcnow().date()
        future = [d for d in dates if hasattr(d, "year") and d >= today]
        if not future:
            return None
        return min(future).isoformat()
    except Exception:
        return None


def refresh_cache(symbols: list[str]) -> dict:
    """Refresh stale entries. Returns the (possibly updated) cache."""
    if not YF_AVAILABLE:
        return {}
    cache = _load_cache()
    updated = 0
    for sym in symbols:
        entry = cache.get(sym, {})
        if _is_fresh(entry):
            continue
        next_earn = _fetch_next_earnings(sym)
        cache[sym] = {
            "next_earnings": next_earn,
            "fetched_at"   : datetime.utcnow().isoformat(),
        }
        updated += 1
    if updated:
        _save_cache(cache)
        print(f"  [earnings] refreshed {updated}/{len(symbols)} symbols")
    return cache


def days_to_earnings(symbol: str, cache: dict) -> int | None:
    """Days until next earnings, or None if unknown."""
    entry = cache.get(symbol)
    if not entry or not entry.get("next_earnings"):
        return None
    try:
        next_date = datetime.fromisoformat(entry["next_earnings"])
        if next_date.tzinfo:
            next_date = next_date.replace(tzinfo=None)
        delta = (next_date - datetime.utcnow()).days
        return delta if delta >= 0 else None
    except Exception:
        return None


def in_blackout(symbol: str, cache: dict, blackout_days: int) -> tuple[bool, str]:
    """Returns (in_blackout, reason). Unknown earnings = NOT blocked."""
    days = days_to_earnings(symbol, cache)
    if days is None:
        return False, ""
    if days <= blackout_days:
        return True, f"earnings in {days}d"
    return False, ""
