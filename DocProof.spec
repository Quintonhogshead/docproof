# PyInstaller spec for the DocProof Mac app.
#
#   .venv/bin/pyinstaller DocProof.spec
#
# Produces dist/DocProof.app. It is unsigned, so the first launch needs
# right-click → Open (Gatekeeper refuses a plain double-click on an unsigned
# bundle, once, per machine).
#
# The two data entries are what the app reads at runtime: the shipped config
# and error-type prompts, and the frontend. app/settings.resource_root() finds
# them through sys._MEIPASS once frozen. Everything the user creates —
# settings, jobs, edited prompts — lives in ~/Library/Application Support and
# is deliberately not in here.
import ast
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


def _version() -> str:
    """Read docproof.__version__ without importing the package — at build time
    the dependencies may not be importable, and the answer is one line of
    text."""
    for line in Path("docproof/__init__.py").read_text("utf-8").splitlines():
        if line.startswith("__version__"):
            return ast.literal_eval(line.split("=", 1)[1].strip())
    raise SystemExit("docproof/__init__.py has no __version__")


def _git(*args: str) -> str:
    try:
        done = subprocess.run(["git", *args], capture_output=True, text=True,
                              timeout=10)
        return done.stdout.strip() if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""                        # a source tarball is not a checkout


VERSION = _version()

# Stamped into the bundle so the running app can say exactly what it is and,
# because it remembers where it was built from, whether that source has moved
# on since. Written under build/ rather than into the source tree.
BUILD_INFO = Path("build/build_info.json")
BUILD_INFO.parent.mkdir(parents=True, exist_ok=True)
BUILD_INFO.write_text(json.dumps({
    "version": VERSION,
    "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "commit": _git("rev-parse", "--short", "HEAD"),
    "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
    # Absolute, so a bundle dragged to /Applications can still find the repo
    # it came from when asked whether there is anything newer.
    "source": str(Path.cwd()),
}, indent=2), encoding="utf-8")

# The Dock, the Finder and ⌘-Tab all read this. It is checked in rather than
# generated at build time so a build needs nothing but PyInstaller; to change
# it, edit tools/make_icon.py and run it.
ICON = "app/DocProof.icns"

datas = [
    ("config", "config"),
    ("app/static", "app/static"),
    (str(BUILD_INFO), "."),
]

hiddenimports = [
    # uvicorn and keyring resolve these by name at runtime, so the analyzer
    # cannot see them in the import graph.
    *collect_submodules("uvicorn"),
    "keyring.backends.macOS",
]

a = Analysis(
    # Not app/desktop.py: PyInstaller runs its entry script as __main__ with
    # no package context, and that module imports its siblings relatively.
    ["docproof_desktop.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DocProof",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DocProof",
)

app = BUNDLE(
    coll,
    name="DocProof.app",
    icon=ICON,
    bundle_identifier="com.docproof.app",
    info_plist={
        "CFBundleName": "DocProof",
        "CFBundleDisplayName": "DocProof",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        # No document types are declared: files come in by drag-and-drop onto
        # the window, not by DocProof claiming .docx from Word.
        "LSMinimumSystemVersion": "11.0",
    },
)
