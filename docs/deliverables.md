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
Mankin - Book 1 - Pre-Proofread.docx
Mankin - Book 1 - Pre-Proofread Change Log.docx
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
- **Not read in this pass**, when a reading pass did not return — the sections
  and error types it covered, and why it failed. Written only when it happens,
  and it withdraws the "every paragraph was read" claim above it rather than
  sitting beside a sentence that contradicts it. See *Coverage* below.

The change log is written *before* the audit is enforced, so a run that refuses
to ship a manuscript still explains itself in the author's own document.

It can be switched off — `change_log: false`, or the switch in **Settings** —
and the switch is worth reading carefully before it is used. The change log
costs no API call: it is written from findings the review has already paid for,
so turning it off buys tidiness rather than money, and it takes the audit
result and the limits statement out of the author's hands with it.

## Short paragraphs

`chunking.min_paragraph_chars` keeps two-word paragraphs out of the model
passes, where they would cost a request and rarely hold a grammar error. They
are still ingested and still swept, because in fiction a short paragraph is
usually a line of dialogue — `"Who?" he asked.` is sixteen characters and
carries a dialogue tag. Without this the sweeps' "zero remaining" would quietly
mean "zero remaining in the long paragraphs", which is not what anyone reading
that number would take it to mean.

## Coverage

A review is one API call per (pass, chunk): six passes over three chunks is
eighteen calls. A call can fail — the model refuses, the output is truncated at
`api.max_output_tokens`, the request errors, the response doesn't match the
schema — and when it does, `_unwrap` logs it and the run carries on with the
rest. That is the right behaviour: losing one pass over one section is not a
reason to throw away seventeen calls that were paid for and succeeded.

What was wrong is that it left no trace. A pass that answered "nothing wrong in
this section" and a pass that never answered both contributed zero findings, so
a run that lost one read as a clean run that happened to find less. Two reviews
of one manuscript four minutes apart applied 155 and 110 changes; the gap was
one truncated call, and neither report said so.

So every call is now recorded, not just its findings:

- **`findings.json`** carries a `passes` block — `requested`, `answered`,
  `complete`, and one row per call with `chunk_id`, `pass`, `error_types`,
  `answered`, `returned`, `kept`, and a `reason` when it failed. `null` when
  the caller doesn't track calls, because an unknown is not a pass.
- **`summary.md`** says *Coverage: complete* under the status counts, or
  replaces that line with a section naming the sections and error types that
  were never read.
- **The change log** withdraws its "every paragraph of running text was read"
  claim and names what was missed — see above.
- **The app's report screen** leads with a warning, because its headline reads
  "Nothing needed changing" on an empty review, which is the one sentence a
  lost pass turns into a lie.

`returned` and `kept` differ when findings came back and were then dropped for
quoting a paragraph that isn't there. That is a different fault from a call
that never landed, and it is not reported as missing coverage — otherwise the
warning would cry wolf on every run.

Truncation is the fixable one. A pass loses the *whole* section when its answer
is cut off, so the sections with the most to fix are the ones most likely to be
lost — exactly backwards from what you want. Raise `api.max_output_tokens`, or
split that pass's error types across two smaller passes.

Nothing here retries automatically. A failed call is not written to the
checkpoint as complete, so re-running the review retries it and replays
everything that succeeded.
