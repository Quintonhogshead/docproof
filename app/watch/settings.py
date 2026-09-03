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
from dataclasses import dataclass, field, fields
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
    # Off by default, so the flat single-folder behaviour is unchanged. On, the
    # folder above is read as the parent "Author Folder" — one subfolder per
    # author, named `First Last` — and each book is routed into its author's own
    # subfolder rather than sitting loose. Requires HubSpot: the author's name
    # comes from the record's first/last properties, never guessed from the
    # filename. See `folders.py`, `tick._discover` and docs/watch.md.
    subfolders_enabled: bool = False
    model: str = "claude-sonnet-5"
    # Which file prep hands back: the book-styled reading copy (the sketch for
    # the author and the developmental editors), the InDesign-ready .docx, the
    # tracked-changes one, "both" (indesign+tracked) or "all". Same vocabulary
    # as the app's own setting.
    prep_output: str = "book"
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
    # stamp and the folder lock decide. Used only when no fixed times are set
    # below — the two are the same clock in its two moods, "every so often" and
    # "at set times".
    tick_every_minutes: int = 60
    # Fixed times of day for the in-app clock, as "HH:MM" strings. Empty keeps
    # the interval above, so an install that never sets one is unchanged. Non-
    # empty makes the clock run at these times instead — the always-on server's
    # answer to launchd, which it cannot use. Read in `tick_timezone`; decided
    # by `daily.due`.
    tick_at_times: list[str] = field(default_factory=list)
    # The zone `tick_at_times` is read in, an IANA name like "America/New_York".
    # Empty means the machine's own time — which is what a Mac's launchd would
    # use too — but a server runs in UTC, so a name is how "look at nine in the
    # morning" means nine where the people are rather than nine in Greenwich.
    tick_timezone: str = ""
    # An address to email when a pass leaves something a person must sort out — a
    # surname matching two ready Projects, or a manuscript that failed prep.
    # Empty means send nothing. Delivered through Gmail as the signed-in Google
    # account (see `notify.py`), which needs the gmail.send scope — so re-run
    # `docproof-watch auth` after upgrading, or the send answers 403.
    notify_email: str = ""
    # Off by default. On, a full-log email is sent to `notify_email` on every
    # book that finishes — cost, model, tokens, effort, timing, routing, quality
    # and the links — so a successful pass is legible and a book routed into the
    # wrong author's folder is visible the same morning. See `notify.completion`.
    notify_on_complete: bool = False

    # -- Drive output archive --------------------------------------------------
    # The durable off-box record: every finished job's produced files, pushed to
    # a Drive folder and organised Reviews|Prep|Promo -> YYYY-MM -> one folder
    # per job. Off by default, so an install that never sets it up is unchanged.
    # On, the app's own Google sign-in (the same one the completion email uses)
    # archives every job as it finishes, and the ticker backfills the history and
    # retries anything that did not land. Serves app jobs and watched books
    # alike, and is independent of `subfolders_enabled` (the author-facing
    # delivery): the archive is DocProof's record, deliberately its own copy. See
    # `archive.py`.
    archive_enabled: bool = False
    # The archive root: a folder a person makes and pastes the address of, parsed
    # by `folder_id_from` exactly like the watched folder. Empty means the
    # archive stays off even if the switch above is on — there is nowhere to put
    # anything.
    archive_folder_id: str = ""
    # Whether the submitted manuscript is archived beside the outputs. On by
    # default: it is what makes a job re-runnable (retry, re-review, download
    # anyway) after a total loss of the volume, not just readable. Turn off if
    # originals should live only in their own folders.
    archive_include_source: bool = True

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
    # The two structured properties holding the author's name, used only when
    # `subfolders_enabled` is on: the record says QUINTON / JOHNSON, the folder
    # is `Quinton Johnson`. Kept apart from the key property, which stays the
    # thing the filename is matched against.
    hubspot_first_property: str = ""
    hubspot_last_property: str = ""
    # Off by default, so an install that does not name files to the house
    # convention is unchanged. On (subfolder mode only), the watcher prepares
    # only the manuscript named "<surname> - Book Original", the surname taken
    # from `hubspot_last_property` on the ready record — a draft or a
    # developmental copy left in the same folder is ignored rather than guessed
    # at. See `naming.is_source_name` and `tick._discover_ready`.
    require_source_label: bool = False
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
    # DocProof gates on it. Proofing's own pair is below, on the same property.
    hubspot_format_ready_value: str = ""
    # The status value DocProof writes back once the formatted file is in the
    # folder.
    hubspot_format_done_value: str = ""
    # The proofing pair, on the same dropdown: "Ready for Proofing" ->
    # "Proofing Complete". Spelled out rather than left blank because these are
    # the press's actual option values (the internal value equals the label
    # verbatim on the Projects object), and a stage that is off does not read
    # them at all — `proofing_enabled` is the switch, not a blank value.
    hubspot_proof_ready_value: str = "Ready for Proofing"
    hubspot_proof_done_value: str = "Proofing Complete"
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

    # -- Proofing (Galley) -----------------------------------------------------
    # The mechanical proofread: the docproof review ladder, its sweeps, verify
    # and settle, delivered as a tracked-changes manuscript with an editorial
    # letter and a style sheet. Off by default and gated on the same status
    # dropdown formatting uses, moved to its own value pair — so an unchanged
    # prod config behaves exactly as it did before this existed, and turning it
    # on is one switch. Requires `hubspot_enabled`, like promo and the plan.
    # See docs/watch.md and `tick.run_proof`.
    proofing_enabled: bool = False
    # Who actually reads the book:
    #
    #   "app"       DocWatch runs it itself, through the app's galley job — the
    #               same path the panel's Galley job takes. Needs the vendor
    #               keys the rest of the app needs, and spends real money.
    #   "external"  DocWatch only *discovers* the book and waits: it records the
    #               book as awaiting an external practitioner run, emails the
    #               owner the book and its folder, and applies the verdict when
    #               the hand-off files appear in that folder. This is how the
    #               Mac-side practitioner loop delivers — it runs on a Claude
    #               Max subscription, which cannot run on Fly.
    proof_runner: str = "app"
    # What the app runner asks the governor for, when it is the runner. The
    # budget is what one book may cost across all its waves; 0 means "the
    # tier's own default" (see app/routes/jobs.py GALLEY_DEFAULT_BUDGET), which
    # is the same thing the panel does when a request leaves it blank.
    proof_tier: str = "T2"
    proof_budget_usd: float = 0.0

    # -- Promo -----------------------------------------------------------------
    # The third pipeline: a teaser and social posts from a finished manuscript.
    # Off by default and entirely independent of formatting — its own HubSpot
    # values, its own Drive marker, its own state — so an install that never
    # turns it on behaves exactly as before. Gates on the same status dropdown
    # (`hubspot_status_property`) as formatting, moved to its own value pair, and
    # so requires `hubspot_enabled`. Flat-folder mode only for now: under
    # `subfolders_enabled` the promo stage stands aside. See docs/promo.md.
    promo_enabled: bool = False
    # The status value that means "write promo copy for this book now", and the
    # value DocProof moves it to once the copy is delivered. The client's dropdown
    # reads "Ready for Promo text" -> "Promo text finished".
    hubspot_promo_ready_value: str = ""
    hubspot_promo_done_value: str = ""
    # Whether the generated copy ships to Drive with no human in the loop. Off by
    # default — the safer posture for public-facing copy: a hold run generates,
    # marks the book "pending" so it is not generated twice, and waits for a
    # person to approve it in the panel before the two .docx go to the folder and
    # HubSpot moves on. On, a run uploads and writes back in the same tick.
    promo_auto_upload: bool = False
    # Which model writes the copy. Empty falls back to `model` (the formatting
    # one); set it to run promo on a stronger model without changing formatting.
    promo_model: str = ""

    # -- Marketing plan --------------------------------------------------------
    # Promo's third deliverable as its own automated stage: a per-author
    # marketing plan, written from the finished book plus what the author told
    # the press, and delivered to the author's Drive folder. Off by default and
    # independent of formatting and promo copy — its own HubSpot property, its
    # own Drive marker, its own state — so an install that never turns it on
    # behaves exactly as before. Requires `hubspot_enabled`. See app/watch/plan.py
    # and docs/promo.md.
    plan_enabled: bool = False
    # The Marketing Plan property on the CRM record — a *separate* property from
    # the status dropdown the format and promo stages gate on, because the press
    # tracks the plan on its own field: the manual workflow flips it from
    # "Needed" to "Uploaded". Store the property's internal name and its two
    # internal option values.
    hubspot_plan_property: str = ""
    hubspot_plan_needed_value: str = ""
    hubspot_plan_done_value: str = ""
    # The display / pen name property, printed verbatim on the plan. Empty heads
    # the plan with the title alone. Kept apart from first/last, which only
    # resolve which Drive folder is the author's.
    hubspot_pen_property: str = ""
    # Which model writes the plan, and at what reasoning effort. Empty model
    # falls back to `model`, like `promo_model`.
    plan_model: str = ""
    plan_effort: str = "low"
    # Whether the plan ships to Drive with no human in the loop. On by default:
    # the plan is author-facing but not public copy, and the point of the stage
    # is no human involvement. Off holds the draft for approval in the panel the
    # way promo's hold mode does, then a later tick delivers it.
    plan_auto_upload: bool = True
    # Which sibling file in the author's folder is the blurb doc (back-cover
    # synopsis + endorsement blurbs) and which is the publicity-questionnaire
    # export. Case-insensitive regex matched against the filename; an empty
    # pattern disables that input, so the plan degrades to book-only. Both are
    # read only when `plan_enabled`, so they change nothing on a stock install.
    plan_blurb_pattern: str = "blurb|back.?cover|endorsement"
    plan_form_pattern: str = "questionnaire|pnq|publicity"

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

    @property
    def promo_model_or_default(self) -> str:
        """The model promo runs on: its own if set, otherwise the formatting
        model, so an install that never touches `promo_model` still works."""
        return self.promo_model or self.model

    @property
    def plan_model_or_default(self) -> str:
        """The model the marketing plan runs on: its own if set, otherwise the
        formatting model — the promo twin, so a plan install that never touches
        `plan_model` still works."""
        return self.plan_model or self.model

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
