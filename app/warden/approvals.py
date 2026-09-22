"""The approvals vocabulary on top of `journal.requests`.

Tier-1 fixes and code requests both wait on the same thing: a text back that
says "yes 14" or "no 14" from Quinton, inside a window, naming one specific
request. This module is the whole grammar of that exchange — rendering a
request as the text that goes out, parsing whatever comes back, applying it
if it is still live, and sweeping up anything nobody answered in time — kept
apart from `journal.py` because the journal only needs to know how to store
and expire a request; it has no opinion on what "yes 14" looks like typed
into an iPhone.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from app.warden.journal import Journal, Request

log = logging.getLogger("docproof.app.warden.approvals")

# "yes 14", "y14", "no 3", "YES #14" - loose on whitespace and an optional
# '#', strict on everything else: this is a command channel, not free text,
# and a text that merely mentions a number in passing ("book 14 chapters")
# must not parse as an answer.
_ANSWER_RE = re.compile(r"^\s*(yes|no|y|n)\s*#?\s*(\d+)\s*$", re.IGNORECASE)

_YES = {"yes", "y"}
_NO = {"no", "n"}


def request_text(req: Request) -> str:
    """The outbound text for a newly-opened request."""
    return f"#{req.number} {req.text}. Reply 'yes {req.number}' or 'no {req.number}'."


def parse_answer(text: str) -> tuple[str, int] | None:
    """`("yes"|"no", number)`, or `None` if `text` is not an answer at all."""
    m = _ANSWER_RE.match(text or "")
    if not m:
        return None
    word, number = m.group(1).lower(), int(m.group(2))
    answer = "yes" if word in _YES else "no"
    return answer, number


def apply_answer(journal: Journal, text: str, who: str,
                  now: datetime) -> Request | None:
    """Parse `text` as an answer and, if it names a request that is still
    open and not past its own deadline, record it and return the updated
    `Request`. Returns `None` for anything that is not a live answer —
    unparseable text, an unknown number, or a request already answered,
    already expired, or past `expires_at` (which this also retires, so a
    late "yes" does not linger as a no-op open request forever).

    `who` is trusted as given: only the caller — `commands.handle`, fed only
    from a verified sender — decides whose name goes on the approval."""
    parsed = parse_answer(text)
    if parsed is None:
        return None
    answer, number = parsed
    req = journal.get_request(number)
    if req is None:
        return None
    if req.status == "open" and req.expires_at:
        journal.expire_requests(now)
        req = journal.get_request(number)
    if req is None or req.status != "open":
        return None
    try:
        return journal.answer_request(number, answer, who)
    except (KeyError, ValueError) as e:
        log.info("Could not apply answer to #%s: %s", number, e)
        return None


def expire(journal: Journal, now: datetime, hours: int) -> list[Request]:
    """Expire every open request older than `hours` that has no explicit
    `expires_at` of its own, plus any explicit-deadline request `now` has
    already passed. One call the tick makes once per pass; see
    `Journal.expire_requests` for the two-deadline logic."""
    return journal.expire_requests(now, default_hours=hours)


__all__ = ["request_text", "parse_answer", "apply_answer", "expire"]
