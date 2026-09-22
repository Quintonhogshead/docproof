"""Warden's own secrets: one Keychain service, environment wins.

A separate Keychain service (`docproof-warden`) rather than reusing the app's
own `docproof` service in `app.settings` — the Warden is a different
principal running as a different macOS user session on a different machine,
with its own admin token and its own Google sign-ins (the DocWatch notify
mailbox for sending, Quinton's own inbox for reading), and mixing its secrets
into the desktop app's Keychain entries would make `docproof-warden delete`
capable of taking the production app's key out from under it.

Same shape as `app.settings.get_api_key` otherwise: the environment is
checked first (so a launchd plist, a `fly ssh` shell, or a test can override
without touching Keychain), then the Keychain, and a missing or unavailable
Keychain is a warning, never a crash — a monitoring agent that raises on its
own missing secret cannot report that the secret is missing.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("docproof.app.warden.secrets")

SERVICE = "docproof-warden"

# name -> the environment variable that overrides the Keychain entry of the
# same name. Spelled out once so the CLI, `missing()` and the docs can all
# point at the same table.
ENV_VARS: dict[str, str] = {
    "warden_token": "DOCPROOF_WARDEN_TOKEN",
    "hubspot": "HUBSPOT_TOKEN",
    "google_client_id": "DOCPROOF_GOOGLE_CLIENT_ID",
    "google_client_secret": "DOCPROOF_GOOGLE_CLIENT_SECRET",
    "google_notify_refresh": "DOCPROOF_GOOGLE_NOTIFY_REFRESH",
    "google_inbox_refresh": "DOCPROOF_GOOGLE_INBOX_REFRESH",
    "fly_token": "FLY_API_TOKEN",
}


def get(name: str) -> str | None:
    """The secret's value, environment first, or `None` if it has neither.

    Never logs the value itself — only ever the name, here and everywhere
    that calls this.
    """
    env_name = ENV_VARS.get(name)
    if env_name:
        env = os.environ.get(env_name)
        if env:
            return env
    try:
        import keyring
        return keyring.get_password(SERVICE, name)
    except Exception as e:                    # noqa: BLE001 - keyring backends vary
        log.warning("Keychain unavailable for %s (%s); set %s in the "
                    "environment instead.", name, e, env_name or name)
        return None


def set(name: str, value: str) -> None:
    """Store `value` in the Keychain under `name`. Raises if Keychain access
    fails outright — unlike `get`, a caller asking to store a secret wants to
    know immediately if that did not happen."""
    import keyring
    keyring.set_password(SERVICE, name, value)


def delete(name: str) -> None:
    try:
        import keyring
        keyring.delete_password(SERVICE, name)
    except Exception as e:                    # noqa: BLE001
        log.warning("Could not remove stored secret for %s: %s", name, e)


def missing(required: list[str]) -> list[str]:
    """Which of `required` have neither an environment override nor a stored
    value. What `warden init`/`warden status` prints so a fresh install says
    exactly what still needs doing."""
    return [name for name in required if not get(name)]


__all__ = ["SERVICE", "ENV_VARS", "get", "set", "delete", "missing"]
