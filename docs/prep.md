# Manuscript prep — Word in, formatted out

DocProof does two jobs. **Review** finds errors and writes tracked changes.
**Prep** answers a different question — what *is* each paragraph — and uses the
answers to format the manuscript: as a book-styled reading copy, as a file
tagged for the house InDesign template, or both.

A prep run hands back any of:

- **`book_<name>.docx`** — *the default.* The manuscript cleaned up and dressed
  as a printed Atmosphere interior: the house trim with mirrored margins,
  running heads and folios, chapter openers with a drop cap, justified Spectral
  body — a reading sketch for the author and the developmental editors, not a
  file for the InDesign team. The title-page lettering follows the book's
  subject matter (see [The book output](#the-book-output) below).
- **`tagged_<name>.docx`** — one clean style sheet whose paragraph style *names*
  are the template's own, blank lines resolved, export artifacts gone. This is
  the file a designer places.
- **`tracked_<name>.docx`** — the same decisions as Word revisions. Accept them
  all and you have the clean file; reject them all and you have the manuscript
  exactly as it arrived.

…plus `prep_notes.md` and `prep.json`: the mapping with counts, how scene breaks
were handled, what was cleaned up, the word-for-word result, and the flags for
the designer.

Asking for more than one file costs almost nothing extra. The cost of a run is
reading the manuscript; the book output adds only one small extra call (below).

## The book output

The book file is built from the same verified decisions as the placed file,
then dressed by **`config/prep/book_design.yaml`** — the interior design as
data: page geometry, faces, per-style formats, running heads, drop caps, and a
subject-matter → display-face map. Like the style sheet, a replacement dropped
into the prep override directory wins wholesale, and the app's Settings screen
reads the subjects out of it.

Three facts about the manuscript feed the design:

- **subject** — picks the title-page display face (`IM FELL English` for
  fantasy, `Special Elite` for a thriller, and so on; the map is YAML).
- **title** and **author** — the running heads (title recto, author verso).

All three are detected by one small structured call over the opening pages
(`docproof/prep/meta.py`); the subject answer is schema-constrained to the
design's own keys. The operator can override any of the three per job — an
empty field means "use what was detected", and a failed detection degrades to
the default face and a file-name title, never a failed job.

The faces are embedded into the .docx (`word/fonts/*.odttf`, the spec's
obfuscation) so the file looks right on machines without the fonts; everything
bundled is OFL/Apache licensed with `fsType=0` (see
`config/prep/fonts/README.md`). The drop cap moves the chapter's first letter
into a `w:framePr` frame paragraph — exactly how Word records its own — and
the verifier glues it back onto the word it came from, so the word-for-word
gate still holds letter for letter.

## The one rule

**Prep does not change the author's words.** Not as an instruction to the model
— as a check on the finished file:

- The author's word stream is extracted from the manuscript and from the file
  that was actually written, and compared token for token.
- The tracked file is checked twice: accepting every change must give the clean
  text, and rejecting every change must give the manuscript back.
- The only permitted difference is a scene-break glyph prep inserted.
- **A file that fails is deleted, not shipped.** The job fails, and the notes
  still get written so you can see where it diverged.

This is what makes the rest safe. The model classifies; every mutation is
deterministic Python.

## What the model decides, and what it doesn't

The model reads paragraphs in order and answers with one label each, from an
enum built out of the style sheet — an off-template style name is impossible on
the wire, not something a validator catches afterwards. It never returns text.

Everything mechanical is [rules.py](../docproof/prep/rules.py):

- **body first vs body para.** The first body paragraph after any heading, part
  title, chapter subtitle, subhead or scene break takes the no-indent style.
- **Scene breaks.** A blank line the model read as a genuine scene shift becomes
  a paragraph containing the house glyph; the paragraph after it opens a block.
  A break glyph the author typed themselves is kept exactly as they typed it.
- **Blank lines.** Everything else blank is removed — InDesign takes vertical
  spacing from the styles.
- **The table of contents.** Read from the file, never from the words, and
  applied over whatever the model answered — see below.
- **Sanity.** A page of prose labelled "scene break", a blank line labelled
  "chapter", a blank under a heading: each is corrected in the safe direction
  and flagged. Nothing is corrected quietly.

Flags are raised, never fixed: bylines, front matter in an unusual place, a
centered "The end" with no home style, lists (the style set has none), and
anything the model itself was unsure about.

## The manuscript's own table of contents

A contents entry reading "Chapter One" is character-for-character the chapter
heading it points at. Nothing reading only the text can tell them apart — so
the model, asked what "Chapter One" is, answered "chapter # / title", which
carries `page_break_before`. A twenty-line contents page became twenty chapter
openers.

So prep asks the file instead. [`_is_toc`](../docproof/prep/ingest.py) reads
three signals, any one of which is enough:

- the entry's own paragraph style — `TOC1`…`TOC9`, or LibreOffice's
  `Contents1`. **Not** `TOCHeading`: the word "Contents" is a front-matter
  title, and the required digit is what keeps it one;
- a `TOC` or `PAGEREF _Toc` field instruction, in a `w:fldSimple` or a
  `w:instrText`;
- a hyperlink to a `_Toc…` bookmark.

Those paragraphs get the sheet's `toc` role — `toc entry` in the shipped set —
whatever the model said about them, and the contents list is flagged once for
the designer. A style sheet that names no `toc` role sends them to body
instead: it looks wrong and reads fine, where a forced page break under every
line does not.

The text itself is untouched, page numbers included, so the file still passes
the word-for-word check. **InDesign generates its own contents from the styles
you place**, so the flag asks whether to keep these lines at all — it does not
answer that for you.

A contents list the author typed by hand carries none of those three signals.
For those, `toc entry` is also one of the labels the model may answer with, and
the prompt tells it that entries in a contents list are pointers rather than
headings.

## Swapping in your own style guide

**Nothing about Atmosphere Press is in the code.** The style set is
[config/prep/house_styles.yaml](../config/prep/house_styles.yaml) and it drives
the tagger's allowed answers, the Word style sheet, and the notes. To prep for a
different template, replace that file. Three ways, in order of convenience:

1. **In the app** — Settings → *The house style guide* → **Use a different
   style guide…**. The file is checked before it is installed: a duplicate
   name or a role pointing at nothing is refused there and then, with the
   reason, and the sheet you were using stays in force. **Go back to the one
   DocProof ships with** undoes it. (Dropping a `house_styles.yaml` into
   `~/Library/Application Support/DocProof/prep/` yourself still works and
   does the same thing — the app writes to that path.)
2. **From the terminal** — `docproof prep book.docx --style-sheet mine.yaml`.
3. **For good** — point `prep.style_sheet` in `config/default.yaml` somewhere else.

## Adjusting how the styles look

Settings → *The styles it will use, and how they look* opens an editor over
whichever sheet is in force: point size, space above and below, first-line
indent, alignment, bold, italic, starts-a-page, stays-with-what-follows — per
style, chosen from the values a template is actually drawn around rather than
typed as free numbers. Trim size and the scene-break glyph are there too.

**Names and ids are not editable, by design.** A style's name is what InDesign
matches when the file is placed *and* what the model is allowed to answer, so
it is a contract with two other things, not a preference. `PUT
/api/prep/styles/format` only accepts `format` keys and the two sheet-level
settings; everything else in the file is copied through untouched.

Edits are written to the same override file an uploaded sheet goes to, so the
shipped copy is never modified and one **Go back to the one DocProof ships
with** undoes everything. Being machine-written, that file loses the comments a
hand-edited one would keep.

This is formatting for the Word file a designer reads before placing it —
InDesign overrides all of it on import. What it does *not* change is the thing
that matters on Place: the names.

Each style needs:

```yaml
- name: "chapter # / title"     # EXACTLY as the InDesign template spells it —
                                # this is what InDesign matches on Place
  id: ChapterTitle              # the Word styleId: letters and digits only
  describe: >-                  # goes into the prompt verbatim; this is also
    A chapter heading …         # how you tune what gets tagged as what
  assign: model                 # model = the model may pick it
                                # derived = only the rules apply it
  opens: true                   # a body paragraph after this one is "body first"
  format: {size: 18, bold: true, align: center}   # light; InDesign overrides it
```

The `roles:` block at the bottom binds the deterministic rules to names in
*your* sheet (`body`, `body_first`, `scene_break`, `table`), so renaming a style
means renaming it in one more place, not editing Python. `pseudo_roles:` holds
the two answers that are not styles — running text and a blank line.

The prompt is data too:
[config/prep/tagging.yaml](../config/prep/tagging.yaml), with `{sheet}`,
`{trim}`, `{glyph}` and `{choices}` filled in from the sheet. A
`tagging.yaml` in the same override folder replaces it.

A malformed sheet fails loudly and specifically — a duplicate name, a styleId
with a space in it, a role pointing at a style that isn't there.

## Running it

**In the app.** Drop the manuscript, choose *Prepare for layout*, pick which
file you want back, start. Prep is always immediate: the windows have to be read
in order, because what a paragraph is depends on what came before it, so there
is no overnight form of it.

**From the terminal:**

```bash
docproof prep book.docx --output both --out ~/Documents/DocProof/book
```

`--mock-tags` labels everything as running text and runs the whole thing — both
writers, the verifier, the notes — with no API call, which is the cheap way to
check a new style sheet loads and produces the document you expect.

## What it costs

One request per window of paragraphs (120 by default, or 6,000 tokens, whichever
comes first). Long paragraphs are truncated to their first 400 characters before
being sent: the model answers with ids and labels, never text, so the tail of a
300-word paragraph would be billed for nothing. On a novel that truncation is
most of the difference in what prep costs.

The Spending tab adds up every run — tokens, requests and estimated cost, by
model and by document.

## What it doesn't do

- **Headers, footers and notes are left alone.** A house template supplies its
  own running heads. They are counted in the notes so you can see they were
  seen.
- **Tracked changes are refused outright.** Prep restyles every paragraph and
  deletes blank lines; doing that around somebody's unresolved edit would bury
  it. Accept or reject them in Word first. (Review has a policy for this; prep
  deliberately does not.)
- **`.doc`, `.rtf`, `.odt`, `.txt`** are converted with LibreOffice if it is
  installed, and refused with a clear message if it isn't. A `.txt` source has
  no italics to recover, and the notes say so.
- **It does not write `.idml` from nothing.** The deliverable is the tagged
  `.docx`. Given a template, DocProof can place it for you — see
  [Placing it for you](indesign.md#placing-it-for-you) — which produces a real
  `.indd`, but by flowing the manuscript into *your* template rather than
  inventing a layout.
- **The tracked file keeps the author's direct formatting.** Stripping a Google
  Docs export's fonts and sizes there would be hundreds more revisions to click
  through. The placed file is the one that gets cleaned.

## The shape of it

```
docproof/prep/
├── convert.py      .doc/.rtf/.odt/.txt → .docx, via LibreOffice
├── ingest.py       every w:p, blanks included; refuses tracked changes
├── chunker.py      ordered windows, with the tail of the previous one
├── tagger.py       the one model call; enum schema from the style sheet
├── rules.py        body first, scene breaks, sanity checks, flags
├── styles.py       the style sheet, and the Word style sheet built from it
├── verify.py       the word-for-word gate
├── notes.py        prep_notes.md + prep.json
├── pipeline.py     prepare → run → finish
└── writers/
    ├── clean.py    the InDesign-ready file
    └── tracked.py  pPrChange, deleted paragraph marks, inserted runs
```

Everything below the model is shared with review: the zip package wrapper, the
paragraph walker (so a `para_id` means the same thing in both pipelines), the
providers, the job queue, the Keychain, the app shell.

## Testing

[tests/test_prep.py](../tests/test_prep.py) and
[tests/test_prep_app.py](../tests/test_prep_app.py) run against
`googledoc.docx` — a fixture written as raw XML because a real Google Docs
export is styleless, full of `goog_rdk` content controls and typed whitespace,
and python-docx would tidy all of that away.

The load-bearing tests are the ones that corrupt a word on purpose and assert no
file was written, and the pair that check the tracked file's accept and reject
views. What they cannot cover is InDesign actually placing the result — verify
that by hand when the emitted markup changes, the same way
[idml-notes.md](idml-notes.md) is verified.
