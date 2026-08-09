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
import html as htmllib
import json
import logging
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from . import drive
from .drive import DriveError

log = logging.getLogger("docproof.app.watch.notify")

SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

DRIVE_FILE = "https://drive.google.com/file/d/{}/view"
DRIVE_FOLDER = "https://drive.google.com/drive/folders/{}"


def _raw(to: str, subject: str, body: str, *, html: str | None = None,
         attachment: tuple[str, bytes] | None = None) -> str:
    """One message, base64url-encoded the way Gmail's API wants it.

    Plain text always; an HTML alternative when there is one, so an inbox shows
    the table and `grep` still finds the mirror; and one attachment — the raw
    prep.json — when the caller has it, for a literal complete record."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    if attachment is not None:
        name, data = attachment
        msg.add_attachment(data, maintype="application", subtype="json",
                           filename=name)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def send(token: str, to: str, subject: str, body: str, *,
         html: str | None = None, attachment: tuple[str, bytes] | None = None,
         opener=drive._open_url) -> None:
    """Send one email as the signed-in Google account.

    `token` is a Google access token carrying the gmail.send scope — the same
    token the Drive calls use, once the sign-in has been re-consented."""
    payload = json.dumps(
        {"raw": _raw(to, subject, body, html=html, attachment=attachment)}
    ).encode()
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


# --- the completion log -------------------------------------------------------
#
# A finished book's whole record in one email: what it cost, what model and
# effort produced it, how long it took, and — the reason this is worth sending
# on every success and not just on failure — where it was routed, so a book put
# into the wrong author's subfolder is seen the same morning rather than found
# weeks later.


def _int(value) -> str:
    """A count for a person to read, or an em dash when there is none."""
    return f"{value:,}" if isinstance(value, int) else "—"


def _cost(job) -> str:
    """The figure, labelled so it is never mistaken for a bill.

    It is priced from the run's real token counts but at the catalog's list
    rates, so it is an estimate — an accurate shape, not the invoice."""
    if job.cost is None:
        return "unavailable (this model is not in the price catalog)"
    return f"${job.cost:,.2f} — estimated at list rates, not a billed figure"


def _elapsed(job) -> str:
    """How long the run took, from the job's own timestamps. There is no stored
    duration, so it is the gap between created and last updated."""
    try:
        secs = int((datetime.fromisoformat(job.updated_at)
                    - datetime.fromisoformat(job.created_at)).total_seconds())
    except (ValueError, TypeError):
        return "—"
    if secs < 0:
        return "—"
    hours, rem = divmod(secs, 3600)
    mins, s = divmod(rem, 60)
    if hours:
        return f"{hours}h {mins}m {s}s"
    if mins:
        return f"{mins}m {s}s"
    return f"{s}s"


def _load_prep(job) -> dict:
    """The run's prep.json, the richest record there is, or an empty mapping
    when it cannot be read — a completion mail is worth sending without it."""
    try:
        raw = json.loads((Path(job.results_dir or "") / "prep.json")
                         .read_text("utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _groups(ws, job, file, rec, uploaded: list[str],
            dest_folder_id: str, prep: dict) -> list[tuple[str, list]]:
    """The log as titled groups of `(label, text, link-or-None)` rows, so the
    plain-text and HTML renderers draw the same thing two ways."""
    author = (rec.subfolder_name
              or " ".join(p for p in (rec.author_first, rec.author_last) if p)
              or "—")
    outputs: list = [(name, name, DRIVE_FILE.format(oid))
                     for name, oid in rec.uploaded.items()] or [("—", "—", None)]
    counts = prep.get("counts") if isinstance(prep.get("counts"), dict) else {}
    usage = prep.get("usage") if isinstance(prep.get("usage"), dict) else {}
    sheet = prep.get("style_sheet") if isinstance(
        prep.get("style_sheet"), dict) else {}
    prompt = prep.get("tagging_prompt") if isinstance(
        prep.get("tagging_prompt"), dict) else {}

    groups: list[tuple[str, list]] = [
        ("Routing", [
            ("Author", author, None),
            ("Subfolder", rec.subfolder_name or "(flat watched folder)",
             DRIVE_FOLDER.format(dest_folder_id) if dest_folder_id else None),
            ("Source", file.name, DRIVE_FILE.format(file.id)),
            ("HubSpot record",
             f"{ws.hubspot_object} / {rec.hubspot_id}" if rec.hubspot_id
             else "—", None),
        ]),
        ("Outputs", outputs),
        ("Model run", [
            ("Model", job.model, None),
            ("Effort", job.effort, None),
            ("Mode", job.mode, None),
            ("API calls", _int(job.api_calls), None),
            ("Windows", f"{job.done}/{job.total}", None),
        ]),
        ("Cost & tokens", [
            ("Estimated cost", _cost(job), None),
            ("Input tokens", _int(job.input_tokens), None),
            ("Output tokens", _int(job.output_tokens), None),
            ("Cache read", _int(job.cache_read_tokens), None),
            ("Cache write", _int(job.cache_write_tokens), None),
        ]),
        ("Manuscript", [
            ("Words", _int(job.words), None),
            ("Paragraphs styled", _int(job.tagged), None),
            ("Flags for a human", _int(job.flags), None),
            ("Author's words verified intact",
             "yes" if job.verified else "no", None),
        ]),
        ("Timing", [
            ("Started", job.created_at or "—", None),
            ("Finished", job.updated_at or "—", None),
            ("Elapsed", _elapsed(job), None),
        ]),
    ]

    detail: list = []
    if sheet:
        detail.append(("Style sheet",
                       f"{sheet.get('name', '?')} v{sheet.get('version', '?')}",
                       None))
    if prompt:
        detail.append(("Tagging prompt",
                       f"{prompt.get('name', '?')} v{prompt.get('version', '?')}",
                       None))
    for key in ("paragraphs", "blank_lines", "scene_breaks_inserted",
                "scene_breaks_from_author", "blank_lines_removed",
                "paragraphs_trimmed", "italic_paragraphs", "link_paragraphs",
                "unanswered", "untouched_outside_body"):
        if key in counts:
            detail.append((key.replace("_", " "), _int(counts[key]), None))
    if usage:
        detail.append(("Raw usage", json.dumps(usage, sort_keys=True), None))
    if detail:
        groups.append(("Detail", detail))
    return groups


def _text(subject: str, groups: list[tuple[str, list]]) -> str:
    lines = [subject, ""]
    for title, rows in groups:
        lines.append(f"{title}:")
        for label, value, link in rows:
            tail = f"  <{link}>" if link else ""
            lines.append(f"  {label}: {value}{tail}")
        lines.append("")
    lines.append("Cost is an estimate priced at list rates from the run's real "
                 "token counts — not a billed figure.")
    return "\n".join(lines)


def _html(subject: str, groups: list[tuple[str, list]]) -> str:
    def cell(value: str, link: str | None) -> str:
        safe = htmllib.escape(value)
        return (f'<a href="{htmllib.escape(link, quote=True)}">{safe}</a>'
                if link else safe)

    parts = [f"<h2>{htmllib.escape(subject)}</h2>"]
    for title, rows in groups:
        parts.append(f"<h3>{htmllib.escape(title)}</h3>")
        parts.append('<table cellpadding="4" cellspacing="0" '
                     'style="border-collapse:collapse">')
        for label, value, link in rows:
            parts.append(
                f'<tr><td style="color:#666;vertical-align:top">'
                f'{htmllib.escape(label)}</td>'
                f'<td>{cell(value, link)}</td></tr>')
        parts.append("</table>")
    parts.append('<p style="color:#666;font-size:12px">Cost is an estimate '
                 'priced at list rates from the run&rsquo;s real token counts '
                 '&mdash; not a billed figure.</p>')
    return "<body>" + "".join(parts) + "</body>"


def completion(ws, job, file, rec, uploaded: list[str],
               dest_folder_id: str) -> tuple[str, str, str]:
    """The subject, plain-text body and HTML body for one finished book."""
    prep = _load_prep(job)
    author = rec.subfolder_name or file.name
    subject = f"DocProof finished {author} — {file.name}"
    groups = _groups(ws, job, file, rec, uploaded, dest_folder_id, prep)
    return subject, _text(subject, groups), _html(subject, groups)


def maybe_complete(token: str, ws, job, file, rec, uploaded: list[str],
                   dest_folder_id: str, *, opener=drive._open_url) -> None:
    """Email the whole log for a book that just finished, if asked to.

    Gated by the caller on `notify_on_complete` and `notify_email`. Best-effort
    like `maybe_notify`: a send that fails is logged with its fix, never raised,
    so a mail server never undoes a finished book. The raw prep.json rides along
    as an attachment when it is on disk."""
    prep_path = Path(job.results_dir or "") / "prep.json"
    attachment = None
    try:
        attachment = ("prep.json", prep_path.read_bytes())
    except OSError:
        pass
    subject, body, html = completion(ws, job, file, rec, uploaded,
                                     dest_folder_id)
    try:
        send(token, ws.notify_email, subject, body, html=html,
             attachment=attachment, opener=opener)
        log.info("Emailed %s the completion log for %s.", ws.notify_email,
                 file.name)
    except DriveError as e:
        log.warning("Could not email %s the completion log for %s (%s). If "
                    "Gmail refused the scope, run `docproof-watch auth` again.",
                    ws.notify_email, file.name, e)


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


__all__ = ["SEND_URL", "send", "summary", "maybe_notify", "completion",
           "maybe_complete"]
