# InDesign (IDML) support

DocProof reviews `.idml` alongside `.docx`. The output is `reviewed_<name>.idml`
carrying native InDesign tracked changes, plus the same `summary.md`,
`findings.json` and `run.log` a Word review produces.

Every guarantee the Word pipeline makes carries over unchanged: the model only
reports findings anchored to exact quoted text, a deterministic validator
confirms each quote, a deterministic reassembler performs the XML surgery, and
parts of the package DocProof did not edit are copied byte-for-byte.

The XML shapes this rests on were verified against real InDesign 2026 output
rather than documentation — **[idml-notes.md](idml-notes.md) is the reference**,
and the first thing to re-derive if a future InDesign changes something.

## The seam

Only the two ends of the pipeline are format-specific. Everything between them
— models, chunker, analyzer, validator, providers, reporting — was already
format-neutral, because a `ParagraphRef` is an id, a part, a location, some text
and a style name, and none of those mean anything OOXML-specific.

```
docproof/formats/
├── base.py        DocumentFormat: the two ends, plus the words the UI uses
├── __init__.py    get_format(path) — dispatch by extension
├── docx.py        re-exports the existing OOXML ingest/reassembler
└── idml/
    ├── walker.py       IdmlPackage + the shared story walker
    ├── ingest.py       preflight, policies, document model
    └── reassembler.py  Change surgery, Notes, accept/reject views
```

`docproof/utils/package.py` holds `ZipPackage`, the zip-in-memory wrapper both
formats subclass — lazy parsing, a dirty set, and byte-for-byte round-tripping
of untouched parts.

The `.docx` modules were deliberately **not** moved. They are the most heavily
tested code in the project, and relocating them to make the tree symmetrical
would churn imports and history for no behavioural gain. `formats/docx.py` is a
four-line re-export.

## What is different about IDML

**A paragraph is not an element.** IDML delimits paragraphs with `<Br/>`, and a
single `CharacterStyleRange` routinely holds several of them. So a paragraph is
a *span* — an ordered tuple of `Content` nodes — and `WalkedParagraph` carries
that tuple instead of a single element. In the test fixture, one paragraph is
stitched back together from five separate style ranges.

**Splitting text is easier than in OOXML.** Formatting lives on the enclosing
`CharacterStyleRange`, never on `Content`, so splitting at an anchor boundary is
just splitting a `Content` node — no `w:rPr` to clone, no `xml:space` to get
wrong.

**Deletions and insertions are not symmetrical.** An insertion holds `Content`
directly; a deletion wraps the text in a full `ParagraphStyleRange` →
`CharacterStyleRange` subtree carrying its own formatting. Both are direct
children of the `CharacterStyleRange`, siblings of `Content`.

**Comments are inline `<Note>` elements**, not a separate package part with
relationships the way Word comments are. Both channels use them: the
explanation hung off a correction, and the query that corrects nothing.

**A Note is a point, not a range.** This is the one place the two formats give
the author a different thing. A Word query highlights the whole sentence it is
asking about (`w:commentRangeStart`/`End` around it); InDesign has no such
range, so the note is anchored at the **first character of that sentence** and
the question carries the rest. Everything else about the channel is identical,
including the words it asks in — those are shared in
[`docproof/queries.py`](../docproof/queries.py) rather than written once per
format, because an IDML review that quietly dropped its queries while every
surface still counted them is what one copy per format bought us.

Placing a note inserts no text, exactly as Word's range markers insert none:
splitting a `Content` node preserves canonical text, and the walker skips
`Note`. So queries go on **before** the tracked changes, and every offset the
edits rely on still points where it did.

## Known limitations (v1)

- **Footnotes are not reviewed.** InDesign cannot carry a tracked change in
  footnote text — asked to make one it silently applies the edit untracked, and
  a `Change` written there produces a file it *crashes* on. Footnote paragraphs
  are listed in `skipped` with the reason
  `footnote:tracked-changes-unsupported`, so the report shows DocProof saw them.
- **Anchored frames and text on paths** are not walked, the same exclusion the
  Word pipeline makes for textboxes. Their text belongs to a separate story on
  the page; folding it into the host paragraph would corrupt every offset after
  it.
- **Tabs contribute no character** to canonical text, mirroring the Word
  walker's treatment of `w:tab`. Consistent at both ends, so anchors stay valid.
- **Overset text is reviewed** — it is in the story XML even though it is not
  visible on the page.
- **`.indd` is never parsed.** A native InDesign document gets a preflight error
  naming File → Export → InDesign Markup (IDML).

## Tracked changes already in the document

`tracked_changes_policy` behaves as it does for Word, with InDesign wording:
`abort` (the default) refuses and points at InDesign's own accept/reject;
`accept_all_first` resolves existing changes — keeping insertions, dropping
deletions — to a fixed point; `ignore` proceeds with a logged warning that new
edits near existing revisions may nest.

## Placing it for you

Prep hands back a `.docx` whose paragraph style *names* are the template's own.
Flowing it in is mechanical, so DocProof will do it: set **your InDesign
template** in Settings, and a finished prep job grows a **Place into the
InDesign template** button. It writes `placed_<name>.indd` beside the job's
other files and reveals it in the Finder.

[`docproof/prep/place.py`](../docproof/prep/place.py) writes an ExtendScript
file and hands it to InDesign by Apple events — the same route
`tests/fixtures/make_idml.jsx` uses. The script:

1. opens the template and **immediately saves it under the output name**, so
   every edit after that lands on the copy. The file the designer maintains is
   never written to;
2. sets Word import preferences — unused styles not imported, style clashes
   resolved to the template's existing definitions, local overrides preserved
   so the author's italics survive;
3. places the manuscript at the start of page 1's text frame, creating one at
   the margins if the template has none;
4. **merges the styles the importer invents** (see below);
5. threads new pages until nothing is overset, then saves and closes.

### The capitalisation trap

Verified against InDesign 2026: **the Word importer capitalises the first
letter of every incoming style name.** A file tagged `chapter # / title`
arrives as `Chapter # / title`, which does not match the template's own style,
so "use existing" never fires and InDesign creates a *new* style carrying
Word's formatting. The manuscript then looks placed and is not — every
paragraph sits in a near-duplicate style, which is the one outcome this whole
pipeline exists to prevent.

So the script records the template's styles before placing, and afterwards
merges anything the import created that matches an existing style
case-insensitively — `style.remove(target)`, which reassigns every paragraph
using it and then deletes it. The reply line reports how many were merged.

A style with no counterpart in the template (`Normal`, and anything your sheet
names that the template doesn't) is left alone: that is a real mismatch and
worth seeing.

### What it needs, and what it will say

macOS asks for permission the first time — **System Settings → Privacy &
Security → Automation** — and the button reports that in those words if it is
refused. Missing-font and style-clash dialogs are suppressed
(`NEVER_INTERACT`) rather than left to hang the run at 11pm. A template that
has moved, an InDesign that will not start, and a run that outlasts its ten
minutes each get their own sentence.

The manuscript's own table of contents is **not** regenerated — InDesign builds
its own from the placed styles, and prep flags the contents list for exactly
that decision. See [prep.md](prep.md#the-manuscripts-own-table-of-contents).

## Testing

`tests/test_place.py` covers the script docproof writes and everything it makes
of InDesign's replies, with the `osascript` boundary passed in — no test starts
an application. What it cannot cover is InDesign actually doing it; that was
verified by hand against InDesign 2026, placing a tagged fixture into a
template and reading the applied styles back out, which is how the
capitalisation trap above was found.

`tests/test_idml.py` covers walker determinism, paragraphs spanning style
ranges, `<Br/>` boundary semantics, table reading order, style-name resolution,
every preflight refusal, the three policies, footnote refusal at both layers,
the accept/reject invariants, and the two packaging traps (`mimetype` first and
stored, `<?aid?>` preserved).

The fixtures are real InDesign exports, checked in because regenerating them
needs InDesign on the machine — `tests/fixtures/make_idml.jsx` is the script
that produced them.

What the automated tests *cannot* cover is InDesign actually opening the result.
That was verified by hand for the shapes documented here; the checklist and its
results are at the end of [idml-notes.md](idml-notes.md). Re-run it when the
emitted markup changes or a new InDesign version ships.
