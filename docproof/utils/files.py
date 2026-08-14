"""Small filesystem helpers shared across passes."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("docproof.utils.files")

# How long a whole-book cache entry is kept. It is keyed by the manuscript text,
# so an entry is worthless the moment that draft is edited — and most drafts are
# reviewed within days, not months. Long enough that a book re-reviewed after a
# round of author changes to OTHER chapters still hits; short enough that the
# folder does not grow forever on a volume shared with the jobs and the database.
CACHE_KEEP_DAYS = 30

# The only files write_cache will ever delete. The cache folder is operator-
# settable (three `cache_dir` keys and DOCPROOF_CACHE_DIR), so it can legitimately
# be pointed at a directory that holds other things — DOCPROOF_HOME itself, where
# settings.json lives and is rewritten only when a setting changes, would be an
# easy mistake to make. A prune that globbed *.json there would delete it. So the
# sweep is restricted to names this module writes.
CACHE_PREFIXES = ("glossary-", "storysheet-", "continuity-")


def write_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` so a reader never sees half of it.

    The whole-book caches are the reason this exists: two runs of the same draft
    can overlap — the worker thread running one review while the ticker thread
    collects another — and a plain write leaves a window where the second run
    reads a truncated file. A torn cache is only a re-read, since every reader
    catches it, but a re-read is a paid whole-book call, which is the one thing
    the cache was added to avoid. Same tmp-then-replace the checkpoint uses.

    The staging name carries the THREAD id as well as the pid, because the two
    writers this guards against live in one interpreter: keyed on pid alone they
    would share a staging path and truncate each other's half-written file, which
    is the exact corruption the function exists to prevent. The replace is atomic
    on POSIX, so that is all they race on — and both wrote the same bytes anyway,
    the cache key being the content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.writing")
    try:
        staging.write_text(text, encoding="utf-8")
        os.replace(staging, path)
    except OSError:
        staging.unlink(missing_ok=True)
        raise


def write_cache(path: Path, text: str, *, keep_days: int = CACHE_KEEP_DAYS) -> None:
    """Save a whole-book cache entry and retire the stale ones beside it.

    Nothing else ever deletes these, and on the hosted build they land on the
    same volume as the job folders and the databases — so the pass that writes
    them is the pass that has to bound them. Only files this module names are
    ever swept (see CACHE_PREFIXES); the folder may not be ours alone. Best-effort
    in both directions: a prune that fails is logged and ignored, because failing
    to tidy a cache is not a reason to fail a review."""
    write_atomic(path, text)
    cutoff = time.time() - keep_days * 86400
    stale = []
    try:
        for prefix in CACHE_PREFIXES:
            stale += [p for p in path.parent.glob(f"{prefix}*.json")
                      if p.stat().st_mtime < cutoff]
    except OSError as e:
        log.debug("could not scan the cache folder to prune it: %s", e)
        return
    for old in stale:
        try:
            old.unlink()
        except OSError:                              # gone already, or read-only
            continue
    if stale:
        log.info("Whole-book cache: retired %d entr(y/ies) older than %d days",
                 len(stale), keep_days)
