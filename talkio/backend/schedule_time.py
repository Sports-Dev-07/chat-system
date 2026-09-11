"""Time maths for Sportstech's agent scheduler.

Deliberately dependency-free and side-effect-free: no DB, no network, no globals.
Everything here is a pure function of its arguments, so the runner in scheduler.py
stays trivial and this part can be unit-tested on its own.

Design notes
------------
* Schedules are stored as a wall-clock time (hour/minute) plus an IANA timezone,
  NOT as a fixed UTC offset. "09:00 Europe/Berlin" must stay 09:00 for the user
  across DST changes, which a stored offset cannot do.
* `next_run_at` is persisted as a UTC epoch int so the "what's due?" query is a
  plain integer comparison and never depends on the server's local timezone.
* DST edge cases are handled explicitly:
    - spring forward: a wall time that does not exist that day (e.g. 02:30 on a
      day the clock jumps 02:00 -> 03:00) runs at the instant the clock reaches
      the requested time, i.e. right after the jump.
    - fall back: an ambiguous wall time that happens twice runs on the FIRST
      occurrence (fold=0), so it fires once per day, never twice.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "UTC"
# A run this far past due is treated as a missed run rather than normal jitter.
MISSED_GRACE_SECONDS = 120

# Windows ships no system tz database, so even ZoneInfo("UTC") raises there unless
# the `tzdata` package is installed. datetime.timezone.utc is pure stdlib and always
# works, so it's the last-resort fallback — the scheduler must never 500 over this.
_UTC = timezone.utc
TZDATA_OK = True
try:
    ZoneInfo(DEFAULT_TZ)
except Exception:                       # pragma: no cover - environment dependent
    TZDATA_OK = False
    print("[scheduler] WARNING: no timezone database found (`pip install tzdata`). "
          "All schedules will run in UTC until it is installed.", flush=True)


# ------------------------------------------------------------------ helpers

def resolve_tz(name: str | None):
    """Never raises. A typo in one schedule — or a missing tz database on the whole
    machine — must not take down the scheduler loop or a request."""
    wanted = (name or DEFAULT_TZ).strip() or DEFAULT_TZ
    try:
        return ZoneInfo(wanted)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pass
    try:
        return ZoneInfo(DEFAULT_TZ)
    except Exception:
        return _UTC                     # no tz database at all


def tz_label(tz) -> str:
    """Display name for a tz object; timezone.utc has no .key."""
    return getattr(tz, "key", None) or "UTC"


def parse_hhmm(value: str) -> tuple[int, int]:
    """'09:30' / '9:30' / '0930' -> (9, 30). Raises ValueError on anything else."""
    s = (value or "").strip()
    if ":" in s:
        hh, _, mm = s.partition(":")
    elif len(s) == 4 and s.isdigit():
        hh, mm = s[:2], s[2:]
    else:
        raise ValueError("Time must look like HH:MM")
    try:
        hour, minute = int(hh), int(mm)
    except ValueError:
        raise ValueError("Time must look like HH:MM") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Time must be between 00:00 and 23:59")
    return hour, minute


def _to_epoch(local_naive: datetime, tz) -> int:
    """Convert a naive wall-clock time in `tz` to a UTC epoch, resolving DST gaps
    and overlaps deterministically."""
    aware = local_naive.replace(tzinfo=tz, fold=0)   # fold=0 => first of an ambiguous pair
    epoch = aware.timestamp()

    # Spring-forward gap: the requested wall time never occurs. Round-tripping the
    # epoch back to local time yields a DIFFERENT wall time, which is the tell.
    back = datetime.fromtimestamp(epoch, tz)
    if (back.hour, back.minute) != (local_naive.hour, local_naive.minute):
        # Walk forward minute by minute to the first instant at/after the request.
        # The gap is at most a couple of hours, so this terminates quickly.
        probe = local_naive
        for _ in range(4 * 60):
            probe += timedelta(minutes=1)
            cand = probe.replace(tzinfo=tz, fold=0).timestamp()
            got = datetime.fromtimestamp(cand, tz)
            if (got.hour, got.minute) == (probe.hour, probe.minute):
                return int(cand)
    return int(epoch)


# ------------------------------------------------------------------ scheduling

def next_daily_run(hour: int, minute: int, tz_name: str, after: float | None = None) -> int:
    """UTC epoch of the next occurrence of `hour:minute` in `tz_name`, strictly
    after `after` (defaults to now). Used both to arm a new schedule and to
    re-arm one after it fires."""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("hour must be 0-23 and minute 0-59")
    tz = resolve_tz(tz_name)
    after = time.time() if after is None else float(after)

    local_now = datetime.fromtimestamp(after, tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Try today, then walk forward. The loop (rather than a single +1 day) covers
    # the case where today's slot lands in a DST gap and gets pushed past `after`.
    for _ in range(4):
        epoch = _to_epoch(candidate, tz)
        if epoch > after:
            return epoch
        candidate += timedelta(days=1)
    raise RuntimeError("could not find a next run slot")   # unreachable in practice


# Days of the week as stored: 0 = Monday, matching datetime.weekday().
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]

KINDS = ("daily", "weekly", "weekdays", "hourly", "interval")


def parse_days(value) -> list[int]:
    """Accept 0-6 ints, names, or a comma string. Returns sorted unique days."""
    if value is None or value == "":
        return []
    if isinstance(value, int):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [p for p in re.split(r"[,\s]+", str(value)) if p]
    out = set()
    for item in raw:
        if isinstance(item, int) or str(item).strip().isdigit():
            n = int(item)
            if 0 <= n <= 6:
                out.add(n)
            continue
        name = str(item).strip().lower()[:3]
        for i, full in enumerate(WEEKDAY_NAMES):
            if full.lower().startswith(name):
                out.add(i)
                break
    return sorted(out)


def next_weekly_run(hour: int, minute: int, tz_name: str, days,
                    after: float | None = None) -> int:
    """Next `hour:minute` on one of `days` (0=Monday). Empty days means every day,
    so a misconfigured weekly schedule still runs rather than silently never
    firing."""
    wanted = parse_days(days)
    if not wanted:
        return next_daily_run(hour, minute, tz_name, after)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("hour must be 0-23 and minute 0-59")

    tz = resolve_tz(tz_name)
    after = time.time() if after is None else float(after)
    local_now = datetime.fromtimestamp(after, tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Up to 8 days covers "later today" plus a full week, including a slot that
    # lands in a DST gap and gets pushed past `after`.
    for _ in range(9):
        if candidate.weekday() in wanted:
            epoch = _to_epoch(candidate, tz)
            if epoch > after:
                return epoch
        candidate += timedelta(days=1)
    raise RuntimeError("could not find a next weekly slot")


def next_interval_run(minutes: int, after: float | None = None,
                      minute_offset: int = 0) -> int:
    """Next slot on a fixed interval, aligned to the clock.

    Deliberately computed from wall-clock UTC rather than by adding to the last
    run: that keeps an hourly job at :00 instead of drifting a little later on
    every fire, and it needs no timezone handling because an interval means the
    same length everywhere — including across a DST change.
    """
    step = max(1, int(minutes)) * 60
    after = time.time() if after is None else float(after)
    offset = (int(minute_offset) % max(1, int(minutes))) * 60
    base = int(after) - ((int(after) - offset) % step)
    nxt = base + step
    while nxt <= after:
        nxt += step
    return int(nxt)


def is_due(next_run_at: int | float | None, now: float | None = None) -> bool:
    now = time.time() if now is None else float(now)
    return next_run_at is not None and float(next_run_at) <= now


def next_run_for(kind: str, hour: int, minute: int, tz_name: str,
                 days=None, every_minutes: int = 60,
                 after: float | None = None) -> int:
    """One entry point for every schedule kind, so callers don't branch."""
    k = (kind or "daily").lower()
    if k == "weekly":
        return next_weekly_run(hour, minute, tz_name, days, after)
    if k == "weekdays":
        return next_weekly_run(hour, minute, tz_name, [0, 1, 2, 3, 4], after)
    if k == "hourly":
        return next_interval_run(60, after, minute)
    if k == "interval":
        return next_interval_run(every_minutes, after, minute)
    return next_daily_run(hour, minute, tz_name, after)


def plan_run(hour: int, minute: int, tz_name: str, next_run_at: int | float | None,
             now: float | None = None, kind: str = "daily",
             days=None, every_minutes: int = 60) -> dict:
    """Decide what to do with a schedule the loop just picked up.

    Returns {"run": bool, "missed": bool, "next_run_at": int}.

    `missed` is True when the slot passed while the server was down. Missed runs
    COALESCE: five days offline produce one catch-up run, not five — the next
    slot is always computed from now, never from the stale timestamp.
    """
    now = time.time() if now is None else float(now)
    if next_run_at is None:                       # freshly created / never armed
        return {"run": False, "missed": False,
                "next_run_at": next_run_for(kind, hour, minute, tz_name,
                                            days, every_minutes, now)}
    if not is_due(next_run_at, now):
        return {"run": False, "missed": False, "next_run_at": int(next_run_at)}
    return {
        "run": True,
        "missed": (now - float(next_run_at)) > MISSED_GRACE_SECONDS,
        "next_run_at": next_run_for(kind, hour, minute, tz_name,
                                    days, every_minutes, now),
    }


def describe(hour: int, minute: int, tz_name: str, kind: str = "daily",
             days=None, every_minutes: int = 60) -> str:
    k = (kind or "daily").lower()
    label = tz_label(resolve_tz(tz_name))
    if k == "hourly":
        return f"Every hour at :{minute:02d}"
    if k == "interval":
        m = max(1, int(every_minutes))
        if m % 60 == 0 and m >= 60:
            h = m // 60
            return f"Every {h} hour{'s' if h > 1 else ''} at :{minute:02d}"
        return f"Every {m} minutes"
    if k == "weekdays":
        return f"Weekdays at {hour:02d}:{minute:02d} ({label})"
    if k == "weekly":
        picked = parse_days(days)
        if not picked:
            return f"Daily at {hour:02d}:{minute:02d} ({label})"
        names = ", ".join(WEEKDAY_NAMES[d] for d in picked)
        return f"{names} at {hour:02d}:{minute:02d} ({label})"
    return f"Daily at {hour:02d}:{minute:02d} ({label})"


def format_local(epoch: int | float | None, tz_name: str) -> str:
    """Render a stored UTC epoch in the schedule's own timezone, for the UI."""
    if not epoch:
        return "—"
    return datetime.fromtimestamp(float(epoch), resolve_tz(tz_name)).strftime("%a %d %b, %H:%M")