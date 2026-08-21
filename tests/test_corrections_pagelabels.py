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


# --- the folios the proof itself prints (the count-mismatch fallback) ----------

from docproof.corrections.pagemap import printed_folio_labels  # noqa: E402


def _body_pages(n, offset, *, skip=()):
    """`n` proof pages whose running feet print folios `1+offset … n+offset`,
    except the pages in `skip` (chapter openers print none)."""
    out = []
    for i in range(1, n + 1):
        body = f"Body text of proof page {i}, long enough to be a page.\n"
        folio = "" if i in skip else f"\n{i + offset}"
        out.append(body + folio)
    return out


def test_printed_folios_label_the_run_by_consensus():
    labels = printed_folio_labels(_body_pages(12, 6))
    assert labels[1] == "7" and labels[12] == "18"
    assert len(labels) == 12


def test_a_chapter_opener_printing_no_folio_is_interpolated():
    labels = printed_folio_labels(_body_pages(12, 6, skip=(5, 9)))
    # The openers sit inside the consensus run, so their folio is arithmetic.
    assert labels[5] == "11" and labels[9] == "15"


def test_running_head_folios_count_too():
    pages = [f"{i + 3}  THE LONG ROAD\nBody text of proof page {i}."
             for i in range(1, 11)]
    labels = printed_folio_labels(pages)
    assert labels[1] == "4" and labels[10] == "13"


def test_scattered_numbers_never_become_folios():
    # Numbers in body copy with no consistent page offset must not be believed.
    pages = [f"In {1900 + i * 7} the harvest failed on page-like text {i}."
             for i in range(1, 13)]
    assert printed_folio_labels(pages) == {}


def test_too_few_reads_never_label():
    assert printed_folio_labels(_body_pages(4, 0)) == {}


def test_front_matter_outside_the_run_keeps_its_physical_page():
    # Two unnumbered leaves, then a body printing folios offset by -2.
    pages = ["HALF TITLE", "FULL TITLE"] + _body_pages(10, 0)[:10]
    # Rebuild with the body starting at proof page 3: folio = page - 2.
    pages = ["HALF TITLE", "FULL TITLE"] + [
        f"Body text of proof page {i}.\n{i - 2}" for i in range(3, 13)]
    labels = printed_folio_labels(pages)
    assert 1 not in labels and 2 not in labels
    assert labels[3] == "1" and labels[12] == "10"


def test_the_fallback_labels_a_mismatched_proof_end_to_end(tmp_path):
    """A proof one page longer than the file (a cover), so the spread map is
    refused — and the folios the proof itself prints label the run instead:
    the comment on proof page 8 is shown as the page InDesign calls 3."""
    comments = [{"id": "p8-1", "page": 8, "kind": "highlight",
                 "instruction": "vacant", "anchor": "empty"}]
    edits = [{"find": "was empty", "replace": "was vacant",
              "instruction": "vacant", "source": "p8-1"}]
    # Fourteen proof pages against a one-page file: pages 6..14 print folios 1..9.
    page_texts = (["COVER"] + [f"Front matter leaf {i}" for i in range(2, 6)]
                  + [f"Body text of the book, page {i}.\n{i - 5}"
                     for i in range(6, 15)])
    got = apply_corrections(LAYOUT, json.dumps(edits), tmp_path,
                            comments=comments, page_texts=page_texts)
    report = json.loads(got.report_json.read_text(encoding="utf-8"))
    item = report["comments"]["items"][0]
    assert item["page"] == 8
    assert item["page_label"] == "3"
    assert report["pages"]["labeled"] == 9


# --- the file's own folios anchored to a mismatched proof ----------------------

from docproof.corrections.pagemap import anchored_folio_labels  # noqa: E402
from docproof.corrections.run import _label_ladder  # noqa: E402


def _cover_proof():
    """A 13-page proof of a 12-page file: a cover the file does not carry, two
    unnumbered front-matter leaves, then a body printing folios 1–10."""
    return (["COVER"] + ["HALF TITLE", "FULL TITLE"]
            + [f"Body text of the book, leaf {p}.\n{p - 3}"
               for p in range(4, 14)])


def test_anchored_labels_are_the_files_own_names(tmp_path):
    folios = ["i", "ii"] + [str(n) for n in range(1, 11)]
    labels = anchored_folio_labels(folios, _cover_proof())
    # The printed arabic folios anchor the shift; the labels handed out are the
    # file's own names — the roman front matter included, which no printed read
    # could ever produce — and the cover, a page the file does not carry, gets
    # no label rather than a wrong one.
    assert 1 not in labels
    assert labels[2] == "i" and labels[3] == "ii"
    assert labels[4] == "1" and labels[13] == "10"


def test_anchored_labels_refuse_without_consensus():
    # One page printing one number is no anchor at all.
    assert anchored_folio_labels(["1"], ["Body.\n1"]) == {}
    # And a file with no folios can anchor nothing.
    assert anchored_folio_labels([], _cover_proof()) == {}


def test_the_ladder_prefers_spreads_then_anchoring_then_printed(tmp_path):
    idml = _idml(tmp_path, [["i", "ii"], [str(n) for n in range(1, 11)]])
    proof = _cover_proof()
    # Counts match (12 == 12): the spreads map holds outright.
    labels, source = _label_ladder(idml, 12, proof[1:])
    assert source == "spreads" and labels[1] == "i"
    # A cover breaks the one-to-one: the file's folios are anchored instead.
    labels, source = _label_ladder(idml, 13, proof)
    assert source == "anchored"
    assert labels[2] == "i" and labels[4] == "1"
    # No spreads to anchor: the printed folios alone are left.
    bare = _idml(tmp_path, [], master=False)
    labels, source = _label_ladder(bare, 13, proof)
    assert source == "printed"
    assert 2 not in labels and labels[4] == "1"


def test_flag_wording_speaks_the_folio_not_the_proof_page(tmp_path):
    """A flag's detail is read by the person who opens the finished IDML, so the
    page it names must be the page that file shows. Proof page 8 is folio "3"
    here (a 14-page proof of a 1-page file, printed-folio fallback), and the
    ambiguous-edit detail must say 3, not 8."""
    edits = [{"find": "was", "replace": "is", "page": 8}]
    page_texts = (["COVER"] + [f"Front matter leaf {i}" for i in range(2, 6)]
                  + [f"Body text of the book, leaf {i}.\n{i - 5}"
                     for i in range(6, 15)])
    got = apply_corrections(LAYOUT, json.dumps(edits), tmp_path,
                            page_texts=page_texts)
    report = json.loads(got.report_json.read_text(encoding="utf-8"))
    detail = report["apply"]["flagged"][0]["detail"]
    assert "page 3" in detail
    assert "page 8" not in detail


def test_the_corrected_file_carries_the_folios_the_labels_promise(tmp_path):
    """The invariant the whole labelling rests on: applying text edits never
    touches a spread, so the finished IDML's folios are the source's — and the
    labels the report speaks in are, literally, the downloaded file's."""
    from docproof.corrections.idml import read_page_folios as folios_of
    got = apply_corrections(LAYOUT,
                            json.dumps([{"find": "Their were",
                                         "replace": "There were"}]),
                            tmp_path)
    assert folios_of(got.corrected_idml) == folios_of(LAYOUT) == ["1"]


def test_a_resolution_in_the_notes_names_its_page():
    from docproof.corrections.report import _markdown
    payload = _bare_payload((), {})
    payload["queue"] = [{"id": "q1", "page": 7, "page_label": "1",
                         "instruction": "fix this", "anchor": "a word",
                         "options": [], "targets": [], "resolved": True}]
    payload["resolutions"] = [{"item_id": "q1", "kind": "picked",
                               "after": "the corrected line"}]
    md = _markdown(payload)
    assert "**page 1** — picked a placement" in md


def test_a_pageless_run_of_a_page_citing_list_warns_loudly(tmp_path):
    """A marked-PDF list re-supplied without its sidecar pages (a reload
    discards them) loses every mark's page narrowing — a repeated find can
    only be flagged. The report must say that up front, count the citing
    edits, and the flag wording must blame the missing pages, not a page map
    that never existed."""
    edits = [{"find": "was", "replace": "is", "page": 3, "occurrence": 2,
              "instruction": "Add comma after"}]
    got = apply_corrections(LAYOUT, json.dumps(edits), tmp_path)   # no pages
    report = json.loads(got.report_json.read_text(encoding="utf-8"))
    assert report["pages"] == {"placed": 0, "total": 0, "labeled": 0,
                               "cited": 1}
    detail = report["apply"]["flagged"][0]["detail"]
    assert "no proof pages accompanied this run" in detail
    assert "could not be placed" not in detail
    md = got.report_md.read_text(encoding="utf-8")
    assert "No proof pages accompanied this run." in md
    assert "1 correction(s) cite a proof page" in md


def test_a_pageless_list_that_cites_no_pages_stays_quiet(tmp_path):
    # A typed list with no page column is the ordinary free path — no warning.
    got = apply_corrections(LAYOUT,
                            json.dumps([{"find": "Their were",
                                         "replace": "There were"}]),
                            tmp_path)
    report = json.loads(got.report_json.read_text(encoding="utf-8"))
    assert report["pages"]["cited"] == 0
    assert "No proof pages accompanied" not in \
        got.report_md.read_text(encoding="utf-8")


def test_queue_options_carry_the_folio_too(tmp_path):
    """The fix screen's clickable placements name the page a designer navigates
    to, so each option is stamped with the folio, not just its item."""
    # The whole one-page book as the one proof page: counts match (1 == 1), the
    # spread map holds, and every paragraph sits on placed page 1.
    from docproof.corrections.idml import read_stories
    book = "\n".join(p.text for s in read_stories(LAYOUT)
                     for p in s.paragraphs)
    got = apply_corrections(LAYOUT,
                            json.dumps([{"find": "was", "replace": "is",
                                         "page": 1}]),
                            tmp_path, page_texts=[book])
    report = json.loads(got.report_json.read_text(encoding="utf-8"))
    queue = report["queue"]
    assert queue, "the ambiguous edit should be flagged"
    options = queue[0]["options"]
    assert options
    assert all(o["page"] == 1 and o["page_label"] == "1" for o in options)
