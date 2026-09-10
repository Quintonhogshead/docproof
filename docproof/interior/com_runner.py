"""Run one generated ExtendScript through Windows InDesign COM in a child process."""
from __future__ import annotations

from pathlib import Path
import sys


def run_script(script: Path, *, prog_id="InDesign.Application") -> str:
    import pythoncom
    from win32com.client import dynamic
    pythoncom.CoInitialize()
    try:
        app = dynamic.Dispatch(prog_id)
        # ScriptLanguage.JAVASCRIPT; passing the contents avoids COM File coercion.
        return str(app.DoScript(script.read_text(encoding="ascii"), 1246973031) or "")
    finally:
        pythoncom.CoUninitialize()


def main():
    try:
        print(run_script(Path(sys.argv[1])))
        return 0
    except Exception as exc:
        print(f"InDesign automation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
