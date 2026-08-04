"""Getting a manuscript into .docx when it arrives as something else.

Authors send .doc, .rtf, .odt and occasionally .txt. LibreOffice converts all of
them faithfully — styles and italics survive — so prep shells out to it rather
than growing four more parsers. It is optional: without it, prep simply says so
and asks for a .docx.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("docproof.prep.convert")

# .txt is here on purpose, with a caveat: there is no italic in a text file, so
# emphasis cannot be recovered. Prep converts it and says so in the notes.
CONVERTIBLE = (".doc", ".rtf", ".odt", ".fodt", ".txt", ".wpd", ".docm")
NO_FORMATTING = (".txt",)

CANDIDATES = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/local/bin/soffice",
    "/opt/homebrew/bin/soffice",
)
TIMEOUT_SECONDS = 180


class ConversionError(Exception):
    """A file that could not be turned into a .docx. User-facing message."""


def find_soffice() -> str | None:
    """Where LibreOffice is, or None. Checked at upload time so a manuscript
    that cannot be converted says so immediately."""
    override = os.environ.get("DOCPROOF_SOFFICE")
    if override and Path(override).exists():
        return override
    for candidate in CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("soffice") or shutil.which("libreoffice")


def available() -> bool:
    return find_soffice() is not None


def needs_conversion(path: str | Path) -> bool:
    return Path(path).suffix.lower() in CONVERTIBLE


def loses_formatting(path: str | Path) -> bool:
    return Path(path).suffix.lower() in NO_FORMATTING


def convert_to_docx(path: str | Path, out_dir: str | Path) -> Path:
    """Convert one file and return the .docx LibreOffice wrote."""
    source = Path(path)
    soffice = find_soffice()
    if soffice is None:
        raise ConversionError(
            f"{source.name} is a {source.suffix} file, and turning it into a "
            f"Word document needs LibreOffice, which isn't installed. Install "
            f"it from libreoffice.org, or open the file yourself and Save As "
            f".docx.")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # A private user profile: a headless run must not collide with a
    # LibreOffice the person already has open.
    profile = out / ".soffice-profile"
    cmd = [soffice, "--headless", "--norestore",
           f"-env:UserInstallation=file://{profile}",
           "--convert-to", "docx", "--outdir", str(out), str(source)]
    log.info("Converting %s with LibreOffice", source.name)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as e:
        raise ConversionError(
            f"Converting {source.name} took longer than "
            f"{TIMEOUT_SECONDS} seconds and was stopped.") from e
    except OSError as e:
        raise ConversionError(
            f"Could not run LibreOffice to convert {source.name}: {e}") from e

    produced = out / f"{source.stem}.docx"
    if result.returncode != 0 or not produced.is_file():
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise ConversionError(
            f"LibreOffice could not convert {source.name}"
            + (f": {detail[-1]}" if detail else "."))
    log.info("Converted %s → %s", source.name, produced.name)
    return produced


def ensure_docx(path: str | Path, out_dir: str | Path) -> tuple[Path, str | None]:
    """The .docx for a manuscript, converting it first if it isn't one.

    Returns the path and a note for the prep notes when something about the
    source format is worth saying out loud."""
    source = Path(path)
    if source.suffix.lower() == ".docx":
        return source, None
    if not needs_conversion(source):
        raise ConversionError(
            f"Prep reads Word manuscripts. {source.name} is a "
            f"{source.suffix or 'file with no extension'}, which isn't a "
            f"format it can convert.")
    converted = convert_to_docx(source, out_dir)
    note = None
    if loses_formatting(source):
        note = (f"{source.name} is a plain text file: it carries no italics or "
                f"styles, so emphasis could not be recovered. Anything the "
                f"author italicised will need putting back by hand.")
    return converted, note
