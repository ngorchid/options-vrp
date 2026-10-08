"""US market hours for the runner (2026-10-08, owner's decision on #16).

ET SLOT. The Windows box runs on CET and Task Scheduler has no per-task US time zone, so a fixed
21:30 local start is 15:30 ET only while Europe and the US are both on summer or both on winter
time; in the daylight-saving gap weeks it is 16:30 ET, after the close. The scheduled tasks
therefore start at 20:30 AND 21:30 local and pass `--et-slot 15:30`: the runner proceeds only
within SLOT_TOLERANCE_MIN of 15:30 New York time, so exactly one of the two starts runs each day.

MARKET-OPEN GUARD. Before opening new spreads the runner asks whether the US market is open now:
IB's own trading hours for the exchange today (`liquidHours`, which knows holidays and early
closes), else the clock (09:30-16:00 ET, Monday-Friday). Closed or unknown -> no new spread is
opened and a WARNING says so. Closes and the assignment unwind are never affected: no guard may
block a close.
"""
from __future__ import annotations

import pandas as pd

ET = "America/New_York"
SLOT_TOLERANCE_MIN = 20
REFERENCE = "SPY"            # the exchange calendar is read off a liquid US listing


def et_now(now: pd.Timestamp | None = None) -> pd.Timestamp:
    """`now` (tz-aware, any zone) or the current time, in New York time."""
    t = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if t.tzinfo is None:
        raise ValueError("et_now needs a timezone-aware timestamp")
    return t.tz_convert(ET)


def in_et_slot(slot: str, now: pd.Timestamp | None = None,
               tolerance_min: int = SLOT_TOLERANCE_MIN) -> bool:
    """True if New York time is within `tolerance_min` of `slot` ("HH:MM") today."""
    t = et_now(now)
    hh, mm = (int(x) for x in slot.split(":"))
    target = t.normalize() + pd.Timedelta(hours=hh, minutes=mm)
    return abs((t - target).total_seconds()) <= tolerance_min * 60


def parse_liquid_hours(spec: str, tz: str, now: pd.Timestamp) -> bool | None:
    """Open now per IB's `liquidHours` string, or None if today is not listed / unparseable.

    Format (IB): sessions separated by ';', each "YYYYMMDD:HHMM-YYYYMMDD:HHMM" or
    "YYYYMMDD:CLOSED", in the contract's time zone `tz` (e.g. "US/Eastern")."""
    if not spec:
        return None
    try:
        t = pd.Timestamp(now).tz_convert(tz)
    except Exception:  # noqa: BLE001
        return None
    today = t.strftime("%Y%m%d")
    seen_today = False
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        if part.endswith(":CLOSED"):
            if part.split(":")[0] == today:
                seen_today = True
            continue
        try:
            a, b = part.split("-")
            start = pd.Timestamp(pd.to_datetime(a, format="%Y%m%d:%H%M")).tz_localize(tz)
            end = pd.Timestamp(pd.to_datetime(b, format="%Y%m%d:%H%M")).tz_localize(tz)
        except Exception:  # noqa: BLE001
            return None
        if start.strftime("%Y%m%d") == today or end.strftime("%Y%m%d") == today:
            seen_today = True
        if start <= t < end:
            return True
    return False if seen_today else None


def clock_open(now: pd.Timestamp | None = None) -> bool:
    """Regular US hours by the clock alone: Monday-Friday 09:30-16:00 New York time. Knows no
    holidays or early closes -- the fallback when IB's calendar cannot be read."""
    t = et_now(now)
    if t.weekday() >= 5:
        return False
    minutes = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def market_open(broker, now: pd.Timestamp | None = None) -> tuple[bool, str]:
    """(open, how it was decided). IB's calendar first, the clock as the fallback."""
    t = et_now(now)
    info = None
    try:
        info = broker.liquid_hours(REFERENCE)
    except Exception:  # noqa: BLE001 -- a calendar read must never break the run
        info = None
    if info:
        spec, tz = info
        r = parse_liquid_hours(spec, tz or "US/Eastern", t)
        if r is not None:
            return r, f"IB trading hours for {REFERENCE}, {t:%Y-%m-%d %H:%M} ET"
    r = clock_open(t)
    return r, f"clock fallback (IB calendar unavailable), {t:%a %Y-%m-%d %H:%M} ET"
