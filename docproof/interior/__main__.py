"""Local worker entry point: python -m docproof.interior."""
from __future__ import annotations

import argparse
import json
import platform
import shutil
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply and verify native InDesign corrections on this Mac.")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor", help="Check this Mac's worker setup without running a book")
    run = subs.add_parser("run", help="Run or resume a local correction job")
    run.add_argument("source", type=Path)
    run.add_argument("--attachment", type=Path, action="append", default=[])
    run.add_argument("--notes-file", type=Path)
    run.add_argument("--work-dir", type=Path, required=True)
    run.add_argument("--rules", type=Path, help="JSON house rules and clarification answers")
    show = subs.add_parser("status", help="Read a saved local job outcome")
    show.add_argument("work_dir", type=Path)
    poll = subs.add_parser("poll", help="Run only the native HubSpot correction queue on this Mac")
    poll.add_argument("--watch-home", type=Path, required=True)
    poll.add_argument("--continuous", action="store_true")
    poll.add_argument("--local-only", action="store_true", help="Prevent Drive uploads and CRM writes regardless of saved settings")
    poll.add_argument("--interval", type=int, default=300, help="Seconds between queue checks (minimum 60)")
    supply = subs.add_parser("supply", help="Supply a manually downloaded HubSpot correction attachment")
    supply.add_argument("file", type=Path)
    supply.add_argument("--file-id", required=True)
    supply.add_argument("--watch-home", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "supply":
        from app.watch.native_files import store_manual_file
        path = store_manual_file(args.file, args.watch_home / "manual-attachments", args.file_id,
                                 filename=args.file.name)
        print(json.dumps({"saved_locally": str(path), "hubspot_file_id": args.file_id}))
        return 0
    if args.command == "poll":
        import time
        from dataclasses import asdict
        from .poller import poll_once
        if args.interval < 60:
            parser.error("--interval must be at least 60 seconds")
        while True:
            try:
                report = poll_once(args.watch_home, local_only=args.local_only)
                print(json.dumps(asdict(report)), flush=True)
                code = 2 if report.failed else 0
            except Exception as exc:
                print(json.dumps({"error": str(exc)}), flush=True)
                code = 2
            if not args.continuous:
                return code
            time.sleep(args.interval)
    if args.command == "doctor":
        from docproof.prep.place import find_indesign
        from galley.codex_runner import codex_home
        home = codex_home()
        data = {"macos": platform.system() == "Darwin", "indesign": find_indesign(),
                "pdf_renderer": shutil.which("pdftoppm"), "codex_cli": shutil.which("codex"),
                "astra_login_present": (home / "auth.json").is_file(),
                "astra_login_directory": str(home),
                "note": "Presence checks only; live account permissions and InDesign automation must also pass."}
        print(json.dumps(data, indent=2))
        return 0 if all(data.get(k) for k in ("macos", "indesign", "pdf_renderer", "codex_cli", "astra_login_present")) else 2
    if args.command == "status":
        path = args.work_dir / "workflow.json"
        if not path.is_file():
            path = args.work_dir / "technical-block.json"
        print(path.read_text())
        return 0
    from .workflow import run_local
    notes = args.notes_file.read_text(encoding="utf-8") if args.notes_file else ""
    rules = json.loads(args.rules.read_text()) if args.rules else {}
    result = run_local(args.source, args.attachment, notes, args.work_dir, rules)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
