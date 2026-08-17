"""The in-app clock's "run at these times of day" arithmetic, kept pure.

`schedule.py` hands a Mac's fixed times to launchd, which runs them even while
DocProof is closed. The always-on server has no launchd and never closes, so
its only clock is the timer inside the running app (`runner.consider`) — which
until now knew "every N minutes" and nothing about a time of day. This is the
other half: given a set of daily times and a time zone, decide whether one has
fallen since the watcher last looked, and say when the next one is.

It is pure and takes `now` as an argument, the same way `schedule.parse_times`
is: a test can ask it anything without waiting on a real clock, and the runner
stays the only place a wall clock is read.

The times themselves are parsed and validated by `schedule.parse_times`, so the
two clocks accept exactly the same "HH:MM,HH:MM" and reject the same nonsense.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .schedule import ScheduleError, parse_times


def zone(name: str) -> tzinfo:
    """The zone a set of times is read in, from an IANA name.

    A name rather than an offset because offsets move: "09:00 America/New_York"
    is nine in the morning in New York in July and in January both, and a fixed
    -05:00 is not. Empty is not this function's job — a blank zone means "the
    machine's own", which the caller reads straight off `now` and never asks
    here for."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ScheduleError(
            f"{name!r} is not a time zone DocProof knows. Use an IANA name "
            "like 'America/New_York' or 'Europe/London', or leave it blank for "
            "this machine's own time.") from e


def _local(now: datetime, tz_name: str) -> datetime:
    """`now` seen in the configured zone, or the machine's own when blank."""
    return now.astimezone(zone(tz_name)) if tz_name else now.astimezone()


def _at(day: datetime, h: int, m: int) -> datetime:
    """The moment `h:m` falls on `day`'s date, in `day`'s zone, as UTC."""
    return day.replace(hour=h, minute=m, second=0,
                       microsecond=0).astimezone(timezone.utc)


def due(times: list[str] | str, tz_name: str, *,
        floor: datetime, now: datetime) -> bool:
    """Has a scheduled time fallen in the window `(floor, now]`?

    `floor` is when the watcher last looked — or, before it ever has, when the
    clock armed — so a time is "due" only if its most recent occurrence is both
    in the past (at or before `now`) and newer than that floor. That is what
    makes enabling a schedule at 2pm wait for the next time rather than firing
    for this morning's, and what stops the minute-by-minute clock from running
    the same 09:00 twice."""
    local = _local(now, tz_name)
    for h, m in parse_times(times):
        occ = local.replace(hour=h, minute=m, second=0, microsecond=0)
        if occ > local:                    # today's slot is still ahead
            occ -= timedelta(days=1)        # so the last one was yesterday
        if _at(occ, h, m) > floor:
            return True
    return False


def next_run(times: list[str] | str, tz_name: str, *,
             now: datetime) -> datetime | None:
    """The next scheduled moment strictly after `now`, as aware UTC.

    For the panel to say "next look at 4:00pm" rather than leave a person
    guessing. None only when there are no times at all."""
    local = _local(now, tz_name)
    upcoming = []
    for h, m in parse_times(times):
        occ = local.replace(hour=h, minute=m, second=0, microsecond=0)
        if occ <= local:                   # today's slot has passed
            occ += timedelta(days=1)        # so the next one is tomorrow
        upcoming.append(_at(occ, h, m))
    return min(upcoming) if upcoming else None
