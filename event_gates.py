"""
Event-based trading gates — all the "is now a safe time to trade?" checks.

Combines three concerns that share the same theme: blocking new entries when
a known market-moving event is imminent or just-released.

  1. VIX gate          — block new entries if the fear gauge is too high
  2. Earnings blackout — skip stocks reporting earnings in the next N days
  3. Macro blackout    — block trades around CPI, FOMC, NFP, PPI, PCE

All three are degradable: failure to fetch external data = no constraint
(fail-open). The pipeline never breaks because yfinance/Yahoo is down.
"""

import io
import json
import logging
import contextlib
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

try:
    import yfinance as yf
    YF_AVAILABLE = True
    # Silence yfinance HTTP 404 noise (ETFs, missing fundamentals, etc.)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("yfinance.scrapers").setLevel(logging.CRITICAL)
except ImportError:
    YF_AVAILABLE = False


ET = ZoneInfo("America/New_York")


# ─────────────────────────────────────────────────────────────────────────────
# 1) VIX GATE
# ─────────────────────────────────────────────────────────────────────────────

def get_vix() -> float | None:
    """Fetch latest VIX close from yfinance. Returns None on failure."""
    if not YF_AVAILABLE:
        return None
    try:
        end   = datetime.utcnow()
        start = end - timedelta(days=7)
        df    = yf.download("^VIX", start=start.strftime("%Y-%m-%d"),
                            end=end.strftime("%Y-%m-%d"),
                            progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        close = df["Close"]
        last  = close.iloc[-1]
        if hasattr(last, "iloc"):
            last = last.iloc[0]
        return float(last)
    except Exception:
        return None


def assess_vix(vix: float | None, cfg) -> dict:
    """
    Returns dict with: vix, block (bool), size_factor (float), reason (str).
    VIX > MAX_VIX → block all entries. VIX > halve threshold → half size.
    """
    if vix is None:
        return {"vix": None, "block": False, "size_factor": 1.0,
                "reason": "VIX unavailable — no constraint"}
    max_vix     = getattr(cfg, "MAX_VIX", 30.0)
    halve_above = getattr(cfg, "VIX_HALVE_THRESHOLD", 20.0)
    if vix > max_vix:
        return {"vix": vix, "block": True, "size_factor": 0.0,
                "reason": f"VIX {vix:.1f} > {max_vix} — no new entries"}
    if vix > halve_above:
        return {"vix": vix, "block": False, "size_factor": 0.5,
                "reason": f"VIX {vix:.1f} > {halve_above} — half size"}
    return {"vix": vix, "block": False, "size_factor": 1.0,
            "reason": f"VIX {vix:.1f} — normal"}


# ─────────────────────────────────────────────────────────────────────────────
# 2) EARNINGS BLACKOUT (per-symbol)
# ─────────────────────────────────────────────────────────────────────────────

EARNINGS_CACHE_FILE = Path(__file__).parent / "earnings_cache.json"
EARNINGS_TTL        = timedelta(days=7)
ETF_SYMBOLS = {  # No earnings — skip yfinance entirely
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLB", "XLU", "XLRE",
    "GLD", "SLV", "GDX", "GDXJ", "USO", "UNG", "DBO",
}


def _load_earnings_cache() -> dict:
    if not EARNINGS_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(EARNINGS_CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_earnings_cache(cache: dict):
    try:
        EARNINGS_CACHE_FILE.write_text(json.dumps(cache, indent=2, default=str))
    except Exception:
        pass


def _earnings_entry_fresh(entry: dict) -> bool:
    fetched = entry.get("fetched_at")
    if not fetched:
        return False
    try:
        return datetime.utcnow() - datetime.fromisoformat(fetched) < EARNINGS_TTL
    except Exception:
        return False


def _fetch_next_earnings(symbol: str) -> str | None:
    if not YF_AVAILABLE or symbol in ETF_SYMBOLS:
        return None
    try:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            t = yf.Ticker(symbol)
            cal = t.calendar
        if not cal or "Earnings Date" not in cal:
            return None
        dates = cal["Earnings Date"]
        if not dates:
            return None
        today = datetime.utcnow().date()
        future = [d for d in dates if hasattr(d, "year") and d >= today]
        return min(future).isoformat() if future else None
    except Exception:
        return None


def refresh_earnings_cache(symbols: list[str]) -> dict:
    """Refresh stale entries. Returns the (possibly updated) cache."""
    if not YF_AVAILABLE:
        return {}
    cache = _load_earnings_cache()
    updated = 0
    for sym in symbols:
        entry = cache.get(sym, {})
        if _earnings_entry_fresh(entry) and entry.get("next_earnings"):
            continue
        cache[sym] = {
            "next_earnings": _fetch_next_earnings(sym),
            "fetched_at"   : datetime.utcnow().isoformat(),
        }
        updated += 1
    if updated:
        _save_earnings_cache(cache)
        print(f"  [earnings] refreshed {updated}/{len(symbols)} symbols")
    return cache


def days_to_earnings(symbol: str, cache: dict) -> int | None:
    """Days until next earnings (None if unknown or already passed)."""
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


def in_earnings_blackout(symbol: str, cache: dict, blackout_days: int) -> tuple[bool, str]:
    """Returns (in_blackout, reason). Unknown earnings = NOT blocked."""
    days = days_to_earnings(symbol, cache)
    if days is None:
        return False, ""
    if days <= blackout_days:
        return True, f"earnings in {days}d"
    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
# 3) MACRO EVENT BLACKOUT (CPI, FOMC, NFP, PPI, PCE)
# ─────────────────────────────────────────────────────────────────────────────
# Hardcoded 2026 calendar. Update CALENDAR_2026 each January.
# Sources:
#   FOMC: federalreserve.gov/monetarypolicy/fomccalendars.htm
#   CPI/PPI: bls.gov/schedule/news_release/cpi.htm
#   NFP: first Friday of each month, 8:30 AM ET

CALENDAR_2026 = [
    # FOMC announcement days (2 PM ET)
    ("2026-01-28", "FOMC", dtime(14, 0)),
    ("2026-03-18", "FOMC", dtime(14, 0)),
    ("2026-04-29", "FOMC", dtime(14, 0)),
    ("2026-06-17", "FOMC", dtime(14, 0)),
    ("2026-07-29", "FOMC", dtime(14, 0)),
    ("2026-09-16", "FOMC", dtime(14, 0)),
    ("2026-10-28", "FOMC", dtime(14, 0)),
    ("2026-12-16", "FOMC", dtime(14, 0)),
    # CPI / PPI / NFP / PCE — all 8:30 AM ET releases
    ("2026-01-13", "CPI",  dtime(8, 30)), ("2026-01-14", "PPI",  dtime(8, 30)),
    ("2026-02-10", "CPI",  dtime(8, 30)), ("2026-02-11", "PPI",  dtime(8, 30)),
    ("2026-03-10", "CPI",  dtime(8, 30)), ("2026-03-11", "PPI",  dtime(8, 30)),
    ("2026-04-14", "CPI",  dtime(8, 30)), ("2026-04-15", "PPI",  dtime(8, 30)),
    ("2026-05-12", "CPI",  dtime(8, 30)), ("2026-05-13", "PPI",  dtime(8, 30)),
    ("2026-06-09", "CPI",  dtime(8, 30)), ("2026-06-10", "PPI",  dtime(8, 30)),
    ("2026-07-14", "CPI",  dtime(8, 30)), ("2026-07-15", "PPI",  dtime(8, 30)),
    ("2026-08-11", "CPI",  dtime(8, 30)), ("2026-08-12", "PPI",  dtime(8, 30)),
    ("2026-09-08", "CPI",  dtime(8, 30)), ("2026-09-09", "PPI",  dtime(8, 30)),
    ("2026-10-13", "CPI",  dtime(8, 30)), ("2026-10-14", "PPI",  dtime(8, 30)),
    ("2026-11-10", "CPI",  dtime(8, 30)), ("2026-11-11", "PPI",  dtime(8, 30)),
    ("2026-12-08", "CPI",  dtime(8, 30)), ("2026-12-09", "PPI",  dtime(8, 30)),
    ("2026-01-02", "NFP",  dtime(8, 30)), ("2026-02-06", "NFP",  dtime(8, 30)),
    ("2026-03-06", "NFP",  dtime(8, 30)), ("2026-04-03", "NFP",  dtime(8, 30)),
    ("2026-05-01", "NFP",  dtime(8, 30)), ("2026-06-05", "NFP",  dtime(8, 30)),
    ("2026-07-03", "NFP",  dtime(8, 30)), ("2026-08-07", "NFP",  dtime(8, 30)),
    ("2026-09-04", "NFP",  dtime(8, 30)), ("2026-10-02", "NFP",  dtime(8, 30)),
    ("2026-11-06", "NFP",  dtime(8, 30)), ("2026-12-04", "NFP",  dtime(8, 30)),
    ("2026-01-30", "PCE",  dtime(8, 30)), ("2026-02-27", "PCE",  dtime(8, 30)),
    ("2026-03-27", "PCE",  dtime(8, 30)), ("2026-04-30", "PCE",  dtime(8, 30)),
    ("2026-05-29", "PCE",  dtime(8, 30)), ("2026-06-26", "PCE",  dtime(8, 30)),
    ("2026-07-31", "PCE",  dtime(8, 30)), ("2026-08-28", "PCE",  dtime(8, 30)),
    ("2026-09-25", "PCE",  dtime(8, 30)), ("2026-10-30", "PCE",  dtime(8, 30)),
    ("2026-11-25", "PCE",  dtime(8, 30)), ("2026-12-23", "PCE",  dtime(8, 30)),
]


def _macro_events_parsed() -> list[tuple[datetime, str]]:
    out = []
    for date_str, name, release_time in CALENDAR_2026:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        dt = datetime.combine(d, release_time).replace(tzinfo=ET)
        out.append((dt, name))
    return out


def in_macro_blackout(now_et: datetime = None, hours_before: int = 24,
                      post_open_buffer_min: int = 60) -> tuple[bool, str]:
    """
    Returns (in_blackout, reason).
    BLOCKED:
      - Within `hours_before` hours BEFORE any scheduled event
      - Same day as event AND before market_open + `post_open_buffer_min`
    """
    if now_et is None:
        now_et = datetime.now(ET)
    elif now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)

    today_date  = now_et.date()
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    digest_until = market_open + timedelta(minutes=post_open_buffer_min)

    for event_dt, event_name in _macro_events_parsed():
        time_until = (event_dt - now_et).total_seconds() / 3600
        if 0 < time_until <= hours_before:
            hrs  = int(time_until)
            mins = int((time_until - hrs) * 60)
            return True, f"{event_name} releases in {hrs}h {mins}m"
        if event_dt.date() == today_date and now_et < digest_until:
            mins_after = max(0, int((now_et - market_open).total_seconds() / 60))
            return True, (f"{event_name} released today — digesting "
                          f"({mins_after}min into open, wait {post_open_buffer_min}min)")
    return False, ""


def next_macro_event(now_et: datetime = None) -> dict | None:
    """Return the next scheduled macro event, or None if calendar exhausted."""
    if now_et is None:
        now_et = datetime.now(ET)
    elif now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)
    for event_dt, name in _macro_events_parsed():
        if event_dt > now_et:
            delta = event_dt - now_et
            return {"name": name, "datetime": event_dt.isoformat(),
                    "days_away": delta.days,
                    "hours_away": round(delta.total_seconds() / 3600, 1)}
    return None
