"""A read-only InDesign walk-through of everything a corrections run could not
settle in the file — the composition checks and the flags a person still owns.

The engine applies what it can and proves the text is what the list asked for,
but it cannot see the composed page: whether a forced break left the page before
it short, whether a heading is stranded, whether a reviewer's "bad rag here"
reads well now. Those go to the report's `checks` list, located but unconfirmed,
because composing is InDesign's job. This module turns that list — plus the
flags nobody resolved — into an ExtendScript the designer runs *inside* InDesign:
it steps through each item, finds the text on the page and selects it, and shows
what to look at. It is the one capability an IDML round-trip cannot have, because
only the live document knows where the text fell.

It is a navigator, not a writer. Nothing here changes a character: it searches,
selects and scrolls, and every safety property of the deterministic apply path is
untouched because this never touches the apply path. The corrected IDML is still
the verified deliverable; this is a guided reading of the part that needs eyes.

The script is generated but never run here — InDesign does not exist on the
server, and ExtendScript cannot run in CI, exactly as `prep.place` documents. So
`build_tour_jsx` is a pure string function tested against its output, and the
one thing the tests cannot cover is InDesign actually opening the tour.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .idml import read_stories

log = logging.getLogger("docproof.corrections.tour")

# The most text a single stop carries into the script. A search snippet only has
# to be long enough to land uniquely; a note only long enough to say what to look
# at. Capped so a book's worth of stops stays a small file.
_FIND_CHARS = 120
_NOTE_CHARS = 300
_REPLACE_CHARS = 200

# The plain phrase for each flag status a queue item can carry — the apply
# statuses a human owns, plus the two comment dispositions the queue leads with
# when a proof drove the run. Kept in step with `report.FLAG_TITLES`, with the
# comment-level pair added.
_FLAG_TITLES = {
    "not_found": "The text to change was not found",
    "ambiguous": "The text appears more than once",
    "crosses_paragraph": "The change would span a paragraph break",
    "routed_to_design": "A layout request, not a text edit",
    "overlaps": "Two corrections land on the same words",
    "withheld": "Held back for a human",
    "unstyleable": "The formatting could not be applied here",
    "unplaceable": "The paragraph could not be given a style of its own",
    "off_page": "The text is not on the page it was marked on",
    "flagged": "Flagged for a person",
    "not_extracted": "A reviewer note that became no edit",
}


def _flag_title(status: str) -> str:
    return _FLAG_TITLES.get(status, "Flagged for review")


def _snippet(text: str, limit: int = _FIND_CHARS) -> str:
    """A leading run of `text`, trimmed and length-capped. The whole thing when
    short, a prefix when long — a prefix is enough to locate, and locating is all
    the search string is for."""
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit]


def build_stops(stories, checks, queue, page_labels) -> list[dict]:
    """The tour, one stop per thing to look at, in reading order.

    `stories` is the *corrected* file's stories — the search text has to be the
    text the designer's document actually carries, so a stop can never send them
    hunting for a string an edit already changed. `checks` are the composition
    items (`CheckItem`); `queue` is the review screen's flags. A stop with neither
    text to find nor a page to turn to is dropped: there is nowhere to send the
    designer, so listing it would only be noise.

    Each stop is `{n, kind, page, find, title, note, replace}`. `find` is the
    string the script searches for and selects; `page` is the folio to fall back
    to when the search finds nothing (an edit removed the line, a heading is too
    short to be unique). Stops are ordered by page so the designer walks the book
    front to back, composition checks before flags on the same page."""
    by_id = {s.story_id: s for s in stories}
    labels = page_labels or {}

    def _page_label(page: int) -> str:
        return labels.get(page) or (str(page) if page else "")

    stops: list[dict] = []
    for c in checks or ():
        page = c.page or 0
        find = ""
        story = by_id.get(c.story_id)
        if story is not None and 0 <= c.paragraph < len(story.paragraphs):
            find = _snippet(story.paragraphs[c.paragraph].text)
        if not find and not page:
            continue
        stops.append({
            "kind": "check", "_sort": page or 10 ** 9,
            "page": _page_label(page), "find": find,
            "title": c.what, "note": _snippet(c.why, _NOTE_CHARS),
            "replace": "",
        })

    for q in queue or []:
        if q.get("resolved") or q.get("dismissed"):
            continue
        page = q.get("page") or 0
        find = _snippet(q.get("anchor") or q.get("find") or "")
        if not find and not page:
            continue
        note = (q.get("instruction") or "").strip()
        advice = (q.get("advice") or "").strip()
        if advice:
            note = f"{note} — {advice}".strip(" —")
        # What the flag wanted the text to become, shown so the designer sees the
        # proposed change without it ever being applied. Dropped when it equals
        # the find (a query that proposes no rewrite) — there is nothing to show.
        replace = q.get("replace") or ""
        if replace == (q.get("find") or ""):
            replace = ""
        stops.append({
            "kind": "flag", "_sort": page or 10 ** 9,
            "page": _page_label(page), "find": find,
            "title": _flag_title(q.get("status") or ""),
            "note": _snippet(note, _NOTE_CHARS),
            "replace": _snippet(replace, _REPLACE_CHARS),
        })

    stops.sort(key=lambda s: (s["_sort"], 0 if s["kind"] == "check" else 1))
    for i, s in enumerate(stops, 1):
        s["n"] = i
        s.pop("_sort", None)
    return stops


def build_tour_jsx(stops: list[dict], *, book_name: str = "") -> str:
    """The ExtendScript a designer runs in InDesign to walk the stops.

    The stops ride in as a JavaScript array literal — `json.dumps` output is
    valid JS, and kept ASCII (`ensure_ascii`) so the file carries no encoding
    assumption and a curly quote in a search string survives as a `\\uXXXX`
    escape the engine unescapes before it searches. The script itself is a
    modeless palette (a session `#targetengine`, so its buttons keep working
    after the script returns) that selects each stop's text and scrolls to it."""
    data = json.dumps(stops, ensure_ascii=True)
    name = json.dumps(book_name or "this document", ensure_ascii=True)
    return (_JSX
            .replace("__STOPS__", data)
            .replace("__BOOK__", name)
            .replace("__COUNT__", str(len(stops))))


def write_tour(out_dir, corrected_path, checks, queue, page_labels, *,
               dest_name: str, book_name: str = "") -> Path | None:
    """Write the check-tour script beside the corrected file, or nothing.

    Returns the path written, or None when there is nothing to tour — a clean run
    with no composition checks and no open flags needs no walk-through, and an
    empty tour that finds its first stop missing would read as a broken one. Reads
    the corrected stories itself so the search text is the finished file's."""
    stories = read_stories(corrected_path)
    stops = build_stops(stories, checks, queue, page_labels)
    if not stops:
        return None
    path = Path(out_dir) / dest_name
    path.write_text(build_tour_jsx(stops, book_name=book_name), encoding="utf-8")
    log.info("Wrote %s — %d stop(s) to check in InDesign", path.name, len(stops))
    return path


# The script. A read-only navigator: it searches, selects and scrolls, and writes
# nothing. `#targetengine` keeps the palette and its handlers alive after the
# top-level code returns, which is what makes Prev/Next work at all.
_JSX = r"""#targetengine "docproofCheckTour"
// DocProof -- Check tour
// ---------------------
// A guided walk through everything a corrections run could not settle in the
// file: the places where the page has to be *composed* (a forced break, a
// stranded heading, a bad rag) and the flags nobody resolved. For each one this
// finds the text on the page and selects it, so you can look at it in place.
//
// It changes nothing. It only searches, selects and scrolls -- reading, not
// writing. The corrected .idml is the finished file; this is a reading of the
// part that needs your eyes.
//
// One-time install: in InDesign, Window > Utilities > Scripts, right-click the
// "User" folder, Reveal in Finder, and drop this file in. Then, with the
// corrected file open, double-click it in the Scripts panel. (You can also just
// open it from File > Scripts, or drag it onto InDesign.)

var STOPS = __STOPS__;
var BOOK = __BOOK__;

function grepEscape(s) {
  return String(s).replace(/[.^$|?*+()\[\]{}\\]/g, function (m) { return "\\" + m; });
}

function clearGrep() {
  try { app.findGrepPreferences = NothingEnum.NOTHING; } catch (e) {}
  try { app.changeGrepPreferences = NothingEnum.NOTHING; } catch (e) {}
}

function findMatches(doc, text) {
  if (!text) return [];
  clearGrep();
  var out = [];
  try {
    app.findGrepPreferences.findWhat = grepEscape(text);
    out = doc.findGrep();
  } catch (e) { out = []; }
  clearGrep();
  return out || [];
}

function matchPage(m) {
  try {
    var frames = m.parentTextFrames;
    if (frames && frames.length) return frames[0].parentPage;
  } catch (e) {}
  return null;
}

function pageByName(doc, name) {
  if (!name) return null;
  for (var i = 0; i < doc.pages.length; i++) {
    try { if (String(doc.pages[i].name) === String(name)) return doc.pages[i]; }
    catch (e) {}
  }
  return null;
}

function showPage(page) {
  // Turning the active page is what scrolls the layout window to it; guarded
  // because the front window might be a story editor, not a layout.
  try { app.activeWindow.activePage = page; return true; } catch (e) {}
  try { app.layoutWindows[0].activePage = page; return true; } catch (e2) {}
  return false;
}

// Locate one stop: search for its text, prefer a match on its own page, select
// it and scroll there. Fall back to a shorter prefix, then to just turning to
// the page. Returns "sel" | "page" | "none" for the status line.
function locate(doc, stop) {
  var matches = findMatches(doc, stop.find);
  if ((!matches.length) && stop.find && stop.find.length > 30) {
    matches = findMatches(doc, stop.find.substring(0, 30));
  }
  var chosen = null;
  if (matches.length) {
    if (stop.page) {
      for (var i = 0; i < matches.length; i++) {
        var p = matchPage(matches[i]);
        if (p && String(p.name) === String(stop.page)) { chosen = matches[i]; break; }
      }
    }
    if (!chosen) chosen = matches[0];
  }
  if (chosen) {
    var pg = matchPage(chosen);
    if (pg) showPage(pg);
    try { app.select(chosen); return "sel"; } catch (e) {}
  }
  var page = pageByName(doc, stop.page);
  if (page && showPage(page)) return "page";
  return "none";
}

function buildPalette(doc) {
  var idx = 0;

  var w = new Window("palette", "DocProof -- check tour - " + BOOK);
  w.orientation = "column";
  w.alignChildren = ["fill", "top"];
  w.margins = 14;
  w.spacing = 8;
  w.preferredSize.width = 400;

  var head = w.add("statictext", undefined, "");
  head.graphics.font = ScriptUI.newFont(head.graphics.font.name, "BOLD",
                                        head.graphics.font.size);
  var kindT = w.add("statictext", undefined, "");
  var titleT = w.add("statictext", undefined, "", { multiline: true });
  titleT.preferredSize = [372, 44];
  var noteT = w.add("statictext", undefined, "", { multiline: true });
  noteT.preferredSize = [372, 72];
  var suggT = w.add("statictext", undefined, "", { multiline: true });
  suggT.preferredSize = [372, 40];
  var status = w.add("statictext", undefined, "");

  var row = w.add("group");
  row.alignment = "center";
  row.spacing = 6;
  var prevB = row.add("button", undefined, "< Prev");
  var goB = row.add("button", undefined, "Show me");
  var nextB = row.add("button", undefined, "Next >");
  var doneB = row.add("button", undefined, "Done");

  function render() {
    var s = STOPS[idx];
    head.text = "Stop " + s.n + " of " + STOPS.length
              + (s.page ? "   -   page " + s.page : "");
    kindT.text = (s.kind === "check")
      ? "Composition -- only the set page can settle this"
      : "Flagged -- a correction a person still owns";
    titleT.text = s.title || "";
    noteT.text = s.note ? ('"' + s.note + '"') : "";
    suggT.text = s.replace ? ("Suggested: "" + s.replace + '"') : "";
    prevB.enabled = idx > 0;
    nextB.enabled = idx < STOPS.length - 1;
    var r = locate(doc, s);
    status.text = (r === "sel")
      ? "Found and selected -- look at it on the page."
      : (r === "page")
        ? ("Couldn't select the exact text; turned to page " + s.page + ".")
        : ("Couldn't locate this one -- go to page "
           + (s.page || "?") + " by hand.");
    w.layout.layout(true);
  }

  prevB.onClick = function () { if (idx > 0) { idx--; render(); } };
  nextB.onClick = function () { if (idx < STOPS.length - 1) { idx++; render(); } };
  goB.onClick = function () { render(); };
  doneB.onClick = function () { w.close(); };

  render();
  w.show();
}

(function () {
  if (!app.documents.length) {
    alert("Open the corrected InDesign file first, then run the check tour.");
    return;
  }
  if (!STOPS || !STOPS.length) {
    alert("Nothing to check -- this run left no composition items or open "
          + "flags.");
    return;
  }
  buildPalette(app.activeDocument);
})();
"""
