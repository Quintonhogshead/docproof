# IDML ground truth

Verified against **Adobe InDesign 2026 (DOMVersion 21.4)** on 2026-08-04 by
scripting InDesign to build a throwaway document and export IDML — once clean,
once with Track Changes on and a real insertion, deletion, and replacement, and
once with a Note. The generator scripts and their output are reproducible; the
shapes below are copied from the exported XML, not from documentation.

Everything the IDML reassembler emits is modelled on these listings. If a
future InDesign version changes them, this file is what to re-derive.

## Package layout

```
mimetype                     <- MUST be the first entry, MUST be ZIP_STORED
designmap.xml
META-INF/container.xml
META-INF/metadata.xml
Resources/{Graphic,Fonts,Styles,Preferences}.xml
XML/{Tags,BackingStory}.xml
MasterSpreads/MasterSpread_*.xml
Spreads/Spread_*.xml
Stories/Story_*.xml         <- all reviewable text lives here
```

`mimetype` contains `application/vnd.adobe.indesign-idml-package` and is stored
uncompressed, EPUB-style. `IdmlPackage` preserves each entry's original
compression type and order on save for exactly this reason.

Story parts are discovered from `designmap.xml`'s `<idPkg:Story src="…"/>`
children, in document order — that is the deterministic walk order. The
`Document@StoryList` attribute also lists a backing story (`XML/BackingStory.xml`)
that has no `idPkg:Story` entry; it is deliberately not reviewed.

## Text model

Only the root element is namespaced (`idPkg:Story`); everything inside is
unqualified.

```xml
<ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/Chapter Head">
  <CharacterStyleRange AppliedCharacterStyle="CharacterStyle/$ID/[No character style]">
    <Content>Chapter One</Content>
    <Br />
  </CharacterStyleRange>
</ParagraphStyleRange>
<ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/$ID/NormalParagraphStyle">
  <CharacterStyleRange AppliedCharacterStyle="CharacterStyle/$ID/[No character style]">
    <Content>It was </Content>
  </CharacterStyleRange>
  <CharacterStyleRange AppliedCharacterStyle="CharacterStyle/Emph">
    <Content>late,</Content>
  </CharacterStyleRange>
  <CharacterStyleRange AppliedCharacterStyle="CharacterStyle/$ID/[No character style]">
    <Content> and the road went on forever.</Content>
    <Br />
    <Content>She opened the door, the room was empty.</Content>
    <Br />
    <Content>Their were several mistakes here to find.</Content>
  </CharacterStyleRange>
</ParagraphStyleRange>
```

**The two facts that drive the whole design:**

1. **A paragraph is not an element.** Paragraphs are delimited by `<Br/>`, and
   one `CharacterStyleRange` routinely contains several of them (see the last
   CSR above). A paragraph is therefore a *span* — an ordered list of `Content`
   nodes — not a subtree. A `ParagraphStyleRange` does contain whole
   paragraphs, so a PSR is a safe outer boundary to split on.
2. **The final paragraph of a story has no trailing `<Br/>`.** A `Br` is a
   terminator, not a separator: a PSR ending in `Br` does *not* imply a
   following empty paragraph, so the empty tail segment is dropped.

Formatting lives on the enclosing `CharacterStyleRange`; `Content` carries no
attributes. Splitting text is therefore just splitting a `Content` node —
markedly simpler than the docx run split, which has to clone `w:rPr`.

Whitespace is literal inside `Content` (`<Content> we were </Content>`); there
is no `xml:space` equivalent to get wrong. Inter-element whitespace is
pretty-printing and lives in element tails, outside canonical text.

## Tracked changes — the shapes docproof emits

Set on the story element: `<Story … TrackChanges="true">`.

**Insertion** — `Content` sits directly inside the `Change`, inheriting the
enclosing CSR's formatting:

```xml
<Change Date="2026-08-04T09:01:46" ChangeType="InsertedText"
        UserName="$ID/Unknown User Name" AppliedDocumentUser="dDocumentUser0">
  <Content>VERYNEW </Content>
</Change>
```

**Deletion** — asymmetric: the deleted text is wrapped in a *full*
`ParagraphStyleRange` → `CharacterStyleRange` subtree carrying its own
formatting:

```xml
<Change Date="2026-08-04T09:01:46" ChangeType="DeletedText"
        UserName="$ID/Unknown User Name" AppliedDocumentUser="dDocumentUser0">
  <ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/$ID/NormalParagraphStyle">
    <CharacterStyleRange AppliedCharacterStyle="CharacterStyle/$ID/[No character style]">
      <Content>o</Content>
    </CharacterStyleRange>
  </ParagraphStyleRange>
</Change>
```

Both are **direct children of `CharacterStyleRange`, siblings of `Content`**.
For a replacement InDesign writes the insertion first, then the deletion; the
reassembler matches that order.

`AppliedDocumentUser` points at a `<DocumentUser Self="…" UserName="…"/>` child
of `<Document>` in `designmap.xml`. One exists (`dDocumentUser0`) even in a
document that has never tracked a change. docproof adds its own so the author
name shown in InDesign is the configured `revision_author`.

Date format is local, second precision, no zone suffix: `2026-08-04T09:01:46`.
A `$ID/` prefix on a `UserName` marks a localized default string; a literal
name is written without it.

## Comments — the `<Note>` element

Also a direct child of `CharacterStyleRange`, sibling to `Content`, and
excluded from canonical text:

```xml
<Note Collapsed="false" CreationDate="2026-08-04T09:08:23"
      ModificationDate="2026-08-04T09:08:23" UserName="Unknown User Name"
      AppliedDocumentUser="dDocumentUser0">
  <ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/$ID/[No paragraph style]">
    <CharacterStyleRange AppliedCharacterStyle="CharacterStyle/$ID/[No character style]">
      <Content>This is the explanation docproof would attach.</Content>
    </CharacterStyleRange>
  </ParagraphStyleRange>
</Note>
```

## Nested containers

**Tables** live inside a `CharacterStyleRange`, and their `Cell` elements carry
`Name="column:row"` (note the order — `Name="1:0"` is column 1 of row 0). Cells
are emitted in reading order, but the walker sorts by `(row, column)` parsed
from `Name` rather than trusting document order.

```xml
<Table Self="uffi115" BodyRowCount="2" ColumnCount="2" …>
  <Row …/><Column …/>
  <Cell Self="uffi115i0" Name="0:0" …>
    <ParagraphStyleRange …><CharacterStyleRange …><Content>…</Content></…>
  </Cell>
</Table>
```

**Footnotes** are inline too, inside a superscript CSR, and hold their own
`ParagraphStyleRange` children:

```xml
<CharacterStyleRange AppliedCharacterStyle="…" Position="Superscript">
  <Footnote>
    <ParagraphStyleRange …><CharacterStyleRange …><Content>…</Content></…>
  </Footnote>
</CharacterStyleRange>
```

## Canonical text — what is included

Concatenation of `Content` text within the paragraph span, where the walker:

- **descends** into `CharacterStyleRange`, `HyperlinkTextSource`, `XMLElement`,
  and `Change[@ChangeType="InsertedText"]` (the accepted view)
- **skips** `Note`, and `Change` of any other type — notably `DeletedText`,
  so canonical text is Word's "accept all" equivalent
- **recurses separately** into `Table` and `Footnote`, which become their own
  paragraphs with their own ids
- **ignores without descending** every other inline element (`Tab`, `Rule`,
  processing instructions such as `<?ACE 4?>` page-number markers, and anchored
  objects). Tabs contributing no character mirrors the docx walker's treatment
  of `w:tab`; anchored frames are the IDML analogue of docx textboxes and are a
  documented v1 exclusion.

## What InDesign refuses — learned the hard way

**Footnotes cannot carry a tracked change.** Asked (by script, with tracking on)
to edit footnote text, InDesign applies the edit *untracked* — it writes no
`Change` element at all, while the same edit in body text and in table cells
produces one. A `Change` written inside a `<Footnote>` is therefore markup
InDesign cannot read back, and it **crashes on open** rather than reporting an
error. docproof skips footnote paragraphs at ingest (reason
`footnote:tracked-changes-unsupported`) and the reassembler refuses one a
second time, because the cost of being wrong is a file that takes InDesign down.

**Table cells are fine.** InDesign writes `Change` elements inside `Cell`
content in exactly the shape documented above. Note that the scripting DOM does
not report them under `story.changes` — they hang off the cell's own text — so
a cell change looks like "no changes" if you count that way. That is a quirk of
the DOM, not of the file.

**`designmap.xml` must keep its `<?aid?>` processing instruction.** It sits
*before* the root element, so serializing the root Element alone silently drops
it and yields a package InDesign refuses to open with the generic "may not
support the file format" message. `ZipPackage._serialize` writes the
ElementTree for this reason.

## Reading the output back

`story.contents` shows U+FEFF wherever an inline `Note` is anchored — one per
note. This is InDesign's own note marker, not corruption: its own note export
shows the identical character in the same position. Footnote markers appear as
U+0004. Neither is part of canonical text, and neither reaches the walker,
which reads `Content` elements rather than the rendered string.

## Verified behaviour of docproof's output

A package written by docproof (3 body changes, 1 table-cell change, 3 notes)
was opened in InDesign 2026 and confirmed to:

- open with no repair prompt, `TrackChanges="true"` on the edited stories
- expose 6 story-level `Change` objects (an insertion + a deletion per
  replacement) plus the table-cell change, all authored `docproof`
- carry the three notes with docproof's explanation text
- **accept all** → the corrected wording, in body and table alike
- **reject all** → the original wording, exactly

## Reproducing this

The generator scripts live beside the fixtures. Regenerating requires InDesign
and takes about a minute:

```bash
osascript -e 'tell application "Adobe InDesign 2026" to do script (POSIX file "<abs-path>/make_idml.jsx") language javascript'
```
