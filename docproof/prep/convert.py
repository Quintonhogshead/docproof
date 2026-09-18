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

# What a Word 97-2003 file starts with; a .docx is a zip and never does.
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Stream names as an OLE2 directory stores them: UTF-16, little-endian.
_WORD_STREAM = "WordDocument".encode("utf-16-le")
SCAN_CHUNK = 1 << 20

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


def holds_legacy_doc(path: str | Path) -> bool:
    """Whether these bytes are a Word 97-2003 .doc, whatever the name says.

    Worth asking because the name lies. A manuscript arrives from Drive as
    "<surname> - Book Original", with no extension at all, and the intake gives
    it the one nearly all of them deserve — .docx. A legacy .doc wearing that
    name then sails past the converter on the strength of a suffix nobody
    checked, and is refused pages later as unreadable. The first eight bytes are
    the only honest account of what the file is, so they get the last word.

    A .doc is an OLE2 compound file, and so is a password-protected .docx: the
    directory tells them apart. A real .doc carries a `WordDocument` stream; an
    encrypted package carries `EncryptedPackage` and no such stream. Only the
    first is convertible, so only the first is claimed here — the encrypted one
    keeps the refusal that tells its author to take the password off.
    """
    source = Path(path)
    try:
        with source.open("rb") as fh:
            if fh.read(len(OLE2_MAGIC)) != OLE2_MAGIC:
                return False
            fh.seek(0)
            # The directory sector holding the stream names can sit anywhere in
            # the file, so this is a scan; the overlap is what keeps a name
            # straddling two chunks from being missed.
            overlap = b""
            while chunk := fh.read(SCAN_CHUNK):
                if _WORD_STREAM in overlap + chunk:
                    return True
                overlap = chunk[-(len(_WORD_STREAM) - 1):]
    except OSError:
        return False
    return False


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
