# Silent normalization and the reject-all audit

These two belong in one document because they define each other. The
normalizer makes the only edits docproof does not track; the audit proves
nothing else escaped.

## The two silent edits

`docproof/normalize.py`. The Atmosphere brief allows exactly two changes
outside the tracked-changes system, on the grounds that neither alters what
the book says:

- straight quotes and apostrophes become curly
- runs of two or more spaces collapse to one

```yaml
normalize:
  quotes: true
  spaces: true
```

They run **before ingest**, which is the design decision everything else
follows from. The document model, the sweeps, every model pass and every
anchor all measure against normalized text, so no later stage has to know this
happened — and the audit gets a clean definition (below).

### Why this is the most dangerous module in the project

Every other edit docproof makes arrives in front of a person as a tracked
change they can reject. These do not. A wrongly-curled apostrophe ships to
print, looking deliberate.

So the rule is **convert what is certain, and leave the rest alone**. A
straight apostrophe left straight is a blemish a proofreader will catch. An
apostrophe curled the wrong way is a typographic error nobody will.

Double quotes are safe — the preceding character settles the direction, and a
double quote is never anything but a quotation mark. Single marks are where
the difficulty lives, and they are decided in this order:

| case | example | result |
|---|---|---|
| after a letter or digit | `it's`, `dogs'`, `runnin'` | `’` |
| before a digit | `'60s` | `’` |
| a known dropped letter | `'tis`, `'twas`, `'ere`, `'n'` | `’` |
| a nested quotation with a partner nearby | `he said 'hello' and left` | `‘` … `’` |
| anything else | an unmatched mark, unfamiliar dialect | **left straight, and counted** |

The elision list cannot be complete and is not meant to be. Dialect drops
letters in endless ways, and the difference between `'ere` (an elision) and
`'ere'` (a quotation) is not always in the text. What is not certain is left
alone, and `summary.md` reports how many marks that was so a person can set
them.

Space collapse leaves leading indentation alone, and skips any paragraph with
no letters or digits in it — `*   *   *` is spaced that way on purpose.

## The reject-all audit

`docproof/audit.py`. The brief states this as reproducing the original
character-for-character with two allowed exceptions. Written that way the
check would have to know about quote curling and space collapsing forever.

There is a cleaner formulation that means the same thing. Normalization runs
before ingest, so the text docproof *ingested* already has both exceptions
applied. The invariant is therefore:

> Rejecting every tracked change reproduces the document as ingested.

No exceptions at all. The two silent edits are accounted for separately, by
count, in the normalization report. Anything else that differs is a bug — text
that reached the manuscript without a revision mark around it, which is
exactly the failure nobody would otherwise notice, because the *accepted* view
looks perfectly correct.

```yaml
audit: strict     # strict | warn | off
```

The audit runs **before the file is written**, so `strict` means what it says:
a run that fails produces no output document at all. `warn` writes the file and
records the failure in `summary.md` and `findings.json` so you can open it and
see what happened. `off` skips the check, and removes the only thing standing
between an untracked edit and the author.

The baseline covers *every* paragraph, not just the reviewable ones — a
heading the normalizer touched needs auditing as much as a body paragraph the
model edited.

When it fails, the report points at the character where the two first diverge
rather than printing two long paragraphs:

```
body-0003 at character 4:
    ingested: …'The forest---which had no name---was quiet.'
    rejected: …'The woods---which had no name---was quiet.'
```

## Formats

Both capabilities are optional on `DocumentFormat`. `.docx` implements
`normalize` and `snapshot`; `.idml` implements neither yet, so InDesign layouts
are reviewed without quote normalization and without the audit, and the run
says so. Adding them is two functions — IDML already has the
`paragraph_view_text` the audit needs.
