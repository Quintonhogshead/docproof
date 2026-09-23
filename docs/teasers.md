# Author teasers

Every accepted formatting job queues its teaser work when formatting starts. App
and DocWatch jobs share the same durable queue. Completion is an idempotent
fallback; poll recovery finds running and completed jobs since activation.
Teaser failures do not change the formatting result.

Sol (`gpt-5.6-sol`, high) uses the existing Codex subscription login on Fly through
the serialized Codex CLI runner. It reads every manuscript portion and selects
five distinct public fact sheets, each at most 450 words. It audits those briefs
against source evidence before any writer call. No paid OpenAI API fallback is used.

The web server makes five concurrent DeepInfra requests to
`deepseek-ai/DeepSeek-V4-Flash-0731`, one per brief. Each request receives ONLY that
option's selected facts, angle, positive direction, and the output contract.
Private source, endings, excluded details, reviewer findings and rejected drafts
never go to DeepInfra. Each result must contain exactly three paragraphs and
150–200 words. Successful options are saved individually; retries request only
missing options. Twelve generation rounds per day bound automatic retries.

Sol checks the resulting package against the full source before delivery. Brief
revisions stay private; DeepSeek sees only newly selected public facts. Exact,
bounded factual corrections can be approved in the same review. Every approval
is tied to the draft hash and complete manuscript coverage. All five options are
presented in order without rankings.

## Speed (v0.231.0)

A book is read in full once. Portion readings run four at a time on one shared
subscription session at medium effort; the session holds the login lock only for
that batch, so Galley's calls interleave between batches. Every later check —
the fact-sheet audit and each draft review — uses those saved readings plus the
original passages their facts cite, never a second pass over the manuscript.
A rejected fact selection is reselected in the same attempt (up to three rounds).
A teaser outside 150–200 words or three paragraphs is rewritten immediately by
the writer (up to four tries, told only the counts). Subscription outages — busy,
unavailable, rate limited, CLI failure — retry in two minutes (fifteen once an
outage persists or for a usage limit) and never count against the book or reach
Sol as feedback. Counted failures back off to at most thirty minutes. Extraction
damage and unsupported claims in the manuscript are recorded as limitations,
not a reason to refuse.

The shared Google folder is named exactly `author teasers`. Every book gets:

- A native Google Doc with five options and the exact requested warning:
  “these are made by our staff, you should change them if you wish, talk to your Developmental Editor”
- The approved two-page dos and donts guide as an editable spreadsheet and PDF.

The shipped guide files live in `config/teasers`. Google uploads are reconciled
by task properties, document text is read back, and guide checksums and folder
membership are verified before the task is complete. All files retain the
connected account's existing access controls; no sharing invitations are sent.

Workflow version 3 introduces this contract. Unfinished production version 2
teaser jobs migrate on claim, retaining their prior storysheet, drafts and reviews
in the private audit. Completed and stopped jobs remain unchanged. Version 1
regression paths remain for older queued work. No formatting book is rerun by a
teaser migration.

The admin toggle controls new work. The destination is created and verified before
enabling the lane. Deployment must update both the web code and teaser sidecar;
never restart a protected Galley run just to update teasers.
