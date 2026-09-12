"""Local notes for unattended work; recording a question never sends mail."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

UNATTENDED_ENV = "GALLEY_UNATTENDED"
WORKSPACE_ENV = "GALLEY_WORKSPACE"
NOTES_NAME = "deferred-questions.jsonl"


def is_unattended() -> bool:
    return os.environ.get(UNATTENDED_ENV) == "1"


def record_question(subject: str, body: str, book: str) -> Path:
    import fcntl  # The unattended service runs on Fly/Linux or macOS.
    workspace = Path(os.environ.get(WORKSPACE_ENV) or Path.cwd())
    path = workspace / "runs" / "driver" / NOTES_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    note = {"at": datetime.now(timezone.utc).isoformat(), "subject": subject,
            "body": body, "book": book,
            "phase": os.environ.get("GALLEY_BRAIN_PHASE", ""),
            "disposition": "local note; no reply expected"}
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(note, ensure_ascii=False) + "\n")
    return path


RECOVERY_GUIDANCE = (
    "AUTONOMOUS RECOVERY: nobody checks this work or answers questions before "
    "the final handoff. Inspect the failure evidence and existing checkpoints; "
    "finish only the missing work in this phase. Do not rerun completed paid "
    "work. Bound recovery per individual repair: after two unsuccessful supported "
    "attempts, preserve its wording, record its anchor and failure evidence as a "
    "production flag, and continue independent repairs. Do not label deferred "
    "repairs completed or turn technical limitations into author questions. "
    "Correct command/anchor/artifact mistakes, use supported resume or "
    "repair paths, and verify the result. Decide editorial mechanics from the "
    "book and house rules. Preserve uncertain author wording and put only "
    "missing author knowledge in a justified, anchored margin query for final "
    "delivery; never wait for its answer. Keep technical notes in the decision "
    "log. This is not permission to change source/config/approval hashes, "
    "increase spend, expand scope, omit required coverage, edit engine code, "
    "or bypass a failed gate. Before approval only, correct an invalid plan "
    "and proposed config within the existing scope, routes and budget. If no "
    "safe supported recovery exists, preserve checkpoints and report the "
    "specific failed operation and evidence as an operational failure, not "
    "a question. Return only when this phase's work and required checks are "
    "complete, or the concrete failure is documented."
)
