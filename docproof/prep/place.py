"""Putting the tagged manuscript into the house InDesign template.

Prep's deliverable is a .docx whose paragraph style NAMES are spelled exactly
as the template spells them. Placing it is then a mechanical step — open the
template, flow the file in, let InDesign match the names — and mechanical steps
are worth doing for somebody rather than describing to them.

This is the only part of docproof that drives another application. It does it
the way InDesign asks to be driven: a script file handed over by Apple events,
running inside InDesign itself. Nothing here talks to a network, and the
template is opened and immediately saved somewhere else, so the file the
designer maintains is never the file being written to.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("docproof.prep.place")

# The bundle id rather than a name: "Adobe InDesign 2026" stops being true
# every year, and a version this code has never heard of should still work.
BUNDLE_ID = "com.adobe.InDesign"
# Launching InDesign cold, opening a template and flowing a novel into it is
# not a fast operation, and the alternative to waiting is a half-placed file.
TIMEOUT_SECONDS = 600


class PlaceError(Exception):
    """Placing did not happen, with a reason the person who asked can act on."""


def find_indesign(*, runner=subprocess.run) -> str | None:
    """Where InDesign is, or None. Used to tell the user it is missing — the
    script itself asks for the application by id and never by path."""
    try:
        found = runner(["mdfind",
                        f'kMDItemCFBundleIdentifier == "{BUNDLE_ID}"'],
                       capture_output=True, text=True, timeout=20)
        hits = [line for line in found.stdout.splitlines() if line.strip()]
        if hits:
            return sorted(hits)[-1]           # the newest year sorts last
    except (OSError, subprocess.SubprocessError):
        pass                                  # Spotlight is not a dependency
    apps = sorted(Path("/Applications").glob("Adobe InDesign */*.app"))
    return str(apps[-1]) if apps else None


def build_jsx(template: Path, tagged: Path, out: Path) -> str:
    """The script InDesign runs.

    It saves the template under the output name before touching anything, so
    every edit that follows lands on the copy. Style clashes resolve to the
    template's existing definitions: the incoming names were written to match
    them, which is the entire point of prep, and "use existing" is what turns a
    name match into the template's own design."""
    return _JSX % {
        "template": json.dumps(str(template)),
        "tagged": json.dumps(str(tagged)),
        "out": json.dumps(str(out)),
    }


_JSX = r"""
// Written by docproof. Opens the house template, saves it under a new name,
// flows the tagged manuscript in, and adds pages until nothing is overset.
var TEMPLATE = %(template)s;
var TAGGED   = %(tagged)s;
var OUT      = %(out)s;

function marginBounds(page) {
  var m = page.marginPreferences;
  var b = page.bounds;                       // [y1, x1, y2, x2]
  var left = m.left, right = m.right;
  // On a facing-pages spread the inside margin swaps sides.
  if (page.side === PageSideOptions.LEFT_HAND) { left = m.right; right = m.left; }
  return [b[0] + m.top, b[1] + left, b[2] - m.bottom, b[3] - right];
}

function importPrefs() {
  var p = app.wordRTFImportPreferences;
  // Wrapped one at a time: a property InDesign renamed between versions must
  // not cost us the whole run.
  try { p.importUnusedStyles = false; } catch (e) {}
  try { p.resolveParagraphStyleClash = ResolveStyleClash.RESOLVE_CLASH_USE_EXISTING; } catch (e) {}
  try { p.resolveCharacterStyleClash = ResolveStyleClash.RESOLVE_CLASH_USE_EXISTING; } catch (e) {}
  try { p.preserveLocalOverrides = true; } catch (e) {}   // the author's italics
  try { p.useTypographersQuotes = false; } catch (e) {}   // the manuscript's own
  try { p.removeFormatting = false; } catch (e) {}
  try { p.includeTOCText = true; } catch (e) {}
  try { p.includeFootnotes = true; } catch (e) {}
  try { p.includeEndnotes = true; } catch (e) {}
}

function styleState(doc) {
  // Which styles the template already had, by id and by lowercased name.
  var state = {ids: {}, byName: {}};
  var lists = [doc.allParagraphStyles, doc.allCharacterStyles];
  for (var l = 0; l < lists.length; l++) {
    for (var i = 0; i < lists[l].length; i++) {
      var s = lists[l][i];
      state.ids[s.id] = true;
      state.byName[s.name.toLowerCase()] = s;
    }
  }
  return state;
}

function mergeImportedStyles(doc, before) {
  // InDesign's Word importer capitalises the first letter of every incoming
  // style name, so "chapter # / title" arrives as "Chapter # / title" and
  // misses the template's own style by one character — leaving the manuscript
  // in a brand-new style carrying Word's formatting, which is the one outcome
  // prep exists to prevent. Anything the import invented that matches a style
  // already in the template, ignoring case, is merged back into it.
  var fresh = [];
  var lists = [doc.allParagraphStyles, doc.allCharacterStyles];
  for (var l = 0; l < lists.length; l++) {
    for (var i = 0; i < lists[l].length; i++) {
      var s = lists[l][i];
      if (!before.ids[s.id]) fresh.push(s);
    }
  }
  var merged = 0;
  for (var j = 0; j < fresh.length; j++) {
    var target = before.byName[fresh[j].name.toLowerCase()];
    if (!target || target.id === fresh[j].id) continue;
    try { fresh[j].remove(target); merged++; } catch (e) {}
  }
  return merged;
}

function targetFrame(doc) {
  var page = doc.pages[0];
  if (page.textFrames.length > 0) return page.textFrames[0];
  var frame = page.textFrames.add();
  frame.geometricBounds = marginBounds(page);
  return frame;
}

function autoflow(doc, frame) {
  // Threading by hand rather than relying on smart text reflow, which is a
  // document preference the template may well have off.
  var guard = 0;
  while (frame.overflows && guard < 5000) {
    var page = doc.pages.add(LocationOptions.AFTER, frame.parentPage);
    var next = page.textFrames.add();
    next.geometricBounds = marginBounds(page);
    frame.nextTextFrame = next;
    frame = next;
    guard++;
  }
  return frame.overflows ? "overset" : "ok";
}

function main() {
  var template = new File(TEMPLATE);
  if (!template.exists) return "ERR:The template is not where DocProof was told it is.";
  var tagged = new File(TAGGED);
  if (!tagged.exists) return "ERR:The tagged manuscript could not be found.";

  var doc = app.open(template);
  try {
    doc.save(new File(OUT));                 // everything after this is the copy
    importPrefs();
    var before = styleState(doc);
    var frame = targetFrame(doc);
    frame.parentStory.insertionPoints[0].place(tagged);
    var merged = mergeImportedStyles(doc, before);
    var flowed = autoflow(doc, frame);
    doc.save();
    var pages = doc.pages.length;
    doc.close(SaveOptions.NO);
    return "OK:" + pages + ":" + flowed + ":" + merged;
  } catch (e) {
    try { doc.close(SaveOptions.NO); } catch (e2) {}
    return "ERR:" + e.message;
  }
}

var __was = app.scriptPreferences.userInteractionLevel;
app.scriptPreferences.userInteractionLevel =
  UserInteractionLevels.NEVER_INTERACT;      // no missing-font dialogs at 11pm
var __result;
try {
  __result = main();
} catch (e) {
  __result = "ERR:" + e.message;
} finally {
  app.scriptPreferences.userInteractionLevel = __was;
}
__result;
"""


def place_into_template(template: str | Path, tagged: str | Path,
                        out: str | Path, *, runner=subprocess.run,
                        timeout: int = TIMEOUT_SECONDS) -> Path:
    """Place the tagged manuscript into a copy of the template.

    Returns the path to the saved .indd. Raises PlaceError with something worth
    reading on every failure — this drives an application the user can see, so
    "it didn't work" is never enough."""
    template, tagged, out = Path(template), Path(tagged), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".jsx", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(build_jsx(template, tagged, out))
        script = Path(fh.name)

    tell = (f'tell application id "{BUNDLE_ID}" to do script '
            f'(POSIX file {json.dumps(str(script))}) language javascript')
    log.info("Placing %s into %s", tagged.name, template.name)
    try:
        done = runner(["osascript", "-e", tell], capture_output=True,
                      text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise PlaceError(
            f"InDesign took longer than {timeout // 60} minutes and DocProof "
            f"stopped waiting. It may still be working — check InDesign "
            f"before trying again.") from e
    except OSError as e:
        raise PlaceError(f"DocProof could not start the script: {e}") from e
    finally:
        script.unlink(missing_ok=True)

    reply = (done.stdout or "").strip()
    if done.returncode != 0 or not reply:
        raise PlaceError(_explain(done))
    if reply.startswith("ERR:"):
        raise PlaceError(f"InDesign reported: {reply[4:].strip()}")
    if not reply.startswith("OK:"):
        raise PlaceError(f"InDesign said something unexpected: {reply}")

    if not out.is_file():
        raise PlaceError("InDesign said it worked, but the file it should have "
                         "written is not there.")
    _, pages, flowed, merged = (reply.split(":") + ["", "", ""])[:4]
    log.info("Placed into %s — %s pages, %s, %s imported style(s) merged into "
             "the template's own", out.name, pages, flowed, merged or "0")
    if flowed == "overset":
        log.warning("%s still has overset text after placing", out.name)
    return out


def _explain(done) -> str:
    """Turn the failures that actually happen into sentences."""
    err = (done.stderr or "").strip()
    if "-1743" in err or "Not authorized" in err:
        return ("macOS has not been told DocProof may control InDesign. Open "
                "System Settings → Privacy & Security → Automation, turn on "
                "InDesign under DocProof, and try again.")
    if "-600" in err or "isn't running" in err:
        return "InDesign would not start."
    if "-1728" in err:
        return ("InDesign could not run the script — it may be showing a "
                "dialog. Bring it to the front, clear it, and try again.")
    return err or "InDesign gave no reply."
