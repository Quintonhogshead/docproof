"""Spell & Check, standing on its own: the public quest prototype server.

A deliberately tiny FastAPI app — the party page at `/` and the skin endpoint,
and nothing else. No accounts, no job store, no volume, no admin: the DocProof
web build keeps all of that; this site exists so the prototype can live at its
own address under its own name. The only money it can spend is the skin call
(a fraction of a cent), so the only guard it needs is a rate limit.

Run locally with `spell-and-check` (console script); on Fly it boots from
Dockerfile.quest + fly.quest.toml with OPENAI_API_KEY set as a secret.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from docproof import __version__

from .routes import quest
from .settings import resource_root

log = logging.getLogger("app.quest_site")

# Per-IP and whole-site ceilings on skin generation. Generous for a person,
# useless for a scraper: the point is that nobody can run up the OpenAI bill
# or hammer the box, not that usage is metered.
PER_IP_LIMIT = 12          # skins per IP per window
PER_IP_WINDOW = 3600.0     # seconds
GLOBAL_LIMIT = 600         # skins site-wide per day
GLOBAL_WINDOW = 86400.0


class RateLimiter:
    """In-memory sliding windows. Per-process on purpose: the prototype runs
    one machine, and a limiter that survives restarts would be more machinery
    than the thing it protects."""

    def __init__(self):
        self.by_ip: dict[str, deque] = defaultdict(deque)
        self.site: deque = deque()

    @staticmethod
    def _prune(window: deque, horizon: float) -> None:
        now = time.monotonic()
        while window and now - window[0] > horizon:
            window.popleft()

    def allow(self, ip: str) -> str | None:
        """None to proceed, else a sentence for the author."""
        self._prune(self.site, GLOBAL_WINDOW)
        if len(self.site) >= GLOBAL_LIMIT:
            return ("The party has costumed a lot of books today and is "
                    "resting by the fire. Please try again tomorrow.")
        window = self.by_ip[ip]
        self._prune(window, PER_IP_WINDOW)
        if len(window) >= PER_IP_LIMIT:
            return ("That's a lot of manuscripts in one hour! Give the "
                    "party a short rest and try again soon.")
        now = time.monotonic()
        window.append(now)
        self.site.append(now)
        return None


def client_ip(request: Request) -> str:
    """The real client behind Fly's proxy, else the socket peer."""
    return (request.headers.get("fly-client-ip")
            or (request.client.host if request.client else "unknown"))


def create_app() -> FastAPI:
    app = FastAPI(title="Spell & Check", version=__version__)
    limiter = RateLimiter()
    app.state.limiter = limiter
    static = resource_root() / "app" / "static"

    @app.middleware("http")
    async def guard_skins(request: Request, call_next):
        if request.url.path == "/api/quest/skin" and request.method == "POST":
            refusal = limiter.allow(client_ip(request))
            if refusal:
                return JSONResponse({"detail": refusal}, status_code=429)
        return await call_next(request)

    quest.register(app)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "version": __version__}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static / "quest.html",
                            headers={"Cache-Control": "no-cache"})

    # The page's own asset path keeps working if anything links it directly.
    @app.get("/quest.html")
    def quest_page() -> FileResponse:
        return index()

    return app


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    if not os.environ.get("OPENAI_API_KEY"):
        # Boot anyway: the page still renders and the endpoint answers 503
        # with a sentence, which beats a crash loop while secrets are sorted.
        log.warning("OPENAI_API_KEY is not set; skin generation will fail "
                    "until it is.")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
