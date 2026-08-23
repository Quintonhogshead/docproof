"""Table-driven tests for galley.tools.checkable.

The classics live here: leap-year edges (2000 leap, 1900 not, 2024 leap),
pre-1900 weekdays, age_at boundaries (birthday on/before/after the event day),
ordinal teens-vs-ones, sum_check float tolerance, and the miles<->km helper.
"""

from __future__ import annotations

import pytest

from galley.tools.checkable import (
    TOOLS,
    age_at,
    miles_km_check,
    ordinal,
    sum_check,
    weekday_of,
    weekday_the_nth,
)


# --------------------------------------------------------------------------- #
# weekday_of
# --------------------------------------------------------------------------- #

WEEKDAY_CASES = [
    # (date, expected weekday)
    ("2024-02-29", "Thursday"),   # 2024 IS a leap year — Feb 29 exists
    ("2000-02-29", "Tuesday"),    # 2000 IS a leap year (÷400)
    ("2024-01-01", "Monday"),
    ("2023-12-31", "Sunday"),
    ("1899-12-31", "Sunday"),     # pre-1900
    ("1000-01-01", "Wednesday"),  # far pre-1900, proleptic Gregorian
    ("2026-08-23", "Sunday"),     # today, per the fixture date
]


@pytest.mark.parametrize("date_str, expected", WEEKDAY_CASES)
def test_weekday_of(date_str, expected):
    res = weekday_of(date_str)
    assert res.weekday == expected
    assert res.ok is None  # no claim supplied


def test_weekday_of_claim_match_is_case_insensitive():
    res = weekday_of("2024-02-29", claimed="thursDAY")
    assert res.ok is True


def test_weekday_of_claim_mismatch():
    res = weekday_of("2024-02-29", claimed="Friday")
    assert res.ok is False
    assert res.weekday == "Thursday"


def test_1900_is_not_a_leap_year():
    # Feb 29 1900 does not exist under Gregorian rules.
    with pytest.raises(ValueError):
        weekday_of("1900-02-29")


def test_weekday_of_rejects_garbage():
    with pytest.raises(ValueError):
        weekday_of("March 5, 2024")


# --------------------------------------------------------------------------- #
# age_at
# --------------------------------------------------------------------------- #

AGE_CASES = [
    # (birth, event, expected age)
    ("2000-03-10", "2024-03-09", 23),  # day before birthday
    ("2000-03-10", "2024-03-10", 24),  # on birthday
    ("2000-03-10", "2024-03-11", 24),  # day after birthday
    ("2000-01-01", "2000-01-01", 0),   # same day
    ("1990-12-31", "1991-01-01", 0),   # year rolled but birthday not reached
    ("2000-02-29", "2024-02-29", 24),  # leap birthday on a leap event year
    ("2000-02-29", "2023-02-28", 22),  # leap birthday, common year, day before Mar 1
    ("2000-02-29", "2023-03-01", 23),  # leap birthday counts on Mar 1 in common year
]


@pytest.mark.parametrize("birth, event, expected", AGE_CASES)
def test_age_at(birth, event, expected):
    assert age_at(birth, event).age == expected


def test_age_at_claim_check():
    assert age_at("1980-06-15", "2023-06-15", claimed=43).ok is True
    assert age_at("1980-06-15", "2023-06-14", claimed=43).ok is False


def test_age_at_event_before_birth_is_negative():
    res = age_at("2000-01-01", "1999-01-01")
    assert res.age < 0


# --------------------------------------------------------------------------- #
# sum_check
# --------------------------------------------------------------------------- #


def test_sum_check_exact_integers():
    res = sum_check([10, 20, 30], 60)
    assert res.ok is True
    assert res.actual == 60
    assert res.delta == 0


def test_sum_check_mismatch_reports_delta():
    res = sum_check([10, 20, 30], 61)
    assert res.ok is False
    assert res.delta == pytest.approx(-1.0)  # actual - stated


def test_sum_check_float_tolerance():
    # 0.1 + 0.2 == 0.30000000000000004 in binary float; must still match 0.3.
    res = sum_check([0.1, 0.2], 0.3)
    assert res.ok is True


def test_sum_check_beyond_tolerance():
    res = sum_check([0.1, 0.2], 0.3005, tolerance=1e-6)
    assert res.ok is False


def test_sum_check_empty():
    res = sum_check([], 0)
    assert res.ok is True
    assert res.actual == 0.0


# --------------------------------------------------------------------------- #
# miles_km_check
# --------------------------------------------------------------------------- #


def test_miles_km_exact():
    res = miles_km_check(1, 1.609344)
    assert res.ok is True
    assert res.expected_km == pytest.approx(1.609344)


def test_miles_km_rounded_author_figure_passes():
    # 100 mi = 160.9344 km; author wrote "160" — within 1%.
    res = miles_km_check(100, 160)
    assert res.ok is True


def test_miles_km_bad_conversion_fails():
    res = miles_km_check(100, 100)  # author used the mile figure as km
    assert res.ok is False


def test_miles_km_zero():
    res = miles_km_check(0, 0)
    assert res.ok is True


# --------------------------------------------------------------------------- #
# ordinal
# --------------------------------------------------------------------------- #

ORDINAL_CASES = [
    (1, "1st"),
    (2, "2nd"),
    (3, "3rd"),
    (4, "4th"),
    (11, "11th"),  # teens are always "th"
    (12, "12th"),
    (13, "13th"),
    (21, "21st"),
    (22, "22nd"),
    (23, "23rd"),
    (100, "100th"),
    (101, "101st"),
    (111, "111th"),
    (112, "112th"),
    (113, "113th"),
    (0, "0th"),
]


@pytest.mark.parametrize("n, expected", ORDINAL_CASES)
def test_ordinal(n, expected):
    assert ordinal(n) == expected


def test_ordinal_rejects_bool():
    with pytest.raises(ValueError):
        ordinal(True)


# --------------------------------------------------------------------------- #
# weekday_the_nth
# --------------------------------------------------------------------------- #


def test_weekday_the_nth_all_match():
    # 2024-03-05 is a Tuesday.
    res = weekday_the_nth("2024-03-05", "Tuesday", 5)
    assert res.ok is True
    assert res.day_ordinal == "5th"
    assert res.weekday == "Tuesday"


def test_weekday_the_nth_wrong_weekday():
    res = weekday_the_nth("2024-03-05", "Wednesday", 5)
    assert res.ok is False


def test_weekday_the_nth_wrong_day():
    res = weekday_the_nth("2024-03-05", "Tuesday", 6)
    assert res.ok is False


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


def test_registry_exposes_all_tools():
    expected = {
        "weekday_of",
        "age_at",
        "sum_check",
        "miles_km_check",
        "ordinal",
        "weekday_the_nth",
    }
    assert set(TOOLS) == expected
    for name, fn in TOOLS.items():
        assert callable(fn)


def test_registry_callables_are_the_module_functions():
    assert TOOLS["weekday_of"] is weekday_of
    assert TOOLS["age_at"] is age_at
    assert TOOLS["sum_check"] is sum_check
