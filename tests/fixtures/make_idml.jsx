// Build a throwaway InDesign document, export IDML clean and with tracked
// changes. Touches nothing the user owns; everything lands in OUT.
var OUT = "/private/tmp/claude-501/-Users-quintonjohnson-Desktop-Atmosphere-docproof/c40ada88-14ae-478d-841a-164fa2825d11/scratchpad/";

function log(s) { $.writeln(s); return s; }

app.scriptPreferences.userInteractionLevel = UserInteractionLevels.NEVER_INTERACT;

var doc = app.documents.add();
var page = doc.pages[0];

// A heading style so we can see AppliedParagraphStyle in the XML.
var head = doc.paragraphStyles.add({name: "Chapter Head"});

var frame = page.textFrames.add();
frame.geometricBounds = [50, 50, 500, 400];

var story = frame.parentStory;
story.contents =
  "Chapter One\r" +
  "It was late, we were tired and the road went on forever.\r" +
  "She opened the door, the room was empty.\r" +
  "A third paragraph with plain text for good measure.\r" +
  "Their were several mistakes here to find.";

story.paragraphs[0].appliedParagraphStyle = head;

// Mixed formatting inside one paragraph -> multiple CharacterStyleRanges,
// which is what forces the reassembler to split runs. Point size and a
// character style are always available; font styles are font-dependent.
var emph = doc.characterStyles.add({name: "Emph", pointSize: 14});
story.paragraphs[1].words[2].appliedCharacterStyle = emph;
story.paragraphs[1].words[5].pointSize = 9;

// A table, so we learn the Cell structure.
var tblFrame = page.textFrames.add();
tblFrame.geometricBounds = [520, 50, 620, 400];
var tbl = tblFrame.parentStory.tables.add();
tbl.columnCount = 2;
tbl.bodyRowCount = 2;
tbl.cells[0].contents = "First cell has a sentence in it, it runs on.";
tbl.cells[1].contents = "Second cell";
tbl.cells[2].contents = "Third cell";
tbl.cells[3].contents = "Fourth cell";

// A footnote, so we learn that structure too.
var fnIp = story.paragraphs[2].insertionPoints[-2];
var fn = story.footnotes.add(LocationOptions.AT_END, fnIp);
fn.texts[0].contents = "A footnote with its own sentence, it also runs on.";

doc.exportFile(ExportFormat.INDESIGN_MARKUP, new File(OUT + "clean.idml"));
log("wrote clean.idml");

// --- now the same document with tracked changes -------------------------
story.trackChanges = true;
var tblStory = tblFrame.parentStory;
tblStory.trackChanges = true;

// An insertion: add a word mid-sentence.
story.paragraphs[3].insertionPoints[2].contents = "VERYNEW ";

// A deletion: remove a word.
var p4 = story.paragraphs[4];
p4.characters.itemByRange(0, 4).remove();

// A replacement (delete + insert adjacent), the shape docproof emits most.
var p2 = story.paragraphs[2];
p2.characters.itemByRange(16, 16).remove();      // the comma
p2.insertionPoints[16].contents = ";";

doc.exportFile(ExportFormat.INDESIGN_MARKUP, new File(OUT + "tracked.idml"));
log("wrote tracked.idml");

doc.close(SaveOptions.NO);
app.scriptPreferences.userInteractionLevel = UserInteractionLevels.INTERACT_WITH_ALL;
"done";
