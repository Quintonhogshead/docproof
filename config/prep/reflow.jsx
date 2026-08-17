// DocProof — Reflow
// ------------------
// A DocProof "InDesign-ready" file (IDML) arrives with the whole book in one
// primary text frame: correct, complete, but not yet flowed across pages —
// InDesign only reflows text as you edit it, not when it opens an overset file.
// Run this once, with the file open, and it threads the book through as many
// pages as it needs.
//
// One-time install: in InDesign, Window > Utilities > Scripts, right-click the
// "User" folder, Reveal in Finder, and drop this file in. Then, with a DocProof
// file open, double-click "reflow.jsx" in the Scripts panel.
//
// This adds pages; it changes no text. Undo (Cmd-Z) reverses it if you opened
// the wrong document.

(function () {
  if (app.documents.length === 0) {
    alert("Open the DocProof InDesign file first, then run Reflow.");
    return;
  }
  var doc = app.activeDocument;

  // The body frame is the one whose story holds the most text.
  var frame = null, best = -1;
  for (var i = 0; i < doc.textFrames.length; i++) {
    var tf = doc.textFrames[i], len = 0;
    try { len = tf.parentStory.contents.length; } catch (e) {}
    if (len > best) { best = len; frame = tf; }
  }
  if (!frame) { alert("No text frame found in this document."); return; }
  if (!frame.overflows) {
    alert("Nothing to reflow — the text already fits (" + doc.pages.length
          + " pages).");
    return;
  }

  var guard = 0;
  while (frame.overflows && guard < 200000) {
    var page = doc.pages.add(LocationOptions.AFTER, frame.parentPage);
    var m = page.marginPreferences, b = page.bounds;   // [y1, x1, y2, x2]
    var left = m.left, right = m.right;
    if (page.side === PageSideOptions.LEFT_HAND) { left = m.right; right = m.left; }
    var next = page.textFrames.add();
    next.geometricBounds = [b[0] + m.top, b[1] + left, b[2] - m.bottom, b[3] - right];
    frame.nextTextFrame = next;
    frame = next;
    guard++;
  }
  alert("Reflow complete: " + doc.pages.length + " pages.");
})();
