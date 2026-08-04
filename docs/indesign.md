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
relationships the way Word comments are.

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

## Testing

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
