"""Finding one author's subfolder in the Author Folder, and nothing else.

When `subfolders_enabled` is on, the watched folder is the parent — an Author
Folder holding one subfolder per author, `First Last`, hundreds to a thousand
of them. A pass must never list all of those to find the one it wants: it asks
HubSpot who is ready, and asks Drive for *that* name directly.

So this is one scoped query, not an enumeration. Drive's `name =` is
case-sensitive, so an exact hit is trusted and a miss falls back to a
case-insensitive `name contains` narrowed by a normalised compare here — still
one small answer, never the whole parent. A name that resolves to zero folders,
or to more than one, is nobody's to guess: `resolve` returns `None` and the
caller leaves the book where it is and tells a person.
"""
from __future__ import annotations

import logging

from . import drive
from .drive import DriveFile

log = logging.getLogger("docproof.app.watch.folders")


def compose(first: str, last: str) -> str:
    """The folder name an author's record points at: `First Last`, one space.

    HubSpot may store the two names in any case — `QUINTON`, `johnson` — so the
    display form is title-cased for the exact query's best chance, while the
    tolerant fallback carries any drift the title-casing does not (a `McDonald`
    is found by the normalised compare, not by the exact hit)."""
    return " ".join(part.title() for part in
                    f"{first.strip()} {last.strip()}".split())


def _norm(name: str) -> str:
    """Case- and whitespace-insensitive, so `quinton  johnson` and `Quinton
    Johnson` are the same folder. `casefold`, not `lower`, so it holds up
    outside ASCII."""
    return " ".join(name.split()).casefold()


def _escape(value: str) -> str:
    """A folder name safe to drop between the single quotes of a Drive query.
    An apostrophe in a surname would otherwise end the string early."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _search(token: str, parent_id: str, clause: str, *, opener) -> list[DriveFile]:
    """Subfolders of `parent_id` matching one name clause. One page: an author
    has one folder, and a query that needs a second page is already ambiguous
    enough to refuse."""
    q = (f"{clause} and '{_escape(parent_id)}' in parents "
         f"and mimeType = '{drive.FOLDER_MIME}' and trashed = false")
    params = {
        "q": q,
        "fields": f"files({drive.FILE_FIELDS})",
        "pageSize": "20",
        **drive.SHARED_DRIVE_LIST,
    }
    answer = drive._json_call(
        drive._request(drive._url(f"{drive.API}/files", params), token),
        opener=opener, what="find the author's folder")
    return [DriveFile.from_api(raw) for raw in (answer.get("files") or [])
            if isinstance(raw, dict)]


def resolve(first: str, last: str, parent_id: str, token: str, *,
            opener=drive._open_url) -> str | None:
    """The Drive id of this author's subfolder, or `None` if it is not a single
    confident match.

    The exact `name =` query first — one hit, done. Its miss is usually only
    case, so the fallback casts `name contains` and keeps whatever equals the
    wanted name once whitespace and case are set aside. Zero survivors, or more
    than one, is `None`: the caller would rather wait than write a manuscript
    into a folder it guessed at."""
    wanted = compose(first, last)
    if not wanted or not parent_id:
        return None

    exact = _search(token, parent_id, f"name = '{_escape(wanted)}'",
                    opener=opener)
    matches = [f for f in exact if _norm(f.name) == _norm(wanted)]
    if not matches:
        loose = _search(token, parent_id, f"name contains '{_escape(wanted)}'",
                        opener=opener)
        matches = [f for f in loose if _norm(f.name) == _norm(wanted)]

    ids = {f.id for f in matches}
    if len(ids) == 1:
        return next(iter(ids))
    if len(ids) > 1:
        log.warning("%d folders in the Author Folder are named %r; DocProof "
                    "will not guess which is the author's.", len(ids), wanted)
    return None


__all__ = ["compose", "resolve"]
