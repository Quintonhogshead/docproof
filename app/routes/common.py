"""Plumbing more than one route module needs.

The functions here are the ones tests stub — money, the Keychain, `open` —
plus the untrusted-input helpers every group resolves uploads through. Route
modules call them as `common.open_path(...)` so a test that patches this
module has patched every caller at once.
"""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from docproof.config import load_config
from docproof.ingest import IngestError
from docproof.pipeline import prepare

from ..accounts import User
from ..jobs import JobStore
from ..settings import CONFIG_PATH, ERROR_DIR, Paths
from ..spending import SpendingLedger, merge_live

log = logging.getLogger("docproof.app")

# Output tokens can't be known in advance; this is a per-request allowance used
# only to turn the estimate into a number with the right order of magnitude.
OUTPUT_TOKEN_GUESS = 600


# --- spend caps ---------------------------------------------------------------

CAP_ENV = "DOCPROOF_DEFAULT_CAP"


def default_cap() -> float | None:
    """The monthly ceiling for a user who has none of their own, or None for no
    ceiling. Set DOCPROOF_DEFAULT_CAP on the server to put a floor of caution
    under every ordinary account at once."""
    raw = os.environ.get(CAP_ENV)
    try:
        return float(raw) if raw else None
    except ValueError:
        log.warning("Ignoring unreadable %s=%r", CAP_ENV, raw)
        return None


def cap_for(user: User | None) -> float | None:
    """What this user may spend this month, or None for no limit. Administrators
    are never capped — that is what God Mode means — and neither is the desktop
    build, where `user` is None."""
    if user is None or user.is_admin:
        return None
    return user.monthly_cap if user.monthly_cap is not None else default_cap()


def store_spend(store: JobStore, owner: str | None = None) -> list:
    """Every job a store still holds, plus the ledger snapshots of the ones it
    has since cleared — merged so a re-recorded job counts once. `owner` scopes
    both halves; None is every owner. The ledger read is what keeps a cleared
    job's cost in the bill, on whichever store it lived in."""
    ledger = SpendingLedger(store.paths.spending_db)
    return merge_live(store.all(owner), ledger.entries(owner))


def watch_spend(watcher) -> list:
    """The watcher's own spend — its live jobs and its own ledger, a separate
    store from the app's. Empty when no watch home exists yet: like
    `WatchRunner.jobs`, this never creates one, so opening the Spending tab on a
    machine that was never pointed at a folder does not grow a watch home."""
    home = Path(watcher.home)
    if not (home / "jobs").is_dir():
        return []
    return store_spend(JobStore(Paths(home)))


def month_spend(store: JobStore, owner: str) -> float:
    """What this owner's jobs have cost so far in the current calendar month,
    including jobs since cleared — their cost lives on in the ledger, so
    clearing the results list can never free cap headroom."""
    prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    ledger = SpendingLedger(store.paths.spending_db).entries(owner)
    rows = merge_live(store.all(owner), ledger)
    return sum(j.cost or 0.0 for j in rows
               if (j.created_at or "").startswith(prefix))


# --- shared route plumbing ----------------------------------------------------

def admin_gate(app: FastAPI, refusal: str):
    """A dependency that lets only administrators through on the web build.

    Shared settings — the style guide, the prompts, the review defaults — are
    one file for the whole server, so on the web build only an administrator
    may change them. The desktop build has one user and no gate, so it passes.
    The refusal names what was being changed, in the caller's words."""
    def gate(request: Request) -> None:
        if app.state.web:
            user = getattr(request.state, "user", None)
            if not (user and user.is_admin):
                raise HTTPException(403, refusal)
    return gate


def resolve_upload(paths: Paths, file_id: str, owner: str) -> Path | None:
    """Map a staged-file id onto a path, refusing anything that escapes the
    owner's own uploads directory. The id comes from the browser, so it is
    untrusted — and staging is per-owner, so requiring the path to sit under
    uploads/<owner> is also what stops one user resolving another's file by
    guessing its id."""
    owner_root = (paths.uploads / owner).resolve()
    candidate = (paths.uploads / owner / file_id).resolve()
    if not candidate.is_relative_to(owner_root) or not candidate.is_file():
        return None
    return candidate


def token_totals(paths: Paths, ids: list[str],
                 owner: str) -> tuple[int, int] | None:
    if not ids:
        return None
    cfg = load_config(CONFIG_PATH)
    tokens = requests = 0
    for file_id in ids:
        path = resolve_upload(paths, file_id, owner)
        if path is None:
            continue
        try:
            prepared = prepare(cfg, path, ERROR_DIR)
        except (IngestError, ValueError):
            continue
        tokens += prepared.est_document_tokens
        requests += prepared.request_count
    return (tokens, requests) if requests else None


def open_path(path: Path, *, reveal: bool = False) -> None:
    """Hand a finished file to whichever application owns it.

    The app runs inside a WKWebView that cannot display a .docx and refuses to
    download one, so serving the bytes over HTTP does nothing visible. The file
    is already on this Mac; `open` is what the user would do themselves."""
    args = ["open", "-R", str(path)] if reveal else ["open", str(path)]
    subprocess.Popen(args)
