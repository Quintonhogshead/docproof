"""Warden's own configuration: one `warden.yaml` under its home directory.

Kept apart from `app.watch.settings.WatchSettings` on purpose — the Warden is
not a DocWatch install, it is the thing that watches DocWatch (and Galley, and
Fly, and HubSpot) from outside, so it gets its own home, its own file, and its
own failure mode when the file is missing or unreadable: fall back to
defaults rather than refuse to run, because a monitoring agent that cannot
start because its own config is broken is exactly the outage it exists to
catch.

YAML rather than JSON (`WatchSettings`'s choice) because this file is meant to
be hand-edited on the Mini between ticks, and a plain `warden.yaml` a person
can open and change one threshold in is worth more here than the desktop
app's json-from-a-form path. Unknown top-level keys are kept in `.extra` and
written back untouched, so a newer Warden's config file survives being loaded
and re-saved by an older one — or a field renamed mid-refactor round-trips
instead of vanishing.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("docproof.app.warden.config")

WARDEN_HOME_ENV = "WARDEN_HOME"
CONFIG_FILE = "warden.yaml"

# Fields carried on the dataclass for convenience that are never read from or
# written to warden.yaml. `paused` is a runtime flag: the `pause`/`resume`
# commands flip it in the journal's `kv` table (see journal.py, commands.py)
# so the currently-running tick and the listener agree on it without a file
# write racing a file read; a stray `paused:` line in a hand-edited yaml is
# therefore folded into `extra` rather than silently doing nothing.
_RUNTIME_ONLY = ("paused",)
_STRUCTURED = ("thresholds", "extra")


def home() -> Path:
    """The Warden's own directory: `$WARDEN_HOME`, or `~/.docproof-warden`."""
    env = os.environ.get(WARDEN_HOME_ENV)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".docproof-warden"


@dataclass
class Thresholds:
    """Knobs the stuck rules (`app/warden/rules.py`) fire against."""

    agent_silent_factor: int = 3
    agent_stalled_min: int = 90
    unclaimed_extra_h: int = 1
    held_batch_h: int = 24
    skipped_ticks: int = 2
    request_expiry_h: int = 12
    tick_late_factor: int = 2
    fly_log_lines: int = 200


@dataclass
class WardenConfig:
    """What the Warden watches, how it reaches people, and what it may do.

    No secrets — those live in `app.warden.secrets`, one Keychain service
    away from this plain-text file.
    """

    app_url: str = "https://atmosphere-docproof.fly.dev"
    fly_app: str = "atmosphere-docproof"
    fly_bin: str = "fly"
    # The native InDesign worker's home on this Mac; empty means it is not
    # installed here yet, and native-* rules simply never fire.
    interior_home: str = ""
    # The iMessage handle (phone number or email) allowed to command the
    # Warden. Only text from this handle is ever treated as an instruction —
    # see docs/monitoring-agent-plan.md's "Talking to people".
    owner_handle: str = ""
    owner_name: str = "Quinton"
    imessage_enabled: bool = True
    team_emails: list[str] = field(default_factory=list)
    inbox_label: str = "DocProof"
    inbox_senders: list[str] = field(default_factory=lambda: [
        "docwatch@", "fly.io", "google.com", "hubspot.com",
    ])
    quiet_start: str = "23:00"
    quiet_end: str = "07:00"
    timezone: str = "America/New_York"
    max_texts_per_hour: int = 4
    tick_interval_min: int = 20
    listen_interval_min: int = 2
    # claude | codex | none — see app/warden/harness.py.
    harness: str = "claude"
    harness_bin: str = ""
    harness_model: str = ""
    thresholds: Thresholds = field(default_factory=Thresholds)
    # Never read from or written to warden.yaml; see `_RUNTIME_ONLY` above.
    paused: bool = False
    # Unknown keys from the file on disk, preserved verbatim across a
    # load/save round trip. Never set this by hand; `load` populates it.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, home_dir: str | Path | None = None) -> "WardenConfig":
        """Read `warden.yaml` from `home_dir` (default: `home()`).

        A missing or unreadable file is not an error — it means "use the
        defaults", the same posture `WatchSettings.load` takes, because a
        Warden that cannot start over a corrupt config file cannot alert
        anyone that its config file is corrupt.
        """
        root = Path(home_dir) if home_dir is not None else home()
        path = root / CONFIG_FILE
        if not path.is_file():
            return cls()
        try:
            data = yaml.safe_load(path.read_text("utf-8"))
        except (OSError, yaml.YAMLError) as e:
            log.warning("Ignoring unreadable warden config (%s); using "
                        "defaults.", e)
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "WardenConfig":
        known = {f.name for f in fields(cls)} - set(_STRUCTURED) - set(_RUNTIME_ONLY)
        kwargs: dict[str, Any] = {k: v for k, v in data.items() if k in known}

        thresholds_data = data.get("thresholds")
        if isinstance(thresholds_data, dict):
            t_known = {f.name for f in fields(Thresholds)}
            kwargs["thresholds"] = Thresholds(
                **{k: v for k, v in thresholds_data.items() if k in t_known})

        extra = {k: v for k, v in data.items()
                 if k not in known and k != "thresholds"}
        return cls(**kwargs, extra=extra)

    def save(self, home_dir: str | Path | None = None) -> None:
        """Write `warden.yaml` under `home_dir` (default: `home()`)."""
        root = Path(home_dir) if home_dir is not None else home()
        root.mkdir(parents=True, exist_ok=True)
        (root / CONFIG_FILE).write_text(
            yaml.safe_dump(self._to_dict(), sort_keys=False),
            encoding="utf-8")

    def _to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for f in fields(self):
            if f.name in _RUNTIME_ONLY or f.name == "extra":
                continue
            if f.name == "thresholds":
                data["thresholds"] = asdict(self.thresholds)
            else:
                data[f.name] = getattr(self, f.name)
        # Appended last, after every field this version knows about, so a
        # diff on the file reads as "new stuff at the bottom" rather than
        # scrambling the order a person is used to editing.
        data.update(self.extra)
        return data


__all__ = ["WardenConfig", "Thresholds", "home", "WARDEN_HOME_ENV", "CONFIG_FILE"]
