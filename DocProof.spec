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
from PyInstaller.utils.hooks import collect_submodules

# The Dock, the Finder and ⌘-Tab all read this. It is checked in rather than
# generated at build time so a build needs nothing but PyInstaller; to change
# it, edit tools/make_icon.py and run it.
ICON = "app/DocProof.icns"

datas = [
    ("config", "config"),
    ("app/static", "app/static"),
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
        "CFBundleShortVersionString": "0.1.0",
        "NSHighResolutionCapable": True,
        # No document types are declared: files come in by drag-and-drop onto
        # the window, not by DocProof claiming .docx from Word.
        "LSMinimumSystemVersion": "11.0",
    },
)
