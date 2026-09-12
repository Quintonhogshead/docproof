"""A PreToolUse hook that keeps a phase brain's hands off the engine's files.

The settle prompt says "never hand-patch an owning row's replacement" and
the verify prompt says "do NOT hand-fix anything it raises", and yet the
settle session on a 3.6k-word test book edited runs/curated/findings.json
twenty-six times (2026-09-06). Every one of those edits was a turn spent
outside the engine's evidence trail, and on a novel that habit alone can
spend the phase's whole turn cap. The driver installs this hook for the
verify and settle sessions; Claude Code runs it before every Edit/Write and
a non-zero exit of 2 refuses the call with the message below fed back to
the brain, which then reaches for the supported verb instead.

Notes, decision logs, memory files and QUESTIONS.md stay writable: only the
engine's own evidence files and the manuscripts are guarded.
"""
from __future__ import annotations

import json
import sys
from pathlib import PurePath

#: Tool names whose file writes the hook inspects.
GUARDED_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
#: Engine evidence files no session may hand-edit, by basename.
GUARDED_NAMES = frozenset({
    "findings.json", "settlement.json", "change_verify.json",
    "finished_walk.json", "approval.json", "state.json", "resources.jsonl",
    "execution-budget.json",
})
#: Manuscript formats: the tracked-changes build is the engine's to write.
GUARDED_SUFFIXES = frozenset({".docx", ".idml", ".indd"})
#: Exit status Claude Code reads as "block this call and tell the model why".
BLOCK_EXIT = 2

MESSAGE = ("edit-guard: {path} is engine evidence; hand-editing it leaves no "
           "trail and does not settle anything. Use the supported verb "
           "(docproof galley settle/verify/residuals/state) and let it write "
           "the file.")


def guarded_path(tool_name: str, tool_input: dict) -> str | None:
    """The offending path when this call must be refused, else None."""
    if tool_name not in GUARDED_TOOLS or not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not isinstance(raw, str) or not raw:
        return None
    path = PurePath(raw)
    if path.name in GUARDED_NAMES or path.suffix.lower() in GUARDED_SUFFIXES:
        return raw
    return None


def decide(payload: dict) -> tuple[int, str]:
    """Exit status and message for one hook payload."""
    path = guarded_path(str(payload.get("tool_name") or ""),
                        payload.get("tool_input") or {})
    if path is None:
        return 0, ""
    return BLOCK_EXIT, MESSAGE.format(path=path)


def main(argv: list[str] | None = None) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    code, message = decide(payload)
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
