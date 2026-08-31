# PyInstaller spec for the Cover Canvas Mac app.
#
#   .venv/bin/pyinstaller CoverCanvas.spec
#
# Produces "dist/Cover Canvas.app". Unsigned, so the first launch needs
# right-click → Open (Gatekeeper refuses a plain double-click on an unsigned
# bundle, once, per machine).
#
# Deliberately a sibling of DocProof.spec rather than a mode of it: the two
# apps share a codebase but not a Dock identity — a person proofreading and a
# person laying out a cover should find two icons, each opening on its own
# job. Shared build logic (version, build stamp) is inlined the same way for
# the same reason the .spec files themselves are: a build must need nothing
# but PyInstaller.
import ast
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


def _version() -> str:
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
        return ""


VERSION = _version()

BUILD_INFO = Path("build/canvas_build_info.json")
BUILD_INFO.parent.mkdir(parents=True, exist_ok=True)
BUILD_INFO.write_text(json.dumps({
    "version": VERSION,
    "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "commit": _git("rev-parse", "--short", "HEAD"),
    "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
    "source": str(Path.cwd()),
}, indent=2), encoding="utf-8")

# DocProof's icon until Cover Canvas earns its own (tools/make_icon.py is the
# path to one). The Dock still tells them apart by name.
ICON = "app/DocProof.icns"

datas = [
    ("config", "config"),
    # The whole static tree: the canvas SPA and its vendored Konva, plus the
    # cover-studio pages the shell's cover routes serve.
    ("app/static", "app/static"),
    (str(BUILD_INFO), "."),
    *collect_data_files("spylls"),
]

hiddenimports = [
    # uvicorn and keyring resolve these by name at runtime; the analyzer
    # cannot see them in the import graph.
    *collect_submodules("uvicorn"),
    "keyring.backends.macOS",
    # The AI box's brain is imported lazily inside the chat route, so the
    # analyzer never sees it — and a bundle without it would answer 501 on a
    # machine that has everything. The SDK drives the `claude` CLI it finds
    # on the system; the CLI itself is not bundled.
    "claude_agent_sdk",
    *collect_submodules("claude_agent_sdk"),
]

a = Analysis(
    # Not app/canvas_desktop.py: PyInstaller runs its entry script as
    # __main__ with no package context, and that module imports relatively.
    ["docproof_canvas.py"],
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
    name="Cover Canvas",
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
    name="Cover Canvas",
)

app = BUNDLE(
    coll,
    name="Cover Canvas.app",
    icon=ICON,
    bundle_identifier="com.docproof.covercanvas",
    info_plist={
        "CFBundleName": "Cover Canvas",
        "CFBundleDisplayName": "Cover Canvas",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
