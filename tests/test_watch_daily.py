"""The in-app clock's time-of-day arithmetic, asked without a real clock.

`daily` takes `now` as an argument on purpose, so every one of these is a fact
about a fixed moment rather than a race against the wall.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.watch import daily
from app.watch.schedule import ScheduleError

UTC = timezone.utc


def at(y, mo, d, h, mi, tz="UTC"):
    """A wall-clock moment in `tz`, returned as aware UTC — the shape the clock
    works in."""
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)


# --- due ----------------------------------------------------------------------

def test_due_when_a_time_has_passed_since_the_last_look():
    now = at(2026, 8, 17, 9, 30)
    floor = at(2026, 8, 17, 8, 0)
    assert daily.due(["09:00"], "UTC", floor=floor, now=now) is True


def test_not_due_when_the_last_look_was_after_the_time():
    now = at(2026, 8, 17, 9, 30)
    floor = at(2026, 8, 17, 9, 15)         # already looked, past 09:00
    assert daily.due(["09:00"], "UTC", floor=floor, now=now) is False


def test_enabling_after_a_time_waits_for_its_next_turn():
    # Armed at 14:00 with a 09:00 schedule: this morning's slot is behind the
    # floor, so nothing is due until tomorrow.
    armed = at(2026, 8, 17, 14, 0)
    assert daily.due(["09:00"], "UTC", floor=armed, now=armed) is False


def test_a_later_time_the_same_day_is_its_own_trigger():
    now = at(2026, 8, 17, 17, 5)
    floor = at(2026, 8, 17, 9, 5)          # ran 09:00, not yet 17:00
    assert daily.due(["09:00", "17:00"], "UTC", floor=floor, now=now) is True


def test_times_are_read_in_the_named_zone():
    # 09:00 in New York on this date is 13:00 UTC (EDT, -4).
    ny_nine = at(2026, 8, 17, 9, 0, "America/New_York")
    assert ny_nine.hour == 13
    before = daily.due(["09:00"], "America/New_York",
                       floor=ny_nine - timedelta(hours=1),
                       now=ny_nine + timedelta(minutes=1))
    after = daily.due(["09:00"], "America/New_York",
                      floor=ny_nine + timedelta(minutes=1),
                      now=ny_nine + timedelta(minutes=5))
    assert before is True and after is False


def test_a_blank_zone_is_the_machine_and_does_not_raise():
    now = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    # Only that it answers without a zone name — the machine's own is whatever
    # the test host runs in, which is not something to assert a UTC hour about.
    assert daily.due(["09:00"], "", floor=now - timedelta(days=1),
                     now=now) in (True, False)


# --- next_run -----------------------------------------------------------------

def test_next_run_is_the_soonest_upcoming_time():
    now = at(2026, 8, 17, 10, 0)
    assert daily.next_run(["09:00", "17:00"], "UTC", now=now) == \
        at(2026, 8, 17, 17, 0)


def test_next_run_rolls_to_tomorrow_after_the_last_time():
    now = at(2026, 8, 17, 20, 0)
    assert daily.next_run(["09:00", "17:00"], "UTC", now=now) == \
        at(2026, 8, 18, 9, 0)


def test_next_run_with_a_blank_zone_is_within_the_day():
    now = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    nxt = daily.next_run(["09:00", "17:00"], "", now=now)
    assert nxt is not None and now < nxt <= now + timedelta(hours=24)


# --- refusals -----------------------------------------------------------------

def test_a_zone_that_is_not_a_zone_is_refused():
    with pytest.raises(ScheduleError):
        daily.zone("Mars/Olympus")


def test_a_time_that_is_not_a_time_is_refused():
    now = at(2026, 8, 17, 10, 0)
    with pytest.raises(ScheduleError):
        daily.due(["half past nine"], "UTC", floor=now, now=now)
