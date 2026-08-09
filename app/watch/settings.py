"""Where the watcher keeps things, and what it was told to watch.

Its own home, deliberately: a separate folder is a separate `owner.lock`, so a
tick and the desktop app can run at the same time without either adopting the
other's jobs. The cost is that the watcher's spending is its own — see
docs/watch.md.

The one secret here — the Google refresh token — is not in this file. It goes
to the Keychain through `app.settings.get_api_key`, the same road the vendor
keys and the GitHub token take.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass, fields
from pathlib import Path

from app.settings import Settings

log = logging.getLogger("docproof.app.watch.settings")

WATCH_SETTINGS = "watch.json"
GOOGLE_KEY = "google"
# What the app calls the HubSpot private-app token in ENV_VARS/Keychain, spelled
# once so the CLI, the preflight and the docs can point at the same thing.
HUBSPOT_KEY = "hubspot"

# What the app calls this in ENV_VARS/Keychain, spelled once so the CLI and the
# docs can point at the same thing.
REFRESH_TOKEN_ENV = "GOOGLE_REFRESH_TOKEN"

_ID = re.compile(r"^[A-Za-z0-9_-]{8,}$")


def default_watch_home() -> Path:
    """Under the app's own folder rather than beside it: it is DocProof's
    state, and one folder in Application Support is enough."""
    env = os.environ.get("DOCPROOF_WATCH_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "DocProof" / "watch"


def folder_id_from(value: str) -> str:
    """The folder id out of whatever was pasted.

    People copy the address bar, not the id, and every Drive URL shape keeps it
    somewhere different — after `/folders/`, or in `?id=`. A bare id passes
    through untouched."""
    text = (value or "").strip()
    if "://" in text:
        parsed = urllib.parse.urlparse(text)
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("id"):
            text = query["id"][0]
        else:
            parts = [p for p in parsed.path.split("/") if p]
            if "folders" in parts:
                # Exactly the one segment after it: a trailing /edit or /view
                # is part of the address, not part of the id.
                parts = parts[parts.index("folders") + 1:][:1]
            text = parts[-1] if parts else ""
    if not _ID.match(text):
        raise ValueError(
            f"{value!r} does not look like a Google Drive folder. Open the "
            "folder in a browser and paste the whole address from the bar.")
    return text


@dataclass
class WatchSettings:
    """What to watch and what to do with it. No secrets."""

    folder_id: str = ""
    model: str = "claude-sonnet-5"
    # Which file prep hands back: the InDesign-ready .docx, the tracked-changes
    # one, or both. Same vocabulary as the app's own setting.
    prep_output: str = "indesign"
    upload_notes: bool = True
    # Off by default: the folder authors and editors look at should hold
    # manuscripts, not apologies. A failure is loud in `status` and the log.
    upload_failure_note: bool = False
    # Not a secret, whatever the name says: Google's own documentation is that
    # an installed application cannot keep one, which is why the flow is built
    # around a loopback redirect rather than around this string.
    client_id: str = ""
    client_secret: str = ""
    # A ceiling on what one tick can spend. Ten manuscripts appearing at once
    # is a person reorganising a folder more often than it is ten new books.
    max_files_per_tick: int = 5
    # Transient failures are worth retrying; the same failure four ticks
    # running is not, so the file is marked and left alone until a human looks.
    max_attempts: int = 3
    output_dir: str = ""
    # The clock inside the running app, which exists because launchd does not
    # run a calendar job while a Mac is asleep and does not go back for the one
    # it missed. Off by default: opening an application should not start
    # spending money.
    auto_ticks: bool = False
    # How often that clock *considers* a pass. It considers; the "last looked"
    # stamp and the folder lock decide.
    tick_every_minutes: int = 60
    # An address to email when a pass leaves something a person must sort out — a
    # surname matching two ready Projects, or a manuscript that failed prep.
    # Empty means send nothing. Delivered through Gmail as the signed-in Google
    # account (see `notify.py`), which needs the gmail.send scope — so re-run
    # `docproof-watch auth` after upgrading, or the send answers 403.
    notify_email: str = ""

    # -- HubSpot ---------------------------------------------------------------
    # All off by default: an install that has never heard of HubSpot behaves
    # exactly as it did before this existed. Turning it on gates every new
    # manuscript on a CRM record — see docs/watch.md and `_gate_hubspot`.
    hubspot_enabled: bool = False
    # The objectType path segment: "deals", "contacts", or a custom object as
    # either its "p_book" name or its "2-XXXXXX" id.
    hubspot_object: str = "deals"
    # The property the filename key is matched against — an ISBN, an order
    # number, whatever the press already writes into the name.
    hubspot_key_property: str = ""
    # How to pull that key out of the filename. Empty means the whole stem;
    # otherwise a regex, and the first capture group (or the whole match).
    hubspot_key_pattern: str = ""
    # A single enumeration ("dropdown") property that carries a book through the
    # pipeline — e.g. a "DocProof" property whose value moves from "Ready for
    # Formatting" to "Formatting Complete". One property with values that
    # transition, rather than a boolean per stage: it is what the production team
    # already reads at a glance, and it leaves room for the proofing values to
    # live on the same property later. Store the option's *internal value*, which
    # HubSpot may spell differently from the label shown in the CRM.
    hubspot_status_property: str = ""
    # The status value that means "format this book now". An editor sets it;
    # DocProof gates on it. Proofing will add its own ready value here later.
    hubspot_format_ready_value: str = ""
    # The status value DocProof writes back once the formatted file is in the
    # folder.
    hubspot_format_done_value: str = ""
    # Optional: a property to write the output filename into, so the CRM record
    # links to what was produced. Empty means do not write it.
    hubspot_output_property: str = ""
    # Whether to write anything back to HubSpot at all. On by default: the gate
    # reads a book's status and, when the formatted file is back, moves it on.
    # Turned off, DocProof still *reads* the gate — a book is prepared only when
    # its record says ready — but never touches the CRM, so a real formatting run
    # can be watched end to end without changing a record. A book still marks
    # itself done in Drive, so it is not prepared twice.
    hubspot_write_back: bool = True

    @classmethod
    def load(cls, home: str | Path) -> "WatchSettings":
        path = Path(home) / WATCH_SETTINGS
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Ignoring unreadable watch settings (%s); using "
                        "defaults.", e)
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, home: str | Path) -> None:
        root = Path(home)
        root.mkdir(parents=True, exist_ok=True)
        (root / WATCH_SETTINGS).write_text(
            json.dumps(self.__dict__, indent=2), encoding="utf-8")

    # -- what the rest of DocProof needs ---------------------------------------

    def results_dir(self, home: str | Path) -> Path:
        """Where finished files land locally.

        Under the watch home rather than ~/Documents/DocProof: the deliverable
        goes back to Drive, so these are working copies, and mixing them into
        the folder the app claims its own results in would leave a person
        wondering which run produced what."""
        return Path(self.output_dir) if self.output_dir else Path(home) / "results"

    def app_settings(self, home: str | Path) -> Settings:
        """The watcher's choices in the shape `JobRunner` already reads."""
        return Settings(model=self.model, prep_output=self.prep_output,
                        output_dir=str(self.results_dir(home)))
