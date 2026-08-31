"""Entry point for the packaged Cover Canvas Mac app.

Same reason docproof_desktop.py exists: PyInstaller runs its entry script as
`__main__` with no package around it, so `app/canvas_desktop.py` — which
imports its siblings relatively — cannot be the entry point directly. This
stub is the thing PyInstaller runs; it imports the real module by package
name.
"""
import sys


def main(argv=None) -> int:
    from app.canvas_desktop import main as canvas
    return canvas(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
