"""Telling a person when a pass needs one.

Some outcomes are nobody's to guess — a surname matching two Projects both
flagged ready, a manuscript that failed prep three runs running. Those land in
`TickReport.needs_human` and `failed`, and this turns them into one email, sent
as the same Google account that reads the Drive, through Gmail's send API. One
request, on the injected opener the rest of the watcher already uses, so no test
ever sends mail.

The sign-in has to carry the `gmail.send` scope for this to work — see
`auth.SCOPE`. A Drive-only token predates it, so the first send after upgrading
answers 403 until `docproof-watch auth` is run again; that is logged, never
raised, because a pass that did its work should not fail over an email.
"""
from __future__ import annotations

import base64
import json
import logging
from email.message import EmailMessage

from . import drive
from .drive import DriveError

log = logging.getLogger("docproof.app.watch.notify")

SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def _raw(to: str, subject: str, body: str) -> str:
    """One plain-text message, base64url-encoded the way Gmail's API wants it."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def send(token: str, to: str, subject: str, body: str, *,
         opener=drive._open_url) -> None:
    """Send one plain-text email as the signed-in Google account.

    `token` is a Google access token carrying the gmail.send scope — the same
    token the Drive calls use, once the sign-in has been re-consented."""
    payload = json.dumps({"raw": _raw(to, subject, body)}).encode()
    request = drive._request(SEND_URL, token, data=payload, method="POST",
                             content_type="application/json")
    drive._json_call(request, opener=opener, what="send the alert email")


def summary(report) -> tuple[str, str] | None:
    """The subject and body for a pass that needs a person, or `None` if it does
    not. `needs_human` is why this exists; a hard `failed` earns the same email,
    so a quiet morning is never a silent one."""
    if not report.needs_human and not report.failed:
        return None
    lines: list[str] = []
    if report.needs_human:
        lines.append("Manuscripts DocProof could not place:")
        lines += [f"  - {name}: {reason}"
                  for name, reason in report.needs_human]
    if report.failed:
        if lines:
            lines.append("")
        lines.append("Manuscripts that failed to prepare:")
        lines += [f"  - {name}: {reason}" for name, reason in report.failed]
    count = len(report.needs_human) + len(report.failed)
    subject = f"DocProof needs a look - {count} manuscript(s)"
    body = ("DocProof finished a pass over the Drive folder and left the "
            "following for a person:\n\n" + "\n".join(lines) +
            "\n\nThe rest of the pass went on as usual.")
    return subject, body


def maybe_notify(token: str, ws, report, *, opener=drive._open_url) -> None:
    """Email the watcher's owner when a pass needs a person and an address is set.

    A mail that will not send never breaks the pass: it is logged — with the fix
    when the fix is "re-consent the sign-in" — and the tick returns as it would
    have. No address set is the quiet default, so an install that never asked for
    email behaves exactly as before."""
    if not ws.notify_email:
        return
    made = summary(report)
    if not made:
        return
    subject, body = made
    try:
        send(token, ws.notify_email, subject, body, opener=opener)
        log.info("Emailed %s about %d manuscript(s) needing a person.",
                 ws.notify_email,
                 len(report.needs_human) + len(report.failed))
    except DriveError as e:
        log.warning("Could not email %s about a pass that needs a person (%s). "
                    "If Gmail refused the scope, run `docproof-watch auth` again "
                    "to add send permission.", ws.notify_email, e)


__all__ = ["SEND_URL", "send", "summary", "maybe_notify"]
