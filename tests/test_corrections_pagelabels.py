"""The page a mark is reported on is the page InDesign shows.

A proof's pages are numbered by where they sit in the PDF; a designer navigates by
the folio InDesign prints. They part company the moment a book has front matter —
the seventh leaf of the proof is folio "1" once six roman-numbered leaves precede
it — and a mark reported on "page 7" then sends the designer to the wrong page.
`read_page_folios` reads the folios out of the file's own spreads, and
`page_label_map` aligns each proof page to one, so the review screen and the
InDesign document agree on what "page 7" means.
"""
from __future__ import annotations

import io
import json
import zipfile

from docproof.corrections.idml import page_label_map, read_page_folios
from docproof.corrections.model import CommentDisposition
from docproof.corrections.report import _payload
from docproof.corrections.run import apply_corrections

from .conftest import FIXTURES

LAYOUT = FIXTURES / "layout.idml"

_NS = 'xmlns:idPkg="http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging"'


def _idml(tmp_path, spreads, *, master=True):
    """A minimal IDML whose designmap threads `spreads` — each a list of page
    folios — in order, optionally behind a master spread whose pages must never be
    counted as pages of the book."""
    refs, parts = [], {}
    if master:
        refs.append(f'<idPkg:MasterSpread src="MasterSpreads/M.xml"/>')
        parts["MasterSpreads/M.xml"] = (
            f'<idPkg:MasterSpread {_NS}><MasterSpread Self="m">'
            f'<Page Self="mp" Name="A"/></MasterSpread></idPkg:MasterSpread>')
    for i, folios in enumerate(spreads):
        src = f"Spreads/Spread_{i}.xml"
        refs.append(f'<idPkg:Spread src="{src}"/>')
        pages = "".join(f'<Page Self="p{i}_{j}" Name="{f}"/>'
                        for j, f in enumerate(folios))
        parts[src] = (f'<idPkg:Spread {_NS}><Spread Self="s{i}">{pages}'
                      f"</Spread></idPkg:Spread>")
    parts["designmap.xml"] = (f'<?xml version="1.0"?><idPkg:Document {_NS}>'
                              + "".join(refs) + "</idPkg:Document>")
    path = tmp_path / "book.idml"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.adobe.indesign-idml-package")
        for name, data in parts.items():
            z.writestr(name, data)
    path.write_bytes(buf.getvalue())
    return path


def test_folios_read_in_reading_order_masters_excluded(tmp_path):
    idml = _idml(tmp_path, [["i", "ii"], ["1"]])
    # The master's page "A" is a template, never a page of the book.
    assert read_page_folios(idml) == ["i", "ii", "1"]


def test_a_real_exported_idml_reads_its_page(tmp_path):
    # The bundled InDesign-exported fixture is a single page, folio "1".
    assert read_page_folios(LAYOUT) == ["1"]


def test_front_matter_shifts_the_folio_off_the_physical_page(tmp_path):
    idml = _idml(tmp_path, [["i", "ii"], ["1"]])
    # Proof page 3 — the third leaf — is the page InDesign calls "1".
    assert page_label_map(idml, 3) == {1: "i", 2: "ii", 3: "1"}


def test_a_page_count_that_does_not_match_leaves_pages_alone(tmp_path):
    idml = _idml(tmp_path, [["i", "ii"], ["1"]])
    # A cover the proof carries and the file does not (or the reverse) breaks the
    # one-to-one, so rather than relabel by a broken offset the physical page stands.
    assert page_label_map(idml, 4) == {}
    assert page_label_map(idml, 0) == {}


def test_a_package_with_no_spreads_yields_no_folios(tmp_path):
    idml = _idml(tmp_path, [], master=False)
    assert read_page_folios(idml) == []
    assert page_label_map(idml, 5) == {}


def _disp(page, cid):
    return CommentDisposition(
        id=cid, page=page, kind="highlight", instruction="fix this",
        anchor="a word", disposition="flagged", edit_ids=(), detail="")


def _bare_payload(comments, page_labels):
    class _P:  # a parse result with nothing parsed
        edits = ()
        issues = ()
    class _V:  # a clean verify with nothing to say
        clean = True; structure_changed = False
        paragraphs_before = paragraphs_after = paragraphs_expected = 0
        structure_intended = False
        reconciliations = (); discrepancies = (); changes = ()
    return _payload(source_path="book.idml", after_path=None, parse=_P(),
                    apply=None, verify=_V(), comments=comments,
                    page_labels=page_labels)


def test_the_report_stamps_the_folio_onto_a_flagged_comment():
    payload = _bare_payload((_disp(3, "p3-1"),), {3: "1"})
    item = payload["comments"]["items"][0]
    assert item["page"] == 3            # provenance: the proof page is kept
    assert item["page_label"] == "1"    # display: the folio InDesign shows


def test_without_an_alignment_no_label_is_stamped():
    payload = _bare_payload((_disp(3, "p3-1"),), {})
    assert "page_label" not in payload["comments"]["items"][0]


def test_end_to_end_the_written_report_carries_the_folio(tmp_path):
    """The whole run: a proof whose one page maps to the file's one page, so every
    comment in the written `corrections.json` is stamped with the folio InDesign
    shows — the number the review screen and the downloaded file must agree on."""
    comments = [{"id": "p1-1", "page": 1, "kind": "highlight",
                 "instruction": "vacant", "anchor": "empty"}]
    edits = [{"find": "was empty", "replace": "was vacant",
              "instruction": "vacant", "source": "p1-1"}]
    # One proof page, so the page count matches the file's one page and the folio
    # map is trusted; the text need only be long enough to be a page.
    page_texts = ["She opened the door, the room was empty. " * 2]
    got = apply_corrections(LAYOUT, json.dumps(edits), tmp_path,
                            comments=comments, page_texts=page_texts)
    report = json.loads(got.report_json.read_text(encoding="utf-8"))
    item = report["comments"]["items"][0]
    assert item["page"] == 1
    assert item["page_label"] == "1"
