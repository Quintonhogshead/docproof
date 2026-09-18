"""What a downloaded manuscript actually is, as opposed to what it is called.

Drive names are a claim, and for an extensionless "<surname> - Book Original"
the claim is one the intake itself invented. A Word 97-2003 file under that
name used to be handed to prep as a .docx, skip the converter on the strength
of the suffix, and be refused as unreadable — a manuscript LibreOffice could
have read, lost for want of four characters. These tests hold the bytes to
their word.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.watch import prep
from app.watch.drive import DOCX_MIME, GOOGLE_DOC_MIME, DriveFile
from docproof.prep import convert
from docproof.prep.convert import holds_legacy_doc

from .fakes import drive_entry, fake_drive

ZIP = b"PK\x03\x04" + b"a real .docx is a zip" * 8


def ole2(*streams: str, padding: int = 0) -> bytes:
    """Enough of an OLE2 compound file for the sniffer: the signature, then
    the directory's stream names as it stores them, in UTF-16."""
    body = b"".join(name.encode("utf-16-le") + b"\x00\x00" for name in streams)
    return convert.OLE2_MAGIC + b"\x00" * padding + body


def drive_file(name: str, *, mime: str = DOCX_MIME) -> DriveFile:
    return DriveFile(id="f-1", name=name, mime_type=mime, app_properties={})


# --- reading the bytes --------------------------------------------------------

def test_word_97_bytes_are_recognised_whatever_the_file_is_called(tmp_path):
    legacy = tmp_path / "Morales - Book Original.docx"
    legacy.write_bytes(ole2("WordDocument", "1Table"))

    assert holds_legacy_doc(legacy) is True


def test_a_real_docx_is_not_a_legacy_doc(tmp_path):
    real = tmp_path / "Book.docx"
    real.write_bytes(ZIP)

    assert holds_legacy_doc(real) is False


def test_a_password_protected_docx_is_not_claimed(tmp_path):
    """Encrypted OOXML is an OLE2 file too, and LibreOffice cannot convert
    what it cannot open. It keeps the refusal that tells the author to take
    the password off."""
    locked = tmp_path / "Book.docx"
    locked.write_bytes(ole2("EncryptedPackage", "EncryptionInfo"))

    assert holds_legacy_doc(locked) is False


def test_a_stream_name_split_across_two_reads_is_still_found(tmp_path,
                                                             monkeypatch):
    """The directory can sit anywhere in the file, so the scan is chunked —
    and a name straddling the seam must not fall through it."""
    monkeypatch.setattr(convert, "SCAN_CHUNK", 64)
    legacy = tmp_path / "Book.docx"
    legacy.write_bytes(ole2("WordDocument", padding=60))

    assert holds_legacy_doc(legacy) is True


def test_a_file_that_is_not_there_is_not_a_doc(tmp_path):
    assert holds_legacy_doc(tmp_path / "gone.docx") is False


# --- renaming what was downloaded ---------------------------------------------

def test_a_legacy_doc_called_docx_is_renamed_to_doc(tmp_path):
    downloaded = tmp_path / "Morales - Book Original.docx"
    downloaded.write_bytes(ole2("WordDocument"))

    renamed = prep.as_its_bytes_say(downloaded)

    assert renamed.name == "Morales - Book Original.doc"
    assert renamed.read_bytes() == ole2("WordDocument")
    assert not downloaded.exists()


def test_a_real_docx_is_left_where_it_is(tmp_path):
    downloaded = tmp_path / "Book.docx"
    downloaded.write_bytes(ZIP)

    assert prep.as_its_bytes_say(downloaded) == downloaded


def test_a_version_number_in_the_name_survives_the_rename(tmp_path):
    downloaded = tmp_path / "Book v1.2 Original.docx"
    downloaded.write_bytes(ole2("WordDocument"))

    assert prep.as_its_bytes_say(downloaded).name == "Book v1.2 Original.doc"


# --- the whole fetch ----------------------------------------------------------

@pytest.fixture
def libreoffice(monkeypatch):
    """LibreOffice, minus LibreOffice: records what it was asked to convert
    and writes the .docx it would have written."""
    converted: list[Path] = []

    def fake(path, out_dir):
        source = Path(path)
        converted.append(source)
        produced = Path(out_dir) / f"{source.stem}.docx"
        produced.write_bytes(ZIP)
        return produced

    monkeypatch.setattr(convert, "convert_to_docx", fake)
    return converted


def test_a_doc_in_the_watched_folder_is_converted_rather_than_refused(
        tmp_path, libreoffice):
    """The Morales failure, end to end: an extensionless Drive file holding
    Word 97-2003 bytes now reaches prep as a converted .docx."""
    opener = fake_drive({"f-1": drive_entry("Morales - Book Original")},
                        docx=ole2("WordDocument", "1Table"))

    local = prep.fetch("at-1", drive_file("Morales - Book Original"), tmp_path,
                       opener=opener)

    assert [p.name for p in libreoffice] == ["Morales - Book Original.doc"]
    assert local.name == "Morales - Book Original.docx"
    assert local.read_bytes() == ZIP


def test_a_docx_in_the_watched_folder_is_not_sent_to_libreoffice(
        tmp_path, libreoffice):
    opener = fake_drive({"f-1": drive_entry("Wolves.docx")}, docx=ZIP)

    local = prep.fetch("at-1", drive_file("Wolves.docx"), tmp_path,
                       opener=opener)

    assert libreoffice == []
    assert local.name == "Wolves.docx"


def test_a_google_doc_is_exported_and_believed(tmp_path, libreoffice):
    """An export is a .docx by construction — there is nothing to sniff, and
    nothing downstream should pay to find that out."""
    opener = fake_drive({"f-1": drive_entry("Wolves", mime=GOOGLE_DOC_MIME)},
                        docx=ZIP)

    local = prep.fetch("at-1", drive_file("Wolves", mime=GOOGLE_DOC_MIME),
                       tmp_path, opener=opener)

    assert libreoffice == []
    assert local.name == "Wolves.docx"
