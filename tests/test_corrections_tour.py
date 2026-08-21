"""The InDesign check tour: a read-only ExtendScript that walks the designer to
each composition check and open flag in the live document.

It is generated but never run here — InDesign is not on the machine and
ExtendScript cannot run in CI, exactly as `prep.place` — so these cover the
stops the run builds and the script it writes around them, not InDesign opening
it. What cannot be covered is asserted about the string it hands over.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

from docproof.corrections.model import CheckItem
from docproof.corrections.tour import build_stops, build_tour_jsx, write_tour


def _stories(*paras_by_id):
    """Lightweight stand-ins for corrected stories: build_stops only reads a
    story's id and its paragraphs' text."""
    out = []
    for story_id, paras in paras_by_id:
        out.append(SimpleNamespace(
            story_id=story_id,
            paragraphs=[SimpleNamespace(text=t) for t in paras]))
    return out


def test_a_check_takes_its_search_text_from_the_corrected_paragraph():
    stories = _stories(("s1", ["First line.", "This chapter starts here."]))
    checks = (CheckItem(page=7, story_id="s1", paragraph=1,
                        what="this now starts a page — check the page before it "
                             "does not run short",
                        why="start on a new page", edit_ids=("c1",)),)
    stops = build_stops(stories, checks, [], {7: "5"})
    assert len(stops) == 1
    s = stops[0]
    assert s["kind"] == "check"
    assert s["find"] == "This chapter starts here."   # the corrected text, verbatim
    assert s["page"] == "5"                            # the InDesign folio, not the proof page
    assert s["n"] == 1


def test_a_flag_carries_its_anchor_note_and_suggestion():
    queue = [{"id": "q1", "page": 12, "status": "ambiguous",
              "anchor": "the harbor lights", "find": "harbor", "replace": "harbour",
              "instruction": "British spelling", "advice": "", "resolved": None}]
    stops = build_stops([], (), queue, {12: "10"})
    assert len(stops) == 1
    s = stops[0]
    assert s["kind"] == "flag"
    assert s["find"] == "the harbor lights"           # the reviewer's marked run
    assert s["page"] == "10"
    assert "British spelling" in s["note"]
    assert s["replace"] == "harbour"


def test_a_flag_proposing_no_rewrite_shows_no_suggestion():
    queue = [{"id": "q1", "page": 3, "status": "flagged", "anchor": "a line",
              "find": "a line", "replace": "a line",   # a query, not a change
              "instruction": "is this right?"}]
    stops = build_stops([], (), queue, {})
    assert stops[0]["replace"] == ""


def test_resolved_and_dismissed_flags_are_not_toured():
    queue = [
        {"id": "q1", "page": 1, "status": "flagged", "anchor": "one",
         "instruction": "", "resolved": {"kind": "manual"}},
        {"id": "q2", "page": 2, "status": "flagged", "anchor": "two",
         "instruction": "", "dismissed": True},
        {"id": "q3", "page": 3, "status": "flagged", "anchor": "three",
         "instruction": ""},
    ]
    stops = build_stops([], (), queue, {})
    assert [s["find"] for s in stops] == ["three"]


def test_a_stop_with_nothing_to_find_and_no_page_is_dropped():
    # A routed-to-design note whose edit never anchored: no story text, no page.
    checks = (CheckItem(page=0, story_id="", paragraph=-1,
                        what="look at the rag", why="bad rag"),)
    assert build_stops([], checks, [], {}) == []


def test_stops_are_ordered_by_page_checks_before_flags():
    stories = _stories(("s1", ["alpha", "beta"]))
    checks = (CheckItem(page=9, story_id="s1", paragraph=1, what="w9", why=""),
              CheckItem(page=2, story_id="s1", paragraph=0, what="w2", why=""))
    queue = [{"id": "q1", "page": 2, "status": "flagged", "anchor": "flag on 2",
              "instruction": ""}]
    stops = build_stops(stories, checks, queue, {})
    # page 2 check, then page 2 flag, then page 9 check — and renumbered 1..3.
    assert [(s["kind"], s["page"]) for s in stops] == [
        ("check", "2"), ("flag", "2"), ("check", "9")]
    assert [s["n"] for s in stops] == [1, 2, 3]


def test_the_script_embeds_the_stops_as_valid_ascii_javascript():
    stops = build_stops(
        _stories(("s1", ["A curly “quote” inside."])),
        (CheckItem(page=1, story_id="s1", paragraph=0, what="look", why="why"),),
        [], {})
    jsx = build_tour_jsx(stops, book_name="My Book")
    assert jsx.isascii()                              # no encoding assumption in the file
    # The data rides in as a JS array literal; the curly quote survives as an escape.
    m = re.search(r"var STOPS = (\[.*?\]);", jsx, re.S)
    assert m and json.loads(m.group(1)) == stops      # valid JSON, round-trips
    assert "\\u201c" in jsx                            # the “ became a \u escape


def test_the_script_is_read_only_and_a_session_navigator():
    stops = build_stops(_stories(("s1", ["x"])),
                        (CheckItem(page=1, story_id="s1", paragraph=0,
                                   what="w", why=""),), [], {})
    jsx = build_tour_jsx(stops)
    # It searches, selects and scrolls — and writes nothing.
    assert "findGrep" in jsx and "app.select(" in jsx
    assert 'targetengine "docproofCheckTour"' in jsx   # palette survives the return
    for writer in ("insertionPoints", ".remove(", "changeGrep()", "doc.save",
                   "contents ="):
        assert writer not in jsx


def test_write_tour_returns_none_when_there_is_nothing_to_walk(tmp_path,
                                                              monkeypatch):
    import docproof.corrections.tour as tour
    monkeypatch.setattr(tour, "read_stories", lambda p: [])
    assert tour.write_tour(tmp_path, tmp_path / "x.idml", (), [], {},
                           dest_name="x_checks.jsx") is None
    assert not (tmp_path / "x_checks.jsx").exists()


def test_write_tour_writes_the_script_when_there_are_stops(tmp_path, monkeypatch):
    import docproof.corrections.tour as tour
    stories = _stories(("s1", ["A line to check."]))
    monkeypatch.setattr(tour, "read_stories", lambda p: stories)
    checks = (CheckItem(page=1, story_id="s1", paragraph=0, what="look", why="w"),)
    path = tour.write_tour(tmp_path, tmp_path / "x.idml", checks, [], {},
                           dest_name="x_checks.jsx", book_name="x")
    assert path is not None and path.name == "x_checks.jsx"
    assert "A line to check." in path.read_text("utf-8")
