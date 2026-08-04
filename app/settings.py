"""Where the app keeps things, and how it finds API keys.

Secrets never land in a settings file. Keys are read from the environment
first (so a terminal user's existing setup keeps working), then from the
system keychain, which is where the Settings screen writes them.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("docproof.app.settings")


def resource_root() -> Path:
    """Where the shipped config and frontend live.

    Inside a PyInstaller bundle that is the unpacked bundle directory; from a
    source checkout it is the repository root. Nothing writable belongs here —
    a packaged .app is read-only, and user state goes to `default_root()`."""
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else Path(__file__).resolve().parent.parent

KEYCHAIN_SERVICE = "docproof"
ENV_VARS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
            "gemini": "GEMINI_API_KEY"}


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def uploads(self) -> Path:
        return self.root / "uploads"

    @property
    def jobs(self) -> Path:
        return self.root / "jobs"

    @property
    def prompts(self) -> Path:
        """Edited error-type prompts. Shadows the shipped config/error_types
        per key, so it lives outside the app bundle and survives updates."""
        return self.root / "error_types"

    @property
    def prep(self) -> Path:
        """A house style set the publisher dropped in themselves. A
        house_styles.yaml here replaces the shipped one wholesale, which is how
        a different template gets prepped for without a new build."""
        return self.root / "prep"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    def ensure(self) -> "Paths":
        for d in (self.root, self.uploads, self.jobs, self.prompts, self.prep):
            d.mkdir(parents=True, exist_ok=True)
        return self


def default_root() -> Path:
    """DOCPROOF_HOME wins so tests (and power users) can relocate everything."""
    env = os.environ.get("DOCPROOF_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "DocProof"


def default_output_dir() -> Path:
    return Path.home() / "Documents" / "DocProof"


@dataclass
class Settings:
    model: str = "claude-sonnet-5"
    min_confidence: str = "medium"
    output_dir: str = field(default_factory=lambda: str(default_output_dir()))
    default_mode: str = "batch"
    # Which file manuscript prep hands back by default: the InDesign-ready
    # .docx, the tracked-changes .docx, or both.
    prep_output: str = "indesign"
    comments: bool = True
    # Ask the model why each change was made. Off is materially cheaper —
    # the reasons are most of what the model writes back.
    explanations: bool = True

    @classmethod
    def load(cls, paths: Paths) -> "Settings":
        if not paths.settings_file.is_file():
            return cls()
        try:
            data = json.loads(paths.settings_file.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Ignoring unreadable settings file (%s); using "
                        "defaults.", e)
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, paths: Paths) -> None:
        paths.ensure()
        paths.settings_file.write_text(
            json.dumps(self.__dict__, indent=2), encoding="utf-8")


# --- API keys -----------------------------------------------------------------

def get_api_key(provider: str) -> str | None:
    env = os.environ.get(ENV_VARS.get(provider, ""))
    if env:
        return env
    try:
        import keyring
        return keyring.get_password(KEYCHAIN_SERVICE, provider)
    except Exception as e:                    # noqa: BLE001 - keyring backends
        log.warning("Keychain unavailable (%s); set %s in the environment "
                    "instead.", e, ENV_VARS.get(provider, "the API key"))
        return None


def set_api_key(provider: str, key: str) -> None:
    import keyring
    keyring.set_password(KEYCHAIN_SERVICE, provider, key)


def delete_api_key(provider: str) -> None:
    try:
        import keyring
        keyring.delete_password(KEYCHAIN_SERVICE, provider)
    except Exception as e:                    # noqa: BLE001
        log.warning("Could not remove stored key for %s: %s", provider, e)


def key_status() -> dict[str, dict]:
    """Whether each provider has a key, and where it came from. Never returns
    the key itself — the frontend has no reason to see it."""
    out = {}
    for provider, var in ENV_VARS.items():
        if os.environ.get(var):
            out[provider] = {"configured": True, "source": "environment"}
        elif get_api_key(provider):
            out[provider] = {"configured": True, "source": "keychain"}
        else:
            out[provider] = {"configured": False, "source": None}
    return out
