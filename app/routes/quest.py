"""The Quest prototype: drop a manuscript, get the party costumed for it.

One endpoint behind the party-assembly page (static/quest.html): the upload
goes to a temp directory, the skin generator reads a sample and makes one cheap
Luna call, and the page gets back the full SkinSpec plus the numbers a person
would want on screen (word count, price band, what the call cost). Nothing is
staged into the job store — a skin is a look, not a job — and repeat drops of
the same bytes are answered from a small in-process cache instead of a second
call.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from docproof.config import load_config
from docproof.ingest import IngestError
from docproof.providers import build_provider, lookup
from docproof.providers.base import ProviderError
from docproof.quest import LUNA_MODEL, generate_skin, iter_sweep
from docproof.quest.skin import SkinError, TEXT_SUFFIXES, read_sample_source

from ..settings import CONFIG_PATH, get_api_key

log = logging.getLogger("app.quest")

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
ALLOWED_SUFFIXES = {".docx"} | TEXT_SUFFIXES

# Same bytes, same costume: keyed by content hash so a re-drop (page reload,
# second look) never buys a second call. Process-local and small on purpose.
_CACHE_MAX = 32
_cache: dict[str, dict] = {}
# The sweep's answer is heavier (six calls) and just as worth pinning by hash,
# so a reload never re-runs the party. Its own map: skin and sweep can be asked
# for separately, and a cached one must not shadow the other.
_sweep_cache: dict[str, dict] = {}


async def _read_upload(file: UploadFile) -> tuple[str, bytes]:
    """Validate an upload the way the party expects and hand back its bytes.
    Raises HTTPException with a sentence a person can read; the caller decides
    what to build from the bytes."""
    name = file.filename or "manuscript"
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, detail=(
            f"Galley reads .docx, .txt, or .md files; {name} is a "
            f"{suffix or 'file with no extension'}."))
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, detail=(
            f"{name} is over the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB "
            f"upload limit."))
    if not data:
        raise HTTPException(400, detail=f"{name} is empty.")
    return name, data


def _provider():
    """The cheap costume/sweep model, built from settings. Raises HTTPException
    503 with a sentence when the model is not configured."""
    cfg = load_config(CONFIG_PATH)
    cfg.api.model = LUNA_MODEL
    cfg.api.effort = "low"
    try:
        return build_provider(
            cfg, api_key=get_api_key(lookup(LUNA_MODEL).provider))
    except ProviderError as e:
        raise HTTPException(503, detail=(
            f"The skin model is not available: {e}")) from e


def _skin_payload(result) -> dict:
    """The costume plus the numbers the page wants, in one JSON-ready dict.
    Shared by the skin endpoint and the sweep's opening `skin` event so both
    speak the same shape."""
    return {
        "skin": result.skin.model_dump(),
        "title": result.title,
        "word_count": result.word_count,
        "band": result.band,
        "model": result.model,
        "cost": result.cost,
        "fallback": result.fallback,
        "error": result.error,
        "alias_collisions": list(result.alias_collisions),
        "cached": False,
    }


def _pin(cache: dict[str, dict], digest: str, payload: dict) -> None:
    """Remember one answer by content hash, evicting the oldest past the cap."""
    if len(cache) >= _CACHE_MAX:
        cache.pop(next(iter(cache)))
    cache[digest] = payload


def _sse(event: str, data: dict) -> str:
    """One Server-Sent Event frame: a named event with a JSON data line."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _scrap_key(catch: dict) -> str:
    """A normalized identity for one first-look scrap, so the same quoted snag
    is never pinned up twice. Keyed on the author's own words: two members
    catching one snag is a duplicate to the reader even when their fixes are
    phrased differently."""
    before = str(catch.get("before") or "").lower()
    before = re.sub(r"\s+", " ", before).strip(" \"'‘’“”.,;:!?…—–-")
    return before


# Below this length, containment stops meaning "same snag" — "a" lives inside
# every sentence — so short keys only ever match exactly.
_CONTAINS_MIN = 6


def _dedupe_catches(catches: list[dict], seen: set[str]) -> list[dict]:
    """Drop catches whose snag was already shown — by this lane or an earlier
    one. A snag counts as shown when its quote matches exactly OR when one
    quote contains the other (Pip pins up "and and", then Bram quotes the whole
    sentence around it). Mutates `seen` so the filter spans the whole sweep."""
    kept = []
    for c in catches:
        key = _scrap_key(c)
        if not key or key in seen:
            continue
        if any(min(len(key), len(s)) >= _CONTAINS_MIN
               and (key in s or s in key) for s in seen):
            continue
        seen.add(key)
        kept.append(c)
    return kept


def register(app: FastAPI) -> None:

    @app.post("/api/quest/skin")
    async def quest_skin(file: UploadFile) -> dict:
        """Read a sample of the uploaded manuscript and costume the party."""
        name, data = await _read_upload(file)

        digest = hashlib.sha256(data).hexdigest()
        if digest in _cache:
            return {**_cache[digest], "cached": True}

        provider = _provider()
        with tempfile.TemporaryDirectory(prefix="quest-skin-") as tmp:
            path = Path(tmp) / Path(name).name
            path.write_bytes(data)
            try:
                result = generate_skin(path, provider)
            except (IngestError, SkinError) as e:
                raise HTTPException(400, detail=str(e)) from e

        payload = _skin_payload(result)
        log.info("Quest skin for %s: %s/%s, %d words, cost %s%s.",
                 result.title, result.skin.genre, result.skin.maturity,
                 result.word_count,
                 "n/a" if result.cost is None else f"${result.cost:.4f}",
                 " (fallback)" if result.fallback else "")
        # A fallback answer is not worth pinning: the author will try again.
        if not result.fallback:
            _pin(_cache, digest, payload)
        return payload

    @app.post("/api/quest/sweep")
    async def quest_sweep(file: UploadFile) -> StreamingResponse:
        """Costume the party, then let all six members take a taste-pass over
        the first pages — streamed as Server-Sent Events so each scrap can be
        pinned up the moment its lane's call lands.

        Events, in order: one `skin` (the costume + quote numbers), up to six
        `lane` (one per member, in completion order), then `done`. A fatal read
        error arrives as a single `error` event instead — the stream has already
        committed a 200 by the time reading happens, so failures ride the body,
        not the status line."""
        name, data = await _read_upload(file)
        digest = hashlib.sha256(data).hexdigest()
        provider = _provider()

        def stream():
            if digest in _sweep_cache:
                cached = _sweep_cache[digest]
                yield _sse("skin", {**cached["skin"], "cached": True})
                for lane in cached["lanes"]:
                    yield _sse("lane", lane)
                yield _sse("done", {"sweep_cost": cached["sweep_cost"],
                                    "cached": True})
                return
            with tempfile.TemporaryDirectory(prefix="quest-sweep-") as tmp:
                path = Path(tmp) / Path(name).name
                path.write_bytes(data)
                try:
                    ms = read_sample_source(path)
                    if not ms.text.strip():
                        raise SkinError(f"{Path(name).name} contains no "
                                        f"readable text.")
                    result = generate_skin(path, provider, ms=ms)
                except (IngestError, SkinError) as e:
                    yield _sse("error", {"detail": str(e)})
                    return
                skin_payload = _skin_payload(result)
                yield _sse("skin", skin_payload)

                lanes, sweep_cost, seen = [], 0.0, set()
                for lane in iter_sweep(ms.text, provider):
                    payload = {"key": lane.key,
                               "catches": _dedupe_catches(lane.catches, seen),
                               "error": lane.error}
                    lanes.append(payload)
                    if lane.cost:
                        sweep_cost += lane.cost
                    yield _sse("lane", payload)
                log.info("Quest sweep for %s: %d/%d lanes with catches, "
                         "cost %s.", result.title,
                         sum(1 for x in lanes if x["catches"]), len(lanes),
                         f"${sweep_cost:.4f}")
                yield _sse("done", {"sweep_cost": sweep_cost})
                if not result.fallback:
                    _pin(_sweep_cache, digest, {"skin": skin_payload,
                                                "lanes": lanes,
                                                "sweep_cost": sweep_cost})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
