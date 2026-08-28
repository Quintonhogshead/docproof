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

import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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

# Waitlist signups spend nothing, so the ceilings only blunt junk floods.
WAITLIST_IP_LIMIT = 20
WAITLIST_IP_WINDOW = 86400.0
WAITLIST_GLOBAL_LIMIT = 5000
WAITLIST_GLOBAL_WINDOW = 86400.0

# One line of JSON per signup; on Fly this lives on the small mounted volume so
# it survives restarts and scale-to-zero.
WAITLIST_PATH = os.environ.get("WAITLIST_PATH", "waitlist.jsonl")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


class RateLimiter:
    """In-memory sliding windows. Per-process on purpose: the prototype runs
    one machine, and a limiter that survives restarts would be more machinery
    than the thing it protects."""

    def __init__(self, per_ip: int, per_ip_window: float,
                 site_limit: int, site_window: float):
        self.per_ip = per_ip
        self.per_ip_window = per_ip_window
        self.site_limit = site_limit
        self.site_window = site_window
        self.by_ip: dict[str, deque] = defaultdict(deque)
        self.site: deque = deque()

    @staticmethod
    def _prune(window: deque, horizon: float) -> None:
        now = time.monotonic()
        while window and now - window[0] > horizon:
            window.popleft()

    def allow(self, ip: str) -> bool:
        self._prune(self.site, self.site_window)
        if len(self.site) >= self.site_limit:
            return False
        window = self.by_ip[ip]
        self._prune(window, self.per_ip_window)
        if len(window) >= self.per_ip:
            return False
        now = time.monotonic()
        window.append(now)
        self.site.append(now)
        return True


class Waitlist:
    """An append-only JSONL of {email, at, source}; duplicates answered warmly
    and not re-written. The whole thing rereads into a set at boot — at
    prototype scale that is the correct database."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.emails: set[str] = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    self.emails.add(json.loads(line)["email"])
                except Exception:  # noqa: BLE001 - a torn line loses one row
                    continue

    def add(self, email: str) -> bool:
        """True if newly added, False if already aboard."""
        email = email.strip().lower()
        if email in self.emails:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "email": email,
                "at": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
        self.emails.add(email)
        return True


def client_ip(request: Request) -> str:
    """The real client behind Fly's proxy, else the socket peer."""
    return (request.headers.get("fly-client-ip")
            or (request.client.host if request.client else "unknown"))


def create_app() -> FastAPI:
    app = FastAPI(title="Spell & Check", version=__version__)
    limiter = RateLimiter(PER_IP_LIMIT, PER_IP_WINDOW,
                          GLOBAL_LIMIT, GLOBAL_WINDOW)
    signups = RateLimiter(WAITLIST_IP_LIMIT, WAITLIST_IP_WINDOW,
                          WAITLIST_GLOBAL_LIMIT, WAITLIST_GLOBAL_WINDOW)
    waitlist = Waitlist(WAITLIST_PATH)
    app.state.limiter = limiter
    app.state.waitlist = waitlist
    static = resource_root() / "app" / "static"

    @app.middleware("http")
    async def guard_skins(request: Request, call_next):
        if request.url.path == "/api/quest/skin" and request.method == "POST":
            if not limiter.allow(client_ip(request)):
                return JSONResponse({"detail": (
                    "The party has costumed a lot of books lately and is "
                    "resting by the fire. Please try again soon.")},
                    status_code=429)
        return await call_next(request)

    quest.register(app)

    @app.post("/api/quest/waitlist")
    async def join_waitlist(request: Request,
                            payload: dict = Body(...)) -> dict:
        """Take an email for when quests open. No account, no verification —
        a raven address, nothing more."""
        email = str(payload.get("email") or "").strip()
        if not EMAIL_RE.match(email) or len(email) > 254:
            return JSONResponse(
                {"detail": "The raven squints at that address — is it right?"},
                status_code=400)
        if not signups.allow(client_ip(request)):
            return JSONResponse(
                {"detail": "The rookery is full for today — try again "
                           "tomorrow."}, status_code=429)
        added = waitlist.add(email)
        log.info("Waitlist %s: %s", "signup" if added else "repeat", email)
        return {"ok": True, "already": not added}

    @app.exception_handler(StarletteHTTPException)
    async def not_found(request: Request, exc: StarletteHTTPException):
        """A wandered-off-the-map page for stray URLs. API and asset misses keep
        their plain JSON 404 so nothing programmatic changes."""
        if exc.status_code == 404 and not request.url.path.startswith(
                ("/api", "/assets", "/healthz")):
            return FileResponse(static / "sc-404.html", status_code=404,
                                headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "version": __version__}

    # The papery pages. /customize is the original candlelit party builder,
    # kept dark on purpose — the workshop, by candlelight.
    def _page(name: str) -> FileResponse:
        return FileResponse(static / name,
                            headers={"Cache-Control": "no-cache"})

    @app.get("/")
    def index() -> FileResponse:
        return _page("sc-index.html")

    @app.get("/quote")
    def quote() -> FileResponse:
        return _page("sc-quote.html")

    @app.get("/party")
    def party() -> FileResponse:
        return _page("sc-party.html")

    @app.get("/pricing")
    def pricing() -> FileResponse:
        return _page("sc-pricing.html")

    @app.get("/customize")
    def customize() -> FileResponse:
        return _page("quest.html")

    # The old front door keeps working for anyone holding the earlier link.
    @app.get("/quest.html")
    def quest_page() -> FileResponse:
        return _page("quest.html")

    from fastapi.staticfiles import StaticFiles
    app.mount("/assets", StaticFiles(directory=static), name="assets")

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
