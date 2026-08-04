# DocProof on Windows

An assessment, not a plan of record. Nothing here is built.

The short version: **the pipeline is portable and the app shell is portable;
what is Mac-shaped is a handful of small seams plus one large feature.** The
build has to happen on a Windows machine — PyInstaller does not cross-compile,
so this cannot be produced from the Mac that builds the `.dmg`.

## What ports for free

Everything that does the actual work. The review pipeline
(`docproof/ingest.py`, `chunker`, `analyzer`, `validator`, `reassembler`), the
prep pipeline (`docproof/prep/`), the IDML reader and writer
(`docproof/formats/idml/`), and every provider are plain Python over `lxml`,
`pydantic` and vendor SDKs. `.docx` and `.idml` are zip files on every
platform. The test suite should pass on Windows more or less as it stands.

The app shell ports too: FastAPI and uvicorn are cross-platform, the frontend
is a folder of static files, and pywebview uses WebView2 (Edge/Chromium) on
Windows instead of WKWebView. Rendering differences are minor; the CSS is
already plain flexbox and grid.

## The seams, smallest first

| What | Where | Windows |
|---|---|---|
| Opening a result | `_open_path`, [app/main.py](../app/main.py) | `os.startfile(path)`; reveal is `explorer /select,<path>` |
| The 501 platform guards | `app/main.py` (3 of them) | become "is this a supported desktop", not "is this a Mac" |
| Where user state lives | `default_root`, [app/settings.py](../app/settings.py) | `%APPDATA%\DocProof` instead of `~/Library/Application Support` |
| Where results go | `default_output_dir` | `%USERPROFILE%\Documents\DocProof` — already right in spirit |
| Key storage | `get_api_key` etc., `app/settings.py` | none — `keyring` already picks the Windows Credential Manager backend automatically; the `keyring.backends.macOS` hidden import in the spec becomes the Windows one |
| LibreOffice | `CANDIDATES`, [docproof/prep/convert.py](../docproof/prep/convert.py) | `C:\Program Files\LibreOffice\program\soffice.exe` |
| Icon | `app/DocProof.icns`, `tools/make_icon.py` | a `.ico`; the generator is Core Graphics and would be rewritten with Pillow |
| Packaging | `DocProof.spec` `BUNDLE(...)`, `tools/package.sh` | no `BUNDLE` step (that is Mac-only); the artifact is a folder or a one-file `.exe`, and the installer is Inno Setup or just a zip |
| The version stamp | already portable | `build_info.json` and the release check need no change |

None of those are hard. Call it a day or two, most of it mechanical, plus the
time to set up a Windows machine with Python and the toolchain.

## The one large piece: Place into InDesign

[`docproof/prep/place.py`](../docproof/prep/place.py) drives InDesign by
writing an ExtendScript file and handing it over with `osascript` and Apple
events. **Apple events do not exist on Windows.** The equivalent is COM:

```python
import win32com.client
app = win32com.client.Dispatch("InDesign.Application")
app.DoScript(jsx, 1246973031)   # ScriptLanguage.JAVASCRIPT
```

The good news is that **the ExtendScript itself is unchanged** — the `_JSX`
body, the import preferences, the autoflow, and the capitalisation-merge fix
are all InDesign's own scripting DOM, which is identical on both platforms.
What gets rewritten is the ~60 lines around it: finding the application,
handing over the script, reading back the `OK:`/`ERR:` line, and mapping
failures to sentences (Windows has no Apple Events consent prompt, so that
whole error path is replaced by COM registration failures).

Realistically: half a day of work, and a day of fighting InDesign on a Windows
machine to find out how it actually behaves. It also adds `pywin32` as a
Windows-only dependency.

## What it would cost overall

Roughly a week of focused work, most of it not coding: standing up a Windows
build machine, working through the seams, re-verifying the InDesign path by
hand the way the Mac one was, and then maintaining **two** build pipelines and
two sets of packaging instructions forever after.

Worth doing when there is a person who needs it. Not worth doing speculatively
— particularly the InDesign half, which is only useful to someone laying out
books on a PC.

## A cheaper answer, if it is ever just one or two people

The app is a local web server. Someone on Windows could run
`.venv\Scripts\docproof-app` from a checkout and use it in a browser, skipping
the packaging, the icon and the installer entirely. That works today apart from
the `_open_path` seam (the Results buttons would need the download fallback,
which is already built for exactly this case) and LibreOffice discovery. It is
not a product, but it is an afternoon rather than a week.
