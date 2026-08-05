# The two rules a paragraph cannot see

Both of these are house rules that per-paragraph review is structurally unable
to apply, for different reasons. One needs to change how text is *set* rather
than what it says; the other needs the whole book at once.

## Title italics

`config/error_types/title_italics.yaml`, `channel: format`.

A long-work title set in roman is a house-style error that alters no
characters, so it cannot be expressed as an insertion and a deletion — the
validator would see the text unchanged and throw it away as a no-op. Word
records it instead as a **tracked formatting revision**:

```xml
<w:r>
  <w:rPr>
    <w:i/><w:iCs/>
    <w:rPrChange w:id="1" w:author="Atmosphere Press Proofreader" w:date="…">
      <w:rPr/>            <!-- the OLD properties: what "reject" restores -->
    </w:rPrChange>
  </w:rPr>
  <w:t>Moby-Dick</w:t>
</w:r>
```

The channel decides what `corrected_text` means, which is the same mechanism
`query` already uses:

| channel | `corrected_text` holds |
|---|---|
| `change` | the fixed sentence |
| `query` | the sentence repeated unchanged — it asks, it does not edit |
| `format` | the exact span to mark |

Two details that took care. `w:rPr`'s children are a schema-enforced sequence,
so `w:i` cannot simply be appended — it goes before the first child that is not
`rStyle`/`rFonts`/`b`/`bCs`, with `w:iCs` after it and `w:rPrChange` last. And
**the model cannot see existing italics**: it reads plain text, so it reports
every long-work title it finds. The reassembler checks the run's actual
properties and silently leaves alone the ones already correct, counted as
`already_set` rather than as a failure. Marking a correctly-italicised title
would be a revision the author has to click through for nothing.

Formatting claims spans in a register of its own, so italicising a title and
fixing a typo inside it are not in conflict.

### What the audit does not cover

The reject-all audit compares **text**. A formatting revision changes no
characters, so it passes trivially — correctly, since rejecting it restores the
old run properties and the text was never in question. But formatting applied
*without* a revision around it would also pass unnoticed. Nothing does that
today; if anything ever should, the audit's baseline needs to carry run
properties too.

## Term consistency

`docproof/consistency.py`. Runs on whole-document reviews only, at no API cost.

The brief asks for compound-word consistency — *blood-cursed* against
*bloodcursed* against *blood cursed*, *safe keeping* against *safekeeping* —
and a model reading chapter 4 has no idea what chapter 40 said. Finding this
needs the whole document at once, which is what a deterministic scan is for.

Detection is mechanical: strip the hyphens and spaces, and two spellings of one
term collapse to the same key. Open compounds are covered by scanning adjacent
word pairs as well as single tokens, which is the case a token-only scan would
miss — and it is the first example the brief gives.

```yaml
consistency:
  enabled: true
  min_length: 7             # shorter keys collide by accident
  min_dominance: 2          # how far the majority must lead; 1 flags even splits
```

**It asks and never corrects.** The same test that finds an inconsistency
cannot tell one from a distinction: *awhile* and *a while* mean different
things, as do *everyday* and *every day*. Those are excluded by name, and
everything else goes to the author as a margin question naming both forms and
which is used more. Which form a book uses is the author's to settle.

Two things deliberately do *not* count as spelling. Capitalization is one: a
noun that opens a sentence is not a second form of itself, and counting it as
one would file a query against *Her mother* every time *her mother* appears
mid-sentence. The shape of the apostrophe is the other — the typographic and
straight marks are normalized to one, but the apostrophe is kept, because
deleting it would match *brothers* against *brother's*, a plural and a
possessive rather than one term written two ways.

Three filters keep it quiet. Keys shorter than `min_length` are dropped,
because short forms collide by accident. A term written one way is not an
inconsistency. And a form must be clearly outnumbered before it reads as a slip
rather than a second deliberate choice — at one occurrence each there is no
dominant form to recommend and no evidence which is the error, so nothing is
said unless `min_dominance` is lowered to 1.

A partial review says nothing at all here: *"you write this three ways"* is not
a claim a review of two chapters is in a position to make.
