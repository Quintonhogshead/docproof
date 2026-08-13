# The two deliverables, and the two channels

What a review hands back, and the distinction that runs through all of it.

## Two channels, never blurred

The Atmosphere brief draws one line harder than any other:

> Tracked changes = high-confidence mechanical or copyediting corrections.
> Comments (queries) = judgment calls, possible voice issues, structural
> questions, anything that could be intentional. Never silently change
> anything that might be voice.

docproof implements both.

**Tracked changes** are corrections. The author accepts or rejects each one in
Word, and rejecting all of them returns the manuscript exactly as it arrived
(see `docs/audit.md`).

**Queries** are questions. They are Word comments with no `w:ins`/`w:del`
around them: they change nothing, and there is nothing to accept. Two kinds of
finding land here.

### Query-only error types

An error type declares its channel in its YAML:

```yaml
key: speaker_change
channel: query        # "change" is the default
```

A query type asks because the answer is not docproof's to give.
`speaker_change` — two people talking in one paragraph — is the first: the fix
is a paragraph break, and where a line of dialogue belongs is the author's
decision, not a punctuation repair. The brief says so explicitly: *"flag it as
a comment. Do not restructure."*

The pipeline treats these differently at every step. The prompt tells the model
this type never edits and to repeat the sentence unchanged. The validator skips
the minimal-diff machinery (a query has no diff), skips the confidence gate (a
question is the output, not a fallback from a correction) and does not claim
the span, so a query may sit on text a tracked change also touches. The
reassembler writes it as a comment anchored to the whole quoted sentence.

### Findings below the confidence gate

A finding the model was not confident enough about to apply is the same thing
in practice: something worth asking about that should not be changed. With
`query_comments: true` these become margin comments too, carrying the
suggestion that was not made:

> Possibly a comma splice — left as written, because it may be deliberate.
> Two independent clauses joined by only a comma. Suggested: "The manuscript
> was finished; nobody wanted to read it."

Turn `query_comments` off and they stay in `summary.md`, where only whoever ran
the review will see them.

### One implementation detail that matters

Queries are attached **before** any tracked change is applied to a paragraph.
Comment range markers are not text, so inserting them does not move the offsets
the edits rely on — whereas applying a deletion first moves text out of `w:t`
and every later offset points at the wrong place.

A query's comment covers the whole sentence it asks about, not the characters
an edit would have touched. A below-gate comma splice would have changed one
comma, and a comment hanging off a single comma tells the author nothing.

## The two files

```
Mankin - Book 1 - Atmosphere Press Proofreader.docx
Mankin - Book 1 - Atmosphere Press Proofreader Change Log.docx
```

The names are the house convention and come from `DocumentFormat`, so anything
that has to recognise docproof's own output — the folder watcher especially —
matches on the same constants rather than a copy.

`summary.md` and `findings.json` are still written. They are for whoever ran
the review; the two files above are for the press and the author.

### The change log

Generated from the run that produced it — every number is measured, nothing is
asserted that was not.

- **Style basis**, including the English-variant assumption. Variants are not
  configurable yet, so it says which conventions were applied in their U.S.
  form and asks the press to confirm before treating the pass as final.
- **Corrections**, as a table of where / original / corrected / why, in
  document order rather than in the order the passes found them.
- **Scripted checks**, with each sweep's flagged / applied / remaining counts.
- **Applied without tracked changes** — the two silent normalizations, by
  count, flagged as things that cannot be rejected in Word.
- **Queries**, every question raised.
- **Deliberately left unchanged**, including the words the dictionary scan
  protected as the author's own.
- **Footnotes and endnotes** — whether the book has them and whether they were
  read. The brief singles this out because the usual failure is silent: most
  tools read only the body.
- **Final audit** — whether rejecting every change reproduces the original.
- **Limits of this pass** — what was not read. This section is not padding.
  A change log that lists four hundred corrections and says nothing about its
  own limits invites the reader to assume there were none.

The change log is written *before* the audit is enforced, so a run that refuses
to ship a manuscript still explains itself in the author's own document.

## Short paragraphs

`chunking.min_paragraph_chars` keeps two-word paragraphs out of the model
passes, where they would cost a request and rarely hold a grammar error. They
are still ingested and still swept, because in fiction a short paragraph is
usually a line of dialogue — `"Who?" he asked.` is sixteen characters and
carries a dialogue tag. Without this the sweeps' "zero remaining" would quietly
mean "zero remaining in the long paragraphs", which is not what anyone reading
that number would take it to mean.
