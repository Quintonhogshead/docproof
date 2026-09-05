"""God Mode: user and cap management, every route behind require_admin.

Registered only for the web build, which is the only one with accounts.
"""
from __future__ import annotations

import logging
import os
import threading

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from . import common
from .. import settings as settingslib
from ..accounts import AccountError, User
from ..auth import require_admin
from ..jobs import JobStore, read_usage
from ..settings import ENV_VARS, KEY_PROVIDERS
from ..spending import SpendingLedger, merge_live
from ..usage import build_usage

log = logging.getLogger("docproof.app")


class AdminCreateUser(BaseModel):
    email: str
    password: str
    is_admin: bool = False
    monthly_cap: float | None = None


class AdminUpdateUser(BaseModel):
    """Everything an administrator can change about an account. A field left out
    is left alone; monthly_cap is the exception the other way — sending it as
    null clears the cap (back to the server default), which is different from
    not sending it, so the route reads `model_fields_set` to tell them apart."""
    disabled: bool | None = None
    is_admin: bool | None = None
    monthly_cap: float | None = None
    password: str | None = None


class KeyUpdate(BaseModel):
    key: str | None = None


# The key labels the portal shows, matching the desktop Settings screen. Sapling
# is the odd one out — a standalone grammar-check test surface, not a reviewer —
# but its key is managed here alongside the review providers.
KEY_DISPLAY = {"anthropic": "Claude", "openai": "ChatGPT", "gemini": "Gemini",
               "deepinfra": "DeepInfra", "sapling": "Sapling"}


def register(app: FastAPI) -> None:
    accounts = app.state.accounts
    store: JobStore = app.state.store

    def _row(u: User) -> dict:
        return {"id": u.id, "email": u.email, "is_admin": u.is_admin,
                "disabled": u.disabled, "monthly_cap": u.monthly_cap,
                "effective_cap": common.cap_for(u),
                "spent_this_month": round(common.month_spend(store, u.id), 4)}

    @app.get("/api/admin/users", dependencies=[Depends(require_admin)])
    def admin_list_users() -> dict:
        return {"users": [_row(u) for u in accounts.list_users()],
                "default_cap": common.default_cap()}

    @app.post("/api/admin/users", dependencies=[Depends(require_admin)])
    def admin_create_user(body: AdminCreateUser) -> dict:
        try:
            user = accounts.create_user(body.email, body.password,
                                        is_admin=body.is_admin,
                                        monthly_cap=body.monthly_cap)
        except AccountError as e:
            raise HTTPException(400, str(e))
        return _row(user)

    @app.put("/api/admin/users/{user_id}")
    def admin_update_user(user_id: str, body: AdminUpdateUser,
                          me: User = Depends(require_admin)) -> dict:
        target = accounts.get_user(user_id)
        if target is None:
            raise HTTPException(404, "No such user")
        # No locking yourself out: an administrator can't disable their own
        # account or drop their own admin. Another administrator has to.
        if target.id == me.id:
            if body.disabled:
                raise HTTPException(400, "You can't disable your own account.")
            if body.is_admin is False:
                raise HTTPException(
                    400, "You can't remove your own admin access.")
        try:
            if body.password is not None:
                accounts.set_password(user_id, body.password)
        except AccountError as e:
            raise HTTPException(400, str(e))
        if body.disabled is not None:
            accounts.set_disabled(user_id, body.disabled)
        if body.is_admin is not None:
            accounts.set_admin(user_id, body.is_admin)
        # Sent-as-null clears the cap; omitted leaves it. model_fields_set is
        # what tells the two apart.
        if "monthly_cap" in body.model_fields_set:
            accounts.set_cap(user_id, body.monthly_cap)
        return _row(accounts.get_user(user_id))

    @app.get("/api/admin/usage", dependencies=[Depends(require_admin)])
    def admin_usage() -> dict:
        """Every user's month-to-date spend, for the God Mode dashboard."""
        ledger = SpendingLedger(store.paths.spending_db)
        rows = []
        for u in accounts.list_users():
            live = store.all(u.id)
            merged = merge_live(live, ledger.entries(u.id))
            totals = build_usage(merged, read_usage)["totals"]
            rows.append({"id": u.id, "email": u.email,
                         "monthly_cap": u.monthly_cap,
                         "effective_cap": common.cap_for(u), **totals})
        # The watcher's spend is nobody's user total, but it is on the same card,
        # so it gets its own line — otherwise the dashboard adds up to less than
        # the bill.
        watch_rows = common.watch_spend(app.state.watch)
        if watch_rows:
            totals = build_usage(watch_rows, read_usage)["totals"]
            rows.append({"id": "docwatch", "email": "DocWatch (automated)",
                         "monthly_cap": None, "effective_cap": None, **totals})
        return {"users": rows, "default_cap": common.default_cap()}


    def _key_rows() -> list[dict]:
        rows = []
        for provider in KEY_PROVIDERS:
            portal = app.state.keystore.get(provider) is not None
            from_env = bool(app.state.env_keys.get(provider))
            source = "portal" if portal else ("environment" if from_env else None)
            rows.append({"provider": provider,
                         "display": KEY_DISPLAY.get(provider, provider),
                         "configured": bool(settingslib.get_api_key(provider)),
                         "source": source})
        return rows

    @app.get("/api/admin/keys", dependencies=[Depends(require_admin)])
    def admin_keys() -> dict:
        """Which providers have a key and where it came from — never the key
        itself, which is set once and never read back to a browser."""
        return {"keys": _key_rows()}

    @app.put("/api/admin/keys/{provider}", dependencies=[Depends(require_admin)])
    def admin_set_key(provider: str, body: KeyUpdate) -> dict:
        if provider not in KEY_PROVIDERS:
            raise HTTPException(404, "Unknown provider")
        key = (body.key or "").strip()
        if not key:
            raise HTTPException(400, "Paste a key, or use Remove to clear it.")
        app.state.keystore.set(provider, key)
        os.environ[ENV_VARS[provider]] = key      # in force for the next review
        log.info("Admin set the %s key", provider)
        return {"keys": _key_rows()}

    @app.delete("/api/admin/keys/{provider}",
                dependencies=[Depends(require_admin)])
    def admin_clear_key(provider: str) -> dict:
        if provider not in KEY_PROVIDERS:
            raise HTTPException(404, "Unknown provider")
        app.state.keystore.delete(provider)
        # Put back whatever the environment gave at boot, so removing a portal
        # key falls back to a fly secret rather than to nothing.
        restore = app.state.env_keys.get(provider)
        if restore:
            os.environ[ENV_VARS[provider]] = restore
        else:
            os.environ.pop(ENV_VARS[provider], None)
        return {"keys": _key_rows()}

    @app.post("/api/admin/archive/restore",
              dependencies=[Depends(require_admin)])
    def admin_restore_archive() -> dict:
        """Rebuild the job list from the Drive archive after a volume loss.

        Reads every archived job's manifest and recreates the records the store
        is missing; a live record is never overwritten, and each restored job's
        files stream back on first download. Runs in the background — a large
        archive is many small reads — so this returns at once; the tally goes to
        the log. Refused when the archive is off, since there is nothing to read
        from."""
        from ..watch import archive as archivelib
        from ..watch.settings import WatchSettings
        home = app.state.runner.notify_home
        if home is None or not archivelib.is_enabled(WatchSettings.load(home)):
            raise HTTPException(
                400, "The Drive archive is switched off, so there is nothing to "
                     "restore from. Turn it on under DocWatch first.")

        def _run() -> None:
            try:
                archivelib.restore(home, app.state.store)
            except Exception:                 # noqa: BLE001 - a thread's last stop
                log.exception("Archive restore failed")

        thread = threading.Thread(target=_run, name="docproof-restore",
                                  daemon=True)
        app.state.restore_thread = thread
        thread.start()
        return {"started": True}
