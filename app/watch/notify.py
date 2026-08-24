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

# Subject tags, so an inbox can sort on them. Every email carries [DocProof]
# and one fixed urgency: a pass that needs a person is [High][Action], a
# finished book is [Low][Done]. The tags lead the subject, ahead of the prose,
# so a Gmail "subject contains" filter has a stable string to match.
ALERT_TAGS = "[DocProof][High][Action]"
DONE_TAGS = "[DocProof][Low][Done]"

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
    so a quiet morning is never a silent one; a `missing_source` — an author
    flagged ready with no Book Original in the folder — rides along too, so a
    file that needs uploading or renaming is seen the same morning; and a
    `stuck_ready` — a book already formatted whose HubSpot status never moved —
    so a stalled record is caught rather than sitting ready forever.

    The body closes with the pass's success tally — how many jobs finished
    cleanly (formatting, promo and plan together) — so an alert about a handful
    of stragglers is read against the scale of the pass rather than in a vacuum:
    "two need a look" lands differently beside twelve that went through than
    beside none."""
    if not (report.needs_human or report.failed or report.missing_source
            or report.stuck_ready):
        return None
    lines: list[str] = []
    if report.needs_human:
        lines.append("Manuscripts DocProof could not place:")
        lines += [f"  - {name}: {reason}"
                  for name, reason in report.needs_human]
    if report.missing_source:
        if lines:
            lines.append("")
        lines.append("Authors flagged ready with no Book Original in the folder:")
        lines += [f"  - {name}: {reason}"
                  for name, reason in report.missing_source]
    if report.stuck_ready:
        if lines:
            lines.append("")
        lines.append("Authors flagged ready whose book is already formatted "
                     "(move the status on):")
        lines += [f"  - {name}: {reason}"
                  for name, reason in report.stuck_ready]
    if report.failed:
        if lines:
            lines.append("")
        lines.append("Manuscripts that failed to prepare:")
        lines += [f"  - {name}: {reason}" for name, reason in report.failed]
    count = (len(report.needs_human) + len(report.missing_source)
             + len(report.stuck_ready) + len(report.failed))
    # Successful jobs across all three stages. Each list is appended to only once
    # the job reached "done" (see tick.run_prep / run_promo / run_plans), and a
    # book is never in two stages in one pass, so the sum is a true job count with
    # no double-counting. `uploaded` is deliberately left out: it is one entry per
    # delivered output, not per job.
    succeeded = len(report.prepped) + len(report.promoted) + len(report.planned)
    subject = f"{ALERT_TAGS} {count} item(s) need a look"
    body = ("DocProof finished a pass over the Drive folder and left the "
            "following for a person:\n\n" + "\n".join(lines) +
            f"\n\nThe rest of the pass went on as usual — {succeeded} "
            f"job(s) completed successfully.")
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
    line = f"${job.cost:,.2f} — estimated at list rates, not a billed figure"
    # When the Sapling pass ran, name its share: the total is model + Sapling,
    # and a per-character charge on a third-party service is exactly the kind of
    # line a person wants to see broken out rather than buried in the total.
    sapling = getattr(job, "sapling_cost", 0.0) or 0.0
    if sapling:
        line += f" (includes ${sapling:,.2f} Sapling grammar check)"
    return line


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
    # The tagged subject is for the inbox; the body keeps the plain heading, so
    # the tags sort the mail without cluttering the log a person reads.
    title = f"DocProof finished {author} — {file.name}"
    subject = f"{DONE_TAGS} {author} — {file.name}"
    groups = _groups(ws, job, file, rec, uploaded, dest_folder_id, prep)
    return subject, _text(title, groups), _html(title, groups)


def maybe_complete(token: str, ws, job, file, rec, uploaded: list[str],
                   dest_folder_id: str, *, opener=drive._open_url) -> bool:
    """Email the whole log for a book that just finished, if asked to.

    Gated by the caller on `notify_on_complete` and `notify_email`. Returns
    whether the mail went out, so the caller marks the book emailed only on a
    real send and a failure is retried. Best-effort like `maybe_notify`: a send
    that fails is logged with its fix, never raised, so a mail server never
    undoes a finished book. The raw prep.json rides along as an attachment when
    it is on disk."""
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
        return True
    except DriveError as e:
        log.warning("Could not email %s the completion log for %s (%s). If "
                    "Gmail refused the scope, run `docproof-watch auth` again.",
                    ws.notify_email, file.name, e)
        return False


# --- the completion log for any job, watched or not ---------------------------
#
# The watched-book log above is the richest form, because a watched book has
# routing and Drive links to show. A job dropped in the app has none of that —
# no author folder, no HubSpot record, no Drive file — so this builds the same
# grouped schema from the job record alone, and adapts the middle group to what
# the pipeline actually did: a proofread reports changes, a format reports styled
# paragraphs, promo reports the copy and its grounding flags.

PIPELINE_LABEL = {"review": "Proofread", "prep": "Format", "promo": "Promo copy",
                  "corrections": "Corrections"}
SOURCE_LABEL = {"app": "Dropped in the app", "watch": "Watched folder"}


def _result_group(job) -> tuple[str, list]:
    """The one group that differs by pipeline: what this job produced."""
    if job.kind == "corrections":
        # Count in one unit so the rows sum to the whole, matching the report. From
        # a proof that unit is the comments — reviewer comments = applied + no change
        # needed + need a human — with the edit tally shown beneath as the mechanism.
        # `applied`/`flags` are edit counts and cannot sum with a comment total, so a
        # typed list (no comments) is the only place they lead. See the report for
        # why the two units differ.
        total = job.total_comments or 0
        if total:
            human = job.unresolved or 0
            rows = [("Reviewer comments", _int(total), None)]
            if isinstance(job.applied_comments, int):
                no_change = max(0, total - job.applied_comments - human)
                rows.append(("Applied", _int(job.applied_comments), None))
                if no_change:
                    rows.append(("No change needed", _int(no_change), None))
            rows.append(("Need a human", _int(human), None))
            rows.append(("Edits applied", _int(job.applied), None))
        else:
            rows = [
                ("Applied", _int(job.applied), None),
                ("Refused for a human", _int(job.flags), None),
            ]
        rows.append(("Unaccounted changes", _int(job.discrepancies), None))
        rows.append(("Clean", "yes" if job.verified else "no", None))
        return ("Corrections", rows)
    if job.kind == "promo":
        return ("Promo copy", [
            ("Words read", _int(job.words), None),
            ("Grounding flags to check", _int(job.unverified), None),
        ])
    if job.kind == "prep":
        return ("Manuscript", [
            ("Words", _int(job.words), None),
            ("Paragraphs styled", _int(job.tagged), None),
            ("Flags for a human", _int(job.flags), None),
            ("Author's words verified intact",
             "yes" if job.verified else "no", None),
        ])
    # A proofread hands back two things, and for a long time this said only one
    # of them. The queries are the questions in the margins — nothing was
    # changed for them, and someone has to answer them — so a log that reports
    # only the corrections understates the work and hides what is waiting.
    # Zero of either is left off rather than printed: a run with no questions
    # should read like a run with no questions, not like a row of noughts.
    rows = [("Changes applied", _int(job.applied), None)]
    queried = getattr(job, "queried", 0) or 0
    if queried:
        rows.append(("Queries for the author", _int(queried), None))
    # Held-back changes are queries too — a judge withdraws a correction by
    # turning it into a margin question — so this is a share of the row above,
    # not a fourth number to add to it. Said that way, or the email reads as
    # more work than there is.
    held = getattr(job, "judge_held", 0) or 0
    if held:
        rows.append(("Held back by judge", f"{_int(held)} of those", None))
    # A review that still finished but had a pass fail or skip is degraded, and
    # the log is the one place a scheduled run is ever seen — so it has to say
    # so here, not only in a summary.md nobody opens on a green run.
    warnings = getattr(job, "warnings", None) or []
    if warnings:
        rows.append(("Run health",
                     f"DEGRADED — {len(warnings)} pass(es) fell short: "
                     + "; ".join(warnings), None))
    rows.append(("Reject-all check",
                 "failed — see the results card" if job.audit_failed
                 else "passed", None))
    return ("Review", rows)


def _settings_rows(job) -> list:
    """What the run was set to do, from the job record — so the email says not
    just what it cost but what it actually ran. Reflects the choices a person
    made on the panel; the attached summary carries the fully-resolved list."""
    # Corrections runs no model, so none of the review dials below apply — say
    # what it is instead of a page of defaults it never read.
    if getattr(job, "kind", "") == "corrections":
        return [("Engine", "deterministic — no model, no cost", None)]
    feats = getattr(job, "features", None) or {}
    rows = [("Confidence gate",
             getattr(job, "min_confidence", None) or "default", None)]
    rounds = getattr(job, "rounds", 1) or 1
    if rounds > 1:
        rows.append(("Review rounds", str(rounds), None))
    gloss = getattr(job, "glossary_model", "") or ""
    rows.append(("Glossary read",
                 "off" if gloss == "off" else (gloss or "default"), None))
    # Continuity-only strips the run to the contradiction read — the case that
    # most needs saying plainly, since almost nothing else runs.
    if getattr(job, "continuity_only", False):
        rows.append(("Continuity read only",
                     "yes — the model review, sweeps and Sapling were skipped",
                     None))
        return rows
    rows.append(("Model error-type passes", "full house set", None))
    # Hand-maintained, and silently partial: a pass that exists in
    # app/features.py but is missing a line here never appears in the email,
    # however loudly it was switched on. Add the pass here in the same change
    # that adds the toggle.
    labels = [("storysheet", "story sheet"),
              ("adjudicate", "real-word typos"),
              ("rewrite", "rewrite & compare"),
              ("languagetool", "LanguageTool"),
              ("sapling", "Sapling grammar check"),
              ("consistency", "consistency"),
              ("spellcheck", "spell scan"),
              ("continuity", "continuity read"),
              ("chapter_continuity", "chapter continuity"),
              ("meaning_check", "meaning check"),
              ("fix_check", "fix check")]
    on = [name for key, name in labels if feats.get(key)]
    rows.append(("Extra passes on", ", ".join(on) if on else "none", None))
    return rows


def completion_for_job(job) -> tuple[str, str, str]:
    """Subject, plain-text body and HTML body for one finished job, from the job
    record alone. The same schema as `completion`, minus the routing a watched
    book carries and an app job does not."""
    label = PIPELINE_LABEL.get(job.kind, job.kind)
    title = f"DocProof finished {label.lower()} for {job.filename}"
    subject = f"{DONE_TAGS} {job.filename} — {label}"
    job_rows = [
        ("Document", job.filename, None),
        ("Pipeline", label, None),
        ("Started from", SOURCE_LABEL.get(job.source, job.source), None),
    ]
    # The archive folder, when this job made it into Drive. It is the one link an
    # app job has — its deliverable outlives the container's disk here — and it
    # doubles as a tripwire: a job filed under the wrong folder is visible the
    # same morning. Empty when the archive is off or has not landed yet.
    if getattr(job, "drive_folder_id", ""):
        job_rows.append(("Drive archive", "Open folder",
                         DRIVE_FOLDER.format(job.drive_folder_id)))
    groups: list[tuple[str, list]] = [
        ("Job", job_rows),
        ("Model run", [
            ("Model", job.model, None),
            ("Effort", job.effort, None),
            ("Mode", job.mode, None),
            ("API calls", _int(job.api_calls), None),
        ]),
        ("Settings", _settings_rows(job)),
        ("Cost & tokens", [
            ("Estimated cost", _cost(job), None),
            ("Input tokens", _int(job.input_tokens), None),
            ("Output tokens", _int(job.output_tokens), None),
            ("Cache read", _int(job.cache_read_tokens), None),
            ("Cache write", _int(job.cache_write_tokens), None),
        ]),
        _result_group(job),
        ("Timing", [
            ("Started", job.created_at or "—", None),
            ("Finished", job.updated_at or "—", None),
            ("Elapsed", _elapsed(job), None),
        ]),
    ]
    return subject, _text(title, groups), _html(title, groups)


def send_job_completion(watch_home, job, *, get_key=None,
                        opener=drive._open_url) -> bool:
    """Email the completion log for a finished job, if it is switched on.

    Reuses DocWatch's own `notify_email` and `notify_on_complete`, and its Google
    sign-in, so there is one address and one switch for every pipeline. Anything
    missing — the switch off, no address, no sign-in, a token without the
    gmail.send scope — is a quiet skip, logged and never raised: a job that did
    its work must not fail over an email."""
    from app.settings import get_api_key
    from .settings import GOOGLE_KEY, WatchSettings

    ws = WatchSettings.load(watch_home)
    if not (ws.notify_on_complete and ws.notify_email):
        return False
    if not (ws.client_id and ws.client_secret):
        log.info("Completion email skipped for %s: Google sign-in is not set up.",
                 job.filename)
        return False
    refresh = (get_key or get_api_key)(GOOGLE_KEY)
    if not refresh:
        log.info("Completion email skipped for %s: DocProof is not signed in to "
                 "Google.", job.filename)
        return False
    try:
        token = drive.refresh_access_token(ws.client_id, ws.client_secret,
                                           refresh, opener=opener)
        subject, body, html = completion_for_job(job)
        send(token, ws.notify_email, subject, body, html=html, opener=opener)
        log.info("Emailed %s the completion log for %s.", ws.notify_email,
                 job.filename)
        return True
    except DriveError as e:
        log.warning("Could not email the completion log for %s (%s). If Gmail "
                    "refused the scope, run `docproof-watch auth` again.",
                    job.filename, e)
        return False


class GalleyEmailTransport:
    """Deliver Galley's mid-run wave digests and alarms over DocWatch's own Gmail.

    Satisfies galley.orchestrator.Transport (one ``send(kind, subject, body)``
    method), so the orchestrator stays transport-agnostic — a Twilio or Pushover
    transport slots in later without touching the loop. Reuses the shared notify
    settings and Google sign-in, like send_job_completion, and is the same quiet
    no-op when any of them is missing: a mid-run alert that cannot send must never
    sink the run it is reporting on."""

    def __init__(self, watch_home, *, get_key=None, opener=drive._open_url):
        self.watch_home = watch_home
        self._get_key = get_key
        self._opener = opener

    def send(self, kind: str, subject: str, body: str) -> None:
        from app.settings import get_api_key

        from .settings import GOOGLE_KEY, WatchSettings
        try:
            ws = WatchSettings.load(self.watch_home)
            if not (ws.notify_on_complete and ws.notify_email):
                return
            if not (ws.client_id and ws.client_secret):
                return
            refresh = (self._get_key or get_api_key)(GOOGLE_KEY)
            if not refresh:
                return
            token = drive.refresh_access_token(
                ws.client_id, ws.client_secret, refresh, opener=self._opener)
            tagged = f"{ALERT_TAGS} {subject}" if kind == "alarm" else subject
            send(token, ws.notify_email, tagged, body, opener=self._opener)
        except DriveError as e:
            log.warning("Galley %s alert could not be emailed (%s): %s",
                        kind, e, subject)
        except Exception:                     # noqa: BLE001 - an alert must not sink the run
            log.warning("Galley %s alert failed to send: %s", kind, subject,
                        exc_info=True)


def send_test(watch_home, *, get_key=None, opener=drive._open_url) -> str:
    """Send a sample alert to the configured notify address, to prove the pipe
    end to end — no pass, no formatting, no cost.

    Returns the address it went to. Raises `ValueError` for a setup gap the caller
    should show plainly (no address, no Google sign-in) and `DriveError` for a
    send Gmail refused — most often the missing `gmail.send` scope, fixed by
    signing in again. Uses the watcher's own Google account, the same one a real
    alert would."""
    from app.settings import get_api_key

    from .settings import GOOGLE_KEY, WatchSettings

    ws = WatchSettings.load(watch_home)
    if not ws.notify_email:
        raise ValueError("No notify address is set. Add one with "
                         "`docproof-watch init --notify-email you@example.com`, "
                         "then try again.")
    if not (ws.client_id and ws.client_secret):
        raise ValueError("Google sign-in is not set up yet.")
    refresh = (get_key or get_api_key)(GOOGLE_KEY)
    if not refresh:
        raise ValueError("DocProof is not signed in to Google.")
    token = drive.refresh_access_token(ws.client_id, ws.client_secret, refresh,
                                       opener=opener)
    subject = f"{ALERT_TAGS} Test — DocWatch notifications are working"
    body = ("This is a test from DocWatch, sent because someone asked for one.\n\n"
            "If it reached you, the alerts a pass raises when it needs a person — "
            "a ready author with no Book Original, a book whose status is stuck, "
            "a manuscript that failed to prepare — will reach this inbox too.\n\n"
            "Nothing was prepared, changed or charged for.")
    send(token, ws.notify_email, subject, body, opener=opener)
    return ws.notify_email


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
        log.info("Emailed %s about %d item(s) needing a look.",
                 ws.notify_email,
                 len(report.needs_human) + len(report.missing_source)
                 + len(report.stuck_ready) + len(report.failed))
    except DriveError as e:
        log.warning("Could not email %s about a pass that needs a person (%s). "
                    "If Gmail refused the scope, run `docproof-watch auth` again "
                    "to add send permission.", ws.notify_email, e)


def promo_too_large(token: str, ws, file, job, *,
                    opener=drive._open_url) -> bool:
    """Tell a person a watched book was too big for automatic promo, with the
    numbers and how to run it by hand.

    The watched folder writes promo in a single call over the whole book; a book
    past that limit can't go through automatically, but a person can still run it
    from the app with the override. This is the email that hands it to them.
    Best-effort and gated on an address, exactly like the other mail here — no
    address set, no mail, and a send that fails is logged, never raised."""
    if not ws.notify_email:
        return False
    words = _int(job.words)
    tokens = _int(job.input_tokens)
    subject = f"Promo needs a hand: {file.name} is too big for one pass"
    body = (
        f"DocProof could not write promo copy for “{file.name}” automatically.\n\n"
        f"The book is about {words} words (~{tokens} tokens), over the "
        f"single-pass limit. The watched folder writes promo in one call over "
        f"the whole book, and this manuscript is too large for that.\n\n"
        f"Nothing about the manuscript was changed, and it will not be tried "
        f"again automatically.\n\n"
        f"To run it by hand: open DocProof and go to the Promo tab (or drop the "
        f"book on the main screen and choose “Write promo copy”). Pick a model, "
        f"then tick “Write it anyway” to send the whole book in one call.\n")
    try:
        send(token, ws.notify_email, subject, body, opener=opener)
        log.info("Emailed %s that %s is too big for automatic promo.",
                 ws.notify_email, file.name)
        return True
    except DriveError as e:
        log.warning("Could not email %s about an oversize promo book (%s). If "
                    "Gmail refused the scope, run `docproof-watch auth` again to "
                    "add send permission.", ws.notify_email, e)
        return False


def plan_too_large(token: str, ws, file, job, *,
                   opener=drive._open_url) -> bool:
    """Tell a person a watched book was too big for an automatic marketing plan,
    with the numbers and how to run it by hand.

    The plan twin of `promo_too_large`: the marketing plan reads the whole book
    in one call too, so a book past the single-pass limit can't be planned
    automatically, but a person can still run it from the app with the override.
    Best-effort and gated on an address, exactly like the rest of the mail here."""
    if not ws.notify_email:
        return False
    words = _int(job.words)
    tokens = _int(job.input_tokens)
    subject = f"Marketing plan needs a hand: {file.name} is too big for one pass"
    body = (
        f"DocProof could not write a marketing plan for “{file.name}” "
        f"automatically.\n\n"
        f"The book is about {words} words (~{tokens} tokens), over the "
        f"single-pass limit. The watched folder writes the plan in one call over "
        f"the whole book, and this manuscript is too large for that.\n\n"
        f"Nothing about the manuscript was changed, and it will not be tried "
        f"again automatically.\n\n"
        f"To run it by hand: open DocProof and go to the Promo tab, choose "
        f"“Write a marketing plan”, drop the book, then tick “Write it anyway” "
        f"to send the whole book in one call.\n")
    try:
        send(token, ws.notify_email, subject, body, opener=opener)
        log.info("Emailed %s that %s is too big for an automatic plan.",
                 ws.notify_email, file.name)
        return True
    except DriveError as e:
        log.warning("Could not email %s about an oversize plan book (%s). If "
                    "Gmail refused the scope, run `docproof-watch auth` again to "
                    "add send permission.", ws.notify_email, e)
        return False


__all__ = ["SEND_URL", "send", "summary", "maybe_notify", "completion",
           "maybe_complete", "completion_for_job", "send_job_completion",
           "send_test", "promo_too_large", "plan_too_large"]
