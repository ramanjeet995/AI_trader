"""
Macro economic event calendar — blocks new trades around scheduled releases
that routinely cause gap risk (CPI, FOMC, NFP, etc.).

Policy:
  - BLOCKED: 24 hours before any major event
  - BLOCKED: event day, before market_open + 60 min (let market digest)
  - ALLOWED: 60+ min after market open on event day (reaction trade window)
  - ALLOWED: Day +1 onwards (post-event drift)

Calendar data is hardcoded for transparency (no external dependency, no API
rate-limits). FOMC dates are pre-announced by the Fed annually. NFP is always
the first Friday. CPI/PPI dates are released by BLS ~2 months ahead.

Update CALENDAR_2026 each January for the new year's known dates.
"""

from datetime import datetime, timedelta, time as dtime

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


# ── Hardcoded 2026 event calendar ─────────────────────────────────────────────
# Format: (date, event_name, release_time_et)
# Sources:
#   FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
#   CPI/PPI: https://www.bls.gov/schedule/news_release/cpi.htm
#   NFP: first Friday of each month, 8:30 AM ET

CALENDAR_2026 = [
    # FOMC meeting days (2-day meetings, announcement = 2nd day at 2 PM ET)
    ("2026-01-28", "FOMC", dtime(14, 0)),
    ("2026-03-18", "FOMC", dtime(14, 0)),
    ("2026-04-29", "FOMC", dtime(14, 0)),
    ("2026-06-17", "FOMC", dtime(14, 0)),
    ("2026-07-29", "FOMC", dtime(14, 0)),
    ("2026-09-16", "FOMC", dtime(14, 0)),
    ("2026-10-28", "FOMC", dtime(14, 0)),
    ("2026-12-16", "FOMC", dtime(14, 0)),

    # CPI release dates (8:30 AM ET — before market open)
    ("2026-01-13", "CPI",  dtime(8, 30)),
    ("2026-02-10", "CPI",  dtime(8, 30)),
    ("2026-03-10", "CPI",  dtime(8, 30)),
    ("2026-04-14", "CPI",  dtime(8, 30)),
    ("2026-05-12", "CPI",  dtime(8, 30)),
    ("2026-06-09", "CPI",  dtime(8, 30)),
    ("2026-07-14", "CPI",  dtime(8, 30)),
    ("2026-08-11", "CPI",  dtime(8, 30)),
    ("2026-09-08", "CPI",  dtime(8, 30)),
    ("2026-10-13", "CPI",  dtime(8, 30)),
    ("2026-11-10", "CPI",  dtime(8, 30)),
    ("2026-12-08", "CPI",  dtime(8, 30)),

    # PPI (usually day after CPI, also 8:30 AM ET)
    ("2026-01-14", "PPI",  dtime(8, 30)),
    ("2026-02-11", "PPI",  dtime(8, 30)),
    ("2026-03-11", "PPI",  dtime(8, 30)),
    ("2026-04-15", "PPI",  dtime(8, 30)),
    ("2026-05-13", "PPI",  dtime(8, 30)),
    ("2026-06-10", "PPI",  dtime(8, 30)),
    ("2026-07-15", "PPI",  dtime(8, 30)),
    ("2026-08-12", "PPI",  dtime(8, 30)),
    ("2026-09-09", "PPI",  dtime(8, 30)),
    ("2026-10-14", "PPI",  dtime(8, 30)),
    ("2026-11-11", "PPI",  dtime(8, 30)),
    ("2026-12-09", "PPI",  dtime(8, 30)),

    # NFP / Jobs report (first Friday of each month, 8:30 AM ET)
    ("2026-01-02", "NFP",  dtime(8, 30)),
    ("2026-02-06", "NFP",  dtime(8, 30)),
    ("2026-03-06", "NFP",  dtime(8, 30)),
    ("2026-04-03", "NFP",  dtime(8, 30)),
    ("2026-05-01", "NFP",  dtime(8, 30)),
    ("2026-06-05", "NFP",  dtime(8, 30)),
    ("2026-07-03", "NFP",  dtime(8, 30)),
    ("2026-08-07", "NFP",  dtime(8, 30)),
    ("2026-09-04", "NFP",  dtime(8, 30)),
    ("2026-10-02", "NFP",  dtime(8, 30)),
    ("2026-11-06", "NFP",  dtime(8, 30)),
    ("2026-12-04", "NFP",  dtime(8, 30)),

    # PCE inflation (Fed's preferred gauge, monthly, ~last Friday of month)
    ("2026-01-30", "PCE",  dtime(8, 30)),
    ("2026-02-27", "PCE",  dtime(8, 30)),
    ("2026-03-27", "PCE",  dtime(8, 30)),
    ("2026-04-30", "PCE",  dtime(8, 30)),
    ("2026-05-29", "PCE",  dtime(8, 30)),
    ("2026-06-26", "PCE",  dtime(8, 30)),
    ("2026-07-31", "PCE",  dtime(8, 30)),
    ("2026-08-28", "PCE",  dtime(8, 30)),
    ("2026-09-25", "PCE",  dtime(8, 30)),
    ("2026-10-30", "PCE",  dtime(8, 30)),
    ("2026-11-25", "PCE",  dtime(8, 30)),
    ("2026-12-23", "PCE",  dtime(8, 30)),
]


def _parsed_events() -> list[tuple[datetime, str]]:
    """Convert CALENDAR_2026 strings into timezone-aware ET datetimes."""
    out = []
    for date_str, name, release_time in CALENDAR_2026:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        dt = datetime.combine(d, release_time).replace(tzinfo=ET)
        out.append((dt, name))
    return out


def check_blackout(now_et: datetime = None, hours_before: int = 24,
                   post_open_buffer_min: int = 60) -> tuple[bool, str]:
    """
    Returns (in_blackout, reason).

    BLOCKED if:
      - Now is within `hours_before` hours BEFORE any scheduled event
      - Now is on the event date AND before market_open + `post_open_buffer_min`

    ALLOWED if:
      - Past market_open + 60min on event day (digest window over)
      - Day +1 or later
    """
    if now_et is None:
        now_et = datetime.now(ET)
    elif now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)

    today_date = now_et.date()
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    digest_until = market_open + timedelta(minutes=post_open_buffer_min)

    for event_dt, event_name in _parsed_events():
        # Pre-event window (24 hours before release)
        time_until = (event_dt - now_et).total_seconds() / 3600
        if 0 < time_until <= hours_before:
            hrs = int(time_until)
            mins = int((time_until - hrs) * 60)
            return True, f"{event_name} releases in {hrs}h {mins}m"

        # Same-day post-event but before digest window ends
        if event_dt.date() == today_date and now_et < digest_until:
            mins_after_open = int((now_et - market_open).total_seconds() / 60)
            if mins_after_open < 0:
                mins_after_open = 0
            return True, (f"{event_name} released today — digesting "
                          f"({mins_after_open}min into open, wait {post_open_buffer_min}min)")

    return False, ""


def next_event(now_et: datetime = None) -> dict | None:
    """Return the next scheduled event, or None if calendar is exhausted."""
    if now_et is None:
        now_et = datetime.now(ET)
    elif now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)
    for event_dt, name in _parsed_events():
        if event_dt > now_et:
            delta = event_dt - now_et
            return {
                "name": name,
                "datetime": event_dt.isoformat(),
                "days_away": delta.days,
                "hours_away": round(delta.total_seconds() / 3600, 1),
            }
    return None
