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


CONFIG_PATH = resource_root() / "config" / "default.yaml"
ERROR_DIR = CONFIG_PATH.parent / "error_types"

KEYCHAIN_SERVICE = "docproof"
# The AI providers, plus three that are not providers at all: a read-only GitHub
# token, so a build somebody was sent can ask whether a newer one has been
# released, the watcher's Google refresh token, and the watcher's HubSpot
# private-app token. They live here because they are secrets and this is where
# secrets go — the Keychain, never a file, never returned to the browser.
# `PROVIDERS` stays the list of vendors that review documents, so nothing offers
# to review one with a Drive or HubSpot token — do not add either here.
ENV_VARS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
            "gemini": "GEMINI_API_KEY", "github": "GITHUB_TOKEN",
            "google": "GOOGLE_REFRESH_TOKEN", "hubspot": "HUBSPOT_TOKEN"}
PROVIDERS = ("anthropic", "openai", "gemini")

# Reasoning depth the model runs at, ordered cheapest → deepest. Mirrors the
# Literal in docproof.config.APIConfig.effort. The app never offers "null"
# (omit the parameter entirely) — that is a config-file-only choice, not a
# slider position — so the UI always sends one of these.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


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
    def results(self) -> Path:
        """Where the web build writes finished documents. On a server there is
        no user Documents folder to reach for, and results must survive a
        redeploy exactly as the job records do, so they live here on the volume
        beside them — not in the desktop's ~/Documents/DocProof default."""
        return self.root / "results"

    @property
    def prep(self) -> Path:
        """A house style set the publisher dropped in themselves. A
        house_styles.yaml here replaces the shipped one wholesale, which is how
        a different template gets prepped for without a new build."""
        return self.root / "prep"

    @property
    def promo(self) -> Path:
        """An edited promo generation prompt the publisher dropped in. A
        generation.yaml here shadows the shipped one, the way `prep` does for
        the house style set — so the copy voice can be tuned without a build."""
        return self.root / "promo"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    @property
    def users_db(self) -> Path:
        """The web build's account database. The desktop app never opens it —
        it has one user and no sign-in — so nothing here creates the file; the
        first `docproof-admin add-user` does."""
        return self.root / "users.db"

    @property
    def keys_db(self) -> Path:
        """The web build's provider-key store. A Linux server has no Keychain,
        so keys an administrator sets in the portal live here, on the volume,
        beside the accounts. The desktop app uses the Keychain and never opens
        this."""
        return self.root / "secrets.db"

    @property
    def spending_db(self) -> Path:
        """A job's cost, kept after its folder is gone. Spending is otherwise
        pure arithmetic over live job records, so clearing a job would erase its
        share of the bill; a snapshot is written here just before the folder is
        removed. One JSON object per line, appended, keyed by job id."""
        return self.root / "spending.jsonl"

    def ensure(self) -> "Paths":
        for d in (self.root, self.uploads, self.jobs, self.prompts, self.prep,
                  self.promo):
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
    # ChatGPT Luna is the shipped default: the cheapest model in the catalog,
    # and grammar detection is a precise, well-specified task that does not
    # need a dearer one. The picker opens on this (via /api/models'
    # default_model) on every load.
    model: str = "gpt-5.6-luna"
    min_confidence: str = "medium"
    # Reasoning depth for the model, one of EFFORT_LEVELS. Medium is the
    # shipped default: on a real manuscript it caught ~40% more in-taxonomy
    # errors than low for ~$0.16 more per book, with trap false positives
    # unchanged; high cost 2.3x for zero further recall. Applies to reviews
    # and to manuscript prep alike, since both make model calls.
    effort: str = "medium"
    # Which model reads the whole manuscript for the glossary pass (proper-noun
    # casing + suspected real-word errors). Luna is the cheap default; Opus adds
    # the subtle semantic tail at ~40x the cost. "off" disables the pass. The
    # submission panel offers a picker defaulting here. See GlossaryConfig.
    glossary_model: str = "gpt-5.6-luna"
    output_dir: str = field(default_factory=lambda: str(default_output_dir()))
    default_mode: str = "batch"
    # Which file manuscript prep hands back by default: the InDesign-ready
    # .docx, the tracked-changes .docx, or both.
    prep_output: str = "indesign"
    comments: bool = True
    # Ask the model why each change was made. Off is materially cheaper —
    # the reasons are most of what the model writes back.
    explanations: bool = True
    # The .indd a designer places manuscripts into. Empty until somebody says
    # where it is: there is no sensible default for another house's template.
    indesign_template: str = ""

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


def field_in_settings_file(paths: Paths, name: str) -> bool:
    """Whether the persisted settings file explicitly carries this field.

    Lets the web build pick its own default for a setting the desktop defaults
    differently — output_dir, say — without ever clobbering a value an
    administrator actually chose and saved."""
    try:
        data = json.loads(paths.settings_file.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and name in data


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
