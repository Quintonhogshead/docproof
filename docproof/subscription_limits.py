"""Recognize Claude subscription exhaustion and its reported reset time."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LIMIT = re.compile(
    r"you(?:['’]ve| have) hit your (?:session|weekly|usage) limit"
    r"|(?:session|weekly|usage) limit (?:reached|exceeded)"
    r"|(?:extra usage|usage) (?:is )?(?:exhausted|limit reached)", re.I)
_RESET = re.compile(
    r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*\(([^)]+)\)", re.I)


class UsageLimitError(RuntimeError):
    """The shared subscription is exhausted; retry after reset, not new code."""


def is_usage_limited(text: str) -> bool:
    return bool(_LIMIT.search(text or ""))


def resume_after(text: str, *, now: float, retry_s: float = 300) -> float:
    """Use an explicit clock/timezone; otherwise recheck on a bounded cooldown.

    A recently passed reset can still be propagating. Do not turn that into
    a whole extra day's wait. Ambiguous/dated formats get a later probe rather
    than a guessed timezone or date.
    """
    match = _RESET.search(text or "")
    if match:
        hour, minute, meridiem, zone = match.groups()
        h, m = int(hour), int(minute or 0)
        if meridiem:
            if not 1 <= h <= 12:
                return now + retry_s
            h = h % 12 + (12 if meridiem.lower() == "pm" else 0)
        if h > 23 or m > 59:
            return now + retry_s
        try:
            tz = timezone.utc if zone.upper() in ("UTC", "GMT") else ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            return now + retry_s
        current = datetime.fromtimestamp(now, tz)
        reset = current.replace(hour=h, minute=m, second=0, microsecond=0)
        if reset < current:
            if (current - reset).total_seconds() <= 900:
                return now + retry_s
            reset += timedelta(days=1)
        return max(now + 1, reset.timestamp() + 30)  # allow reset propagation
    return now + retry_s
