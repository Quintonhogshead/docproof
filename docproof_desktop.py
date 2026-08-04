"""Entry point for the packaged Mac app.

PyInstaller runs its entry script as `__main__`, with no package around it, so
`app/desktop.py` cannot be the entry point directly — its relative imports
would have nothing to resolve against. This stub is the thing PyInstaller
runs; it imports the real module by package name.
"""
from app.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
