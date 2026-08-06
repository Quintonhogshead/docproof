"""Sign-in for the web build.

The desktop app never imports this: it is one person at one Mac, so there is
no one to sign in and nothing to keep apart. The web build calls
`install_auth`, which does three things — puts signed-cookie sessions on the
app, gates every /api route behind a session, and adds the login/logout/me
routes the browser talks to.

The gate is deny-by-default. Anything under /api that isn't on the short open
list needs a signed-in, not-disabled user; a route added next year is private
the moment it exists, without anyone remembering to protect it. The two ways
in that don't need a session — asking to log in, and the health check — are
named explicitly.

Ownership (whose jobs are whose) is the next file's problem, but the pieces it
leans on live here: `current_user`, `require_admin`, and `owner_for`, the last
of which answers "who owns what this request creates" in both builds — a real
user on the web, a single local owner on the desktop.
"""
from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .accounts import Accounts, User

log = logging.getLogger("docproof.app.auth")

SESSION_ENV = "DOCPROOF_SESSION_SECRET"
# What every desktop-build job is owned by. The desktop app has no users, so
# its records all carry this and the ownership checks pass trivially.
LOCAL_OWNER = "local"
# The only /api paths reachable without a session. Everything else is private.
_OPEN_PATHS = {"/api/login"}


class Credentials(BaseModel):
    email: str
    password: str


class _Throttle:
    """Per-email backoff, so the login form can't be tried thousands of times a
    minute. In-memory is enough: there is one server process, and a restart
    clearing it only ever helps the honest user who was locked out."""

    LIMIT = 5
    COOLDOWN = 60.0                            # seconds locked after LIMIT fails

    def __init__(self) -> None:
        self._state: dict[str, tuple[int, float]] = {}   # email -> (fails, until)

    def check(self, email: str) -> None:
        fails, until = self._state.get(email, (0, 0.0))
        if until > time.monotonic():
            raise HTTPException(429, "Too many attempts. Wait a minute, then "
                                     "try again.")

    def failed(self, email: str) -> None:
        fails, _ = self._state.get(email, (0, 0.0))
        fails += 1
        until = time.monotonic() + self.COOLDOWN if fails >= self.LIMIT else 0.0
        self._state[email] = (fails, until)

    def succeeded(self, email: str) -> None:
        self._state.pop(email, None)


def resolve_secret(secret: str | None = None) -> str:
    secret = secret or os.environ.get(SESSION_ENV)
    if not secret:
        raise RuntimeError(
            f"The web build needs a session secret. Set {SESSION_ENV} to a "
            f"long random string (e.g. `python -c \"import secrets; "
            f"print(secrets.token_urlsafe(48))\"`).")
    return secret


def _public_user(user: User) -> dict:
    """What the browser is allowed to know about the signed-in user. Never the
    password hash, never other people's anything."""
    return {"id": user.id, "email": user.email, "is_admin": user.is_admin,
            "monthly_cap": user.monthly_cap}


def _session_user(request: Request, accounts: Accounts) -> User | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = accounts.get_user(uid)
    if user is None or user.disabled:
        # Account deleted or switched off since the cookie was issued.
        return None
    return user


def install_auth(app: FastAPI, accounts: Accounts, *, secret: str | None = None,
                 https_only: bool = True) -> None:
    secret = resolve_secret(secret)
    throttle = _Throttle()
    app.state.accounts = accounts

    async def gate(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in _OPEN_PATHS:
            user = _session_user(request, accounts)
            if user is None:
                return JSONResponse({"detail": "Sign in required"},
                                    status_code=401)
            request.state.user = user
        return await call_next(request)

    # Order matters: middleware added last runs outermost. SessionMiddleware
    # must wrap the gate so request.session is populated before the gate reads
    # it — so it is added second, here.
    app.add_middleware(BaseHTTPMiddleware, dispatch=gate)
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax",
                       https_only=https_only)

    @app.post("/api/login")
    def login(creds: Credentials, request: Request) -> dict:
        email = creds.email.strip().lower()
        throttle.check(email)
        user = accounts.verify_credentials(email, creds.password)
        if user is None:
            throttle.failed(email)
            # One message for both wrong-email and wrong-password, so the form
            # can't be used to find out which addresses have accounts.
            raise HTTPException(401, "Wrong email or password.")
        throttle.succeeded(email)
        request.session["user_id"] = user.id
        log.info("Signed in %s", user.email)
        return _public_user(user)

    @app.post("/api/logout")
    def logout(request: Request) -> dict:
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request) -> dict:
        # The gate has already proven a user; this route only reports it.
        return _public_user(request.state.user)


# -- dependencies routes use ---------------------------------------------------

def current_user(request: Request) -> User:
    """The signed-in user, or 401. On the web the gate has already set this; the
    dependency is what hands it to a route that asks."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Sign in required")
    return user


def require_admin(request: Request) -> User:
    """God Mode: the route runs only for an administrator. Everyone else gets a
    plain 403."""
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(403, "Administrator access required.")
    return user


def owner_for(request: Request) -> str:
    """Who owns what this request touches. A real user id on the web; the single
    local owner on the desktop, where the gate never ran and there is only one
    person anyway."""
    user = getattr(request.state, "user", None)
    return user.id if user else LOCAL_OWNER
