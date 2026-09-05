"""Deterministic arithmetic and calendar checks for proofreading claims.

Each tool returns ``ok`` plus the computed value. Functions are pure and
deterministic. Dates use Python's proleptic Gregorian calendar (including years
before 1582); Julian-calendar manuscripts may therefore differ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

# ISO weekday index (Monday=0 .. Sunday=6) -> name.
_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# One statute mile is exactly 1.609344 kilometres.
_KM_PER_MILE = 1.609344


def _parse_iso(date_str: str) -> date:
    """Parse a strict ISO ``YYYY-MM-DD`` string into a ``date``.

    Raises ``ValueError`` (with a model-facing message) on anything else so the
    caller learns the input was malformed rather than getting a wrong answer.
    """
    if not isinstance(date_str, str):
        raise ValueError(f"expected an ISO date string, got {type(date_str).__name__}")
    s = date_str.strip()
    if not re.fullmatch(r"-?\d{1,}-\d{2}-\d{2}", s):
        raise ValueError(
            f"expected an ISO date 'YYYY-MM-DD', got {date_str!r}"
        )
    try:
        return date.fromisoformat(s)
    except ValueError as exc:  # e.g. 2023-02-30
        raise ValueError(f"not a real calendar date: {date_str!r} ({exc})") from exc




@dataclass(frozen=True)
class WeekdayResult:
    """Result of :func:`weekday_of`.

    ``weekday`` is the true day name for the date. ``ok`` is ``True`` when the
    caller supplied a ``claimed`` day and it matches (case-insensitively); it is
    ``None`` when no claim was given (you only asked what day it was).
    """

    date: str
    weekday: str
    claimed: str | None = None
    ok: bool | None = None


def weekday_of(date_str: str, claimed: str | None = None) -> WeekdayResult:
    """Return the day of the week for an ISO date, and check a claimed day.

    Use this whenever the text ties a date to a weekday ("on Tuesday, March 5,
    2024" or "the 14th, a Friday"). Pass the date as ``YYYY-MM-DD``. If the
    manuscript names the day, pass it as ``claimed`` (e.g. ``"Friday"``) and read
    ``ok`` to learn whether the text is right.

    Pre-1900 and far-future dates are handled correctly under the proleptic
    Gregorian calendar (see the module docstring).
    """
    d = _parse_iso(date_str)
    name = _WEEKDAY_NAMES[d.weekday()]
    ok: bool | None = None
    if claimed is not None:
        ok = claimed.strip().lower() == name.lower()
    return WeekdayResult(date=d.isoformat(), weekday=name, claimed=claimed, ok=ok)




@dataclass(frozen=True)
class AgeResult:
    """Result of :func:`age_at`.

    ``age`` is the completed age in whole years at the event date. ``ok`` checks
    it against a ``claimed`` age when one is supplied.
    """

    birth: str
    event: str
    age: int
    claimed: int | None = None
    ok: bool | None = None


def age_at(birth: str, event: str, claimed: int | None = None) -> AgeResult:
    """Compute completed years between ISO dates and optionally check claimed.

    February 29 birthdays advance on March 1 in non-leap years. Return
    negative ages for events before birth."""
    b = _parse_iso(birth)
    e = _parse_iso(event)
    age = e.year - b.year
    # Subtract one if the birthday has not yet occurred in the event year.
    if (e.month, e.day) < (b.month, b.day):
        age -= 1
    ok: bool | None = None
    if claimed is not None:
        ok = age == claimed
    return AgeResult(
        birth=b.isoformat(), event=e.isoformat(), age=age, claimed=claimed, ok=ok
    )




@dataclass(frozen=True)
class SumResult:
    """Result of :func:`sum_check`.

    ``actual`` is the true sum of the amounts. ``delta`` is ``actual`` minus the
    stated total (positive means the stated total is too low). ``ok`` is ``True``
    when the two agree within tolerance.
    """

    actual: float
    stated_total: float
    delta: float
    ok: bool
    tolerance: float


def sum_check(
    amounts: "list[float] | tuple[float, ...]",
    stated_total: float,
    tolerance: float = 1e-9,
) -> SumResult:
    """Compare the sum of amounts with stated_total using absolute tolerance.

    Return actual, delta (actual - stated_total), and ok. The default
    tolerance absorbs floating-point rounding; widen it for coarser units."""
    actual = 0.0
    for a in amounts:
        actual += float(a)
    delta = actual - float(stated_total)
    ok = abs(delta) <= tolerance
    return SumResult(
        actual=actual,
        stated_total=float(stated_total),
        delta=delta,
        ok=ok,
        tolerance=tolerance,
    )




@dataclass(frozen=True)
class ConversionResult:
    """Result of :func:`miles_km_check`.

    ``expected_km`` is what the miles figure converts to. ``delta_km`` is the
    stated kilometres minus that expected value. ``ok`` is ``True`` within the
    relative tolerance.
    """

    miles: float
    stated_km: float
    expected_km: float
    delta_km: float
    ok: bool
    rel_tolerance: float


def miles_km_check(
    miles: float, stated_km: float, rel_tolerance: float = 0.01
) -> ConversionResult:
    """Check a miles-to-kilometres conversion for consistency.

    The manuscript gives a distance both ways ("a hundred miles — about 160
    kilometres"). Pass the miles figure and the stated kilometres. The tool
    converts miles at the exact factor 1 mi = 1.609344 km and reports whether the
    stated kilometres are within ``rel_tolerance`` (a *relative* tolerance,
    default 1%, so an author's rounded "160" for 160.9344 still passes). Read
    ``expected_km`` for the precise conversion when you need to suggest a fix.

    This checks only the miles<->km pairing; it does not validate the distance
    itself.
    """
    expected_km = float(miles) * _KM_PER_MILE
    delta_km = float(stated_km) - expected_km
    # Relative to the expected magnitude; guard the zero case.
    scale = abs(expected_km) if expected_km != 0 else 1.0
    ok = abs(delta_km) <= rel_tolerance * scale
    return ConversionResult(
        miles=float(miles),
        stated_km=float(stated_km),
        expected_km=expected_km,
        delta_km=delta_km,
        ok=ok,
        rel_tolerance=rel_tolerance,
    )




def ordinal(n: int) -> str:
    """Return the English ordinal string for an integer: 1 -> "1st".

    Handles the teens correctly (11th, 12th, 13th all take "th", unlike 1st, 2nd,
    3rd) and the compound tens (21st, 22nd, 23rd, 101st, 113th). Negative numbers
    keep their sign ("-1st"). Use this to verify or generate ordinals in prose
    ("the 22nd of May", "her 43rd birthday").
    """
    if not isinstance(n, int) or isinstance(n, bool):
        raise ValueError(f"ordinal expects an int, got {type(n).__name__}")
    last_two = abs(n) % 100
    if 11 <= last_two <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(abs(n) % 10, "th")
    return f"{n}{suffix}"




@dataclass(frozen=True)
class WeekdayNthResult:
    """Result of :func:`weekday_the_nth` — a 'Tuesday the 5th' style claim.

    ``weekday`` and ``day_ordinal`` are the true values for the date. ``ok`` is
    ``True`` only when both the claimed weekday and the claimed day-of-month
    match the actual date.
    """

    date: str
    weekday: str
    day_ordinal: str
    claimed_weekday: str
    claimed_day: int
    ok: bool


def weekday_the_nth(date_str: str, claimed_weekday: str, claimed_day: int) -> WeekdayNthResult:
    """Verify a 'Weekday the Nth' claim against a full date.

    For phrases like "Tuesday the 5th" that name both a weekday and a day of the
    month, pass the anchoring full ISO date plus the claimed weekday and the
    claimed day number. ``ok`` is ``True`` only if both agree with the real date.
    ``day_ordinal`` gives the correct ordinal so you can propose a fix.
    """
    d = _parse_iso(date_str)
    true_weekday = _WEEKDAY_NAMES[d.weekday()]
    ok = (
        claimed_weekday.strip().lower() == true_weekday.lower()
        and claimed_day == d.day
    )
    return WeekdayNthResult(
        date=d.isoformat(),
        weekday=true_weekday,
        day_ordinal=ordinal(d.day),
        claimed_weekday=claimed_weekday,
        claimed_day=claimed_day,
        ok=ok,
    )



TOOLS: "dict[str, Callable]" = {
    "weekday_of": weekday_of,
    "age_at": age_at,
    "sum_check": sum_check,
    "miles_km_check": miles_km_check,
    "ordinal": ordinal,
    "weekday_the_nth": weekday_the_nth,
}

__all__ = [
    "WeekdayResult",
    "AgeResult",
    "SumResult",
    "ConversionResult",
    "WeekdayNthResult",
    "weekday_of",
    "age_at",
    "sum_check",
    "miles_km_check",
    "ordinal",
    "weekday_the_nth",
    "TOOLS",
]
