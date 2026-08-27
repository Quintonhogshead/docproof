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
import logging
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile

from docproof.config import load_config
from docproof.ingest import IngestError
from docproof.providers import build_provider, lookup
from docproof.providers.base import ProviderError
from docproof.quest import LUNA_MODEL, generate_skin
from docproof.quest.skin import SkinError, TEXT_SUFFIXES

from ..settings import CONFIG_PATH, get_api_key

log = logging.getLogger("app.quest")

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
ALLOWED_SUFFIXES = {".docx"} | TEXT_SUFFIXES

# Same bytes, same costume: keyed by content hash so a re-drop (page reload,
# second look) never buys a second call. Process-local and small on purpose.
_CACHE_MAX = 32
_cache: dict[str, dict] = {}


def register(app: FastAPI) -> None:

    @app.post("/api/quest/skin")
    async def quest_skin(file: UploadFile) -> dict:
        """Read a sample of the uploaded manuscript and costume the party."""
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

        digest = hashlib.sha256(data).hexdigest()
        if digest in _cache:
            return {**_cache[digest], "cached": True}

        cfg = load_config(CONFIG_PATH)
        cfg.api.model = LUNA_MODEL
        cfg.api.effort = "low"
        try:
            provider = build_provider(
                cfg, api_key=get_api_key(lookup(LUNA_MODEL).provider))
        except ProviderError as e:
            raise HTTPException(503, detail=(
                f"The skin model is not available: {e}")) from e

        with tempfile.TemporaryDirectory(prefix="quest-skin-") as tmp:
            path = Path(tmp) / Path(name).name
            path.write_bytes(data)
            try:
                result = generate_skin(path, provider)
            except (IngestError, SkinError) as e:
                raise HTTPException(400, detail=str(e)) from e

        payload = {
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
        log.info("Quest skin for %s: %s/%s, %d words, cost %s%s.",
                 result.title, result.skin.genre, result.skin.maturity,
                 result.word_count,
                 "n/a" if result.cost is None else f"${result.cost:.4f}",
                 " (fallback)" if result.fallback else "")
        # A fallback answer is not worth pinning: the author will try again.
        if not result.fallback:
            if len(_cache) >= _CACHE_MAX:
                _cache.pop(next(iter(_cache)))
            _cache[digest] = payload
        return payload
