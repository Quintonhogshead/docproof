"""The corrections run from end to end: a source list becomes edits, the edits
are applied to the designer's IDML, and the result is verified against the list.

This is the corrections counterpart to `prep.pipeline` — the one function the app
calls per job. It owns no policy of its own; it wires the three stages together
and writes the report, so the app runner stays thin and every decision lives in
`parse`, `apply` and `verify`.

Two entry points, sharing the report:

  * `apply_corrections` — the usual case. The designer exports the finished book
    to IDML; we apply the corrections and hand back a corrected IDML plus the
    report. We verify our own output as a self-check (it should always be clean;
    a discrepancy here is a bug in `apply`, surfaced rather than shipped).
  * `verify_corrections` — the audit case from Nick's thread. The book was
    corrected by someone else's script; given the before file, that after file
    and the list, we report every change the list does not explain. We write no
    IDML — the after file is already the deliverable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..models import Usage
from ..providers import Provider
from .apply import apply_edits
from .idml import read_stories
from .model import (APPLIED_EXACTLY, ApplyReport, CheckItem, CommentDisposition,
                    DISP_APPLIED, DISP_FLAGGED, DISP_NO_OP, DISP_NOT_EXTRACTED,
                    Edit, JUDGMENT, PARA_STRUCTURAL, ROUTED_TO_DESIGN,
                    VerifyReport)
from .instructions import fill_edit_occurrences
from .pagemap import build_page_map, page_book_text
from .parse import ParseResult, parse_edits
from .report import write_report
from .sanity import review_edits
from .secondlook import merge_overlaps, probe, reanchor_edits, settle_queries
from .verify import verify

log = logging.getLogger("docproof.corrections.run")


@dataclass(frozen=True)
class CorrectionsOutputs:
    """Everything a corrections run produced: the file (in apply mode), the two
    report artifacts, and the three stage reports the app reads for its card."""
    report_md: Path
    report_json: Path
    parse: ParseResult
    verify: VerifyReport
    corrected_idml: Path | None = None      # None in verify-only mode
    apply: ApplyReport | None = None        # None in verify-only mode
    # One row per reviewer comment, so the change log can account for every mark —
    # applied, flagged, a no-op, or never turned into an edit. Empty when the
    # source was a typed list with no comments behind it.
    comments: tuple[CommentDisposition, ...] = ()
    # The model spend of the opt-in passes (second look, sanity gate), when any
    # ran; None on the free path.
    usage: Usage | None = None
    # What the opt-in second look repaired: queries settled into concrete edits,
    # lost anchors re-quoted from the book's page text, and colliding sets of
    # corrections merged into one. All 0 when the pass was off, or when every
    # flag it read really was a person's.
    settled: int = 0
    reanchored: int = 0
    merged: int = 0
    # How much of the proof the page map could place in the book. A page it could
    # not place is a page whose marks fall back to searching the whole book, so a
    # low number here explains a run with more flags than expected — and stops the
    # narrowing degrading silently.
    pages_placed: int = 0
    pages_total: int = 0
    # What a person still has to look at in InDesign, because composing the page is
    # the one thing this engine cannot do and so cannot check.
    checks: tuple[CheckItem, ...] = ()

    @property
    def applied(self) -> int:
        return self.apply.applied if self.apply is not None else 0

    @property
    def flagged(self) -> int:
        """Corrections refused rather than guessed at — the ones a human owns."""
        if self.apply is not None:
            return len(self.apply.flagged)
        # Verify-only: a correction the after file does not carry is the same
        # idea, read off the reconciliations.
        return sum(1 for r in self.verify.reconciliations
                   if r.status != APPLIED_EXACTLY)

    @property
    def discrepancies(self) -> int:
        return len(self.verify.discrepancies)

    @property
    def total_comments(self) -> int:
        return len(self.comments)

    @property
    def applied_comments(self) -> int:
        """Reviewer comments whose edit(s) landed — the comment-level 'applied'.

        This is the count that reconciles with `total_comments`; `applied` is an
        *edit* count and cannot, because one comment may become several edits or
        none, and a typed edit may carry no comment at all. Headlines read off a
        proof (comments present) count in this unit so the buckets sum to the
        total the reviewer actually marked."""
        return sum(1 for c in self.comments if c.disposition == DISP_APPLIED)

    @property
    def no_change_comments(self) -> int:
        """Reviewer comments whose edit was a true no-op — the text already read
        the way the mark asked. Neither applied nor a human's to own, so it is its
        own bucket; dropping it is what left the old headline short of the total."""
        return sum(1 for c in self.comments if c.disposition == DISP_NO_OP)

    @property
    def unresolved(self) -> int:
        """Reviewer comments a person still owns — flagged, or never turned into
        an edit at all. The count that used to be invisible.

        `applied_comments + no_change_comments + unresolved == total_comments`, by
        construction: every comment reaches exactly one of the four dispositions,
        and these three buckets name all of them (unresolved folds the two
        needs-a-human ones)."""
        return sum(1 for c in self.comments if c.needs_human)

    @property
    def to_check(self) -> int:
        """Rows on the designer's check list. Not a failure — a bounded, located
        handover of the part that needs InDesign open."""
        return len(self.checks)

    @property
    def clean(self) -> bool:
        """Every correction landed exactly and nothing else changed — and every
        entry in the source parsed, and no comment was left unresolved."""
        return (self.verify.clean and self.parse.ok and self.flagged == 0
                and self.unresolved == 0)


# What to look at, per paragraph operation. Each of these changes where text falls
# on the page, and where text falls is InDesign's answer, not ours — so the engine
# says what it did and what that should have caused, and a person confirms it.
CHECK_AFTER: dict[str, str] = {
    "recto": "this now starts on a right-hand page — check the page before it does "
             "not run short",
    "verso": "this now starts on a left-hand page — check the page before it does "
             "not run short",
    "page-break": "this now starts a page — check the page before it does not run "
                  "short",
    "column-break": "this now starts a column",
    "frame-break": "this now starts a new frame",
    "no-page-break": "the forced break here is gone — check the text runs on as it "
                     "should",
    "keep-with-next": "this should now sit with the text that follows it, not at "
                      "the foot of a page",
    "keep-together": "this should no longer split across a page",
    "allow-break": "this may now split across a page",
    "delete-paragraph": "a paragraph was removed — the pages after it have reflowed",
    "insert-after": "a paragraph was added — the pages after it have reflowed",
    "insert-before": "a paragraph was added — the pages after it have reflowed",
}


def _checks(apply_report: ApplyReport | None) -> tuple[CheckItem, ...]:
    """Everything a person has to look at in InDesign once this run is done.

    Two sources, and they are the two halves of the same limit. A reviewer's note
    about how the page composed — a widow, a bad rag, a line that broke badly —
    cannot be applied, because the thing it describes only exists once InDesign sets
    the text. And a paragraph operation this engine *did* apply cannot be confirmed
    by any comparison it can make, for the same reason: the file now says "start on
    a right-hand page", and whether that left the previous page half empty is a
    question about the composed book.

    So neither is claimed. Both are handed over located — the proof page the mark was
    on, the paragraph it landed in, what to look at and why."""
    if apply_report is None:
        return ()
    out: list[CheckItem] = []
    for o in apply_report.outcomes:
        if o.status == ROUTED_TO_DESIGN:
            out.append(CheckItem(
                page=o.edit.page, story_id=o.story_id, paragraph=o.paragraph,
                what="the reviewer marked something about how this page composed, "
                     "which no file comparison can settle",
                why=o.edit.instruction or "(no note)", edit_ids=(o.edit.id,)))
        elif o.applied and o.edit.paragraph:
            out.append(CheckItem(
                page=o.edit.page, story_id=o.story_id, paragraph=o.paragraph,
                what=CHECK_AFTER.get(o.edit.paragraph,
                                     "a paragraph setting changed here"),
                why=o.edit.instruction or o.edit.paragraph,
                edit_ids=(o.edit.id,)))
    return tuple(out)


def corrected_name(src_idml: str | Path) -> str:
    """The default name for the corrected file: the source stem with a suffix, so
    a designer can tell it from the export they sent."""
    return f"{Path(src_idml).stem}_corrected.idml"


def _comment_fields(c) -> tuple[str, int, str, str, str]:
    """(id, page, kind, instruction, anchor) from a `PdfComment` or a plain dict,
    so the ledger reads the comment list however it arrived."""
    if isinstance(c, dict):
        return (str(c.get("id") or ""), int(c.get("page") or 0),
                str(c.get("kind") or ""), str(c.get("instruction") or ""),
                str(c.get("anchor") or c.get("context") or ""))
    return (getattr(c, "id", "") or "", getattr(c, "page", 0) or 0,
            getattr(c, "kind", "") or "", getattr(c, "instruction", "") or "",
            getattr(c, "anchor", "") or getattr(c, "context", "") or "")


def _reconcile_comments(comments, edits: Sequence[Edit],
                        status_of) -> tuple[CommentDisposition, ...]:
    """One `CommentDisposition` per reviewer comment: match every edit back to the
    comment it names (`edit.source`), and read the edit's outcome to decide what
    became of the comment. A comment no edit names is `not_extracted` — the mark
    the model turned into nothing, which is the one this whole change exists to
    stop losing. `status_of(edit_id) -> (disposition, detail)` classifies one
    edit's fate, so apply and verify can share this reconciliation."""
    if not comments:
        return ()
    by_source: dict[str, list[Edit]] = {}
    for e in edits:
        # An edit may cite more than one comment: a reviewer who marks the opening
        # and closing quotation marks separately has made one request twice, and it
        # becomes one edit that both comments have to be told the outcome of.
        for cid in e.source.split():
            by_source.setdefault(cid, []).append(e)
    out: list[CommentDisposition] = []
    for c in comments:
        cid, page, kind, instruction, anchor = _comment_fields(c)
        made = by_source.get(cid, [])
        if not made:
            out.append(CommentDisposition(
                id=cid, page=page, kind=kind, instruction=instruction,
                anchor=anchor, disposition=DISP_NOT_EXTRACTED))
            continue
        classified = [status_of(e.id) for e in made]
        # The comment inherits its most attention-needing edit: a flag beats an
        # applied beats a no-op, so a comment is never quietly "applied" while one
        # of its edits still needs a human.
        flagged = [d for d in classified if d[0] == DISP_FLAGGED]
        if flagged:
            disp, detail = DISP_FLAGGED, flagged[0][1]
        elif any(d[0] == DISP_APPLIED for d in classified):
            disp, detail = DISP_APPLIED, ""
        else:
            disp, detail = DISP_NO_OP, ""
        out.append(CommentDisposition(
            id=cid, page=page, kind=kind, instruction=instruction, anchor=anchor,
            disposition=disp, edit_ids=tuple(e.id for e in made), detail=detail))
    return tuple(out)


def _apply_status_of(apply_report: ApplyReport):
    """A `status_of(edit_id)` over an apply run's outcomes."""
    by_id = {o.edit.id: o for o in apply_report.outcomes}

    def status_of(edit_id: str) -> tuple[str, str]:
        o = by_id.get(edit_id)
        if o is None:
            return DISP_NOT_EXTRACTED, ""
        if o.applied:
            return DISP_APPLIED, ""
        if o.needs_human:
            return DISP_FLAGGED, (o.detail or o.status)
        # A no-op the model marked "judgment" is a query it would not turn into a
        # concrete change — a person should decide, so it is flagged, not filed
        # under "no change needed" where an editor would never see it.
        if o.edit.kind == JUDGMENT:
            return DISP_FLAGGED, ("a query for a person — the model proposed no "
                                  "concrete change")
        return DISP_NO_OP, ""                  # a true no-op (find == replace)

    return status_of


def apply_corrections(src_idml: str | Path, corrections, out_dir: str | Path, *,
                      id_prefix: str = "c", dest_name: str | None = None,
                      comments: Sequence | None = None,
                      sanity: tuple[Provider, str] | None = None,
                      second_look: tuple[Provider, str] | None = None,
                      page_texts: Sequence[str] | None = None
                      ) -> CorrectionsOutputs:
    """Apply a corrections source to `src_idml`, writing the corrected IDML and
    the report into `out_dir`.

    `corrections` is anything `parse.parse_edits` accepts (a list, JSON text, a
    `.json` path). `src_idml` is never modified. Entries that cannot be parsed
    and edits that cannot be anchored are reported, never guessed.

    `comments` is the original reviewer comments (from the marked-up PDF), each
    with the `id` its edits cite in `source`; when given, every comment gets a row
    in the report — applied, flagged, a no-op, or never extracted — so none is
    lost. `sanity` is an optional `(provider, model)` gate that reads each proposed
    edit and holds a doubtful one back for a human instead of applying it; it adds
    a small model spend (returned on the outputs) and is off by default.
    `second_look` is the other opt-in `(provider, model)` pass, run first, with
    three repairs (see `secondlook`): it commits the delegated queries — a
    reviewer offering alternatives, a conditional the page settles — to concrete
    edits; it re-quotes an anchor the book does not carry (an artifact-ridden
    find, a repeated one nothing chose between) from the book's own page text;
    and it merges corrections that collided on one span into a single edit.
    Everything it produces then anchors, gates and verifies like any other edit.
    Both passes share the returned usage.

    `page_texts` is the text of each page of the proof, in order, as the PDF reader
    read it. Given it, each page is aligned against the book's own text once, and a
    correction then narrows to the run of text the page it was marked on actually
    set — which is what lets a mark on a comma land at all, instead of finding
    seven thousand of them. It is a narrower and nothing more: a page the alignment
    cannot place costs precision on that page's marks, never a correction."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    parsed = parse_edits(corrections, id_prefix=id_prefix)
    edits = list(parsed.edits)

    usage = Usage() if (sanity is not None or second_look is not None) else None

    # The page map, built once and shared by the apply and the self-check so both
    # place every edit identically. The book's own text per cited page rides with
    # it: the second look quotes its anchors from it, and the ordinal fill below
    # counts a repeated word's copies over it.
    scope = None
    book_pages: dict[int, str] = {}
    pages_placed = pages_total = 0
    if page_texts:
        page_texts = list(page_texts)
        pages_total = len(page_texts)
        stories = read_stories(src_idml)
        scope = build_page_map(stories, page_texts)
        pages_placed = scope.placed
        cited = {e.page for e in edits if e.page and scope.knows(e.page)}
        book_pages = {p: page_book_text(stories, scope, p) for p in cited}

    # The second look runs before the sanity gate on purpose: what it settles
    # becomes an ordinary proposed edit, and the gate then reads the model's
    # choices with the same suspicion as everyone else's.
    settled = 0
    if second_look is not None:
        provider, model = second_look
        edits, settled = settle_queries(edits, provider, model=model,
                                        usage=usage, book_pages=book_pages)

    # Fill the copy-ordinal for a repeated-word edit the model could not count.
    # The rule-resolved edits already carry it; a model-read one is pinned here
    # to the copy the mark sits on, read the same deterministic way the rules
    # read it — from the citing comment's own offset, the book text settling the
    # count and the proof's page text settling the position. This is the one
    # place a model-read edit and its mark are back together with both
    # renderings, so it is where the ordinal the model is unreliable at is put
    # right. It runs before the second look's dry run below, so an edit the
    # mark's own position can pin is never mistaken for a lost anchor.
    if scope is not None and comments:
        edits = fill_edit_occurrences(edits, comments, book_pages=book_pages,
                                      pdf_pages=page_texts)

    # The second look's other two repairs need to know what a real apply would
    # flag, so a dry run on throwaway stories finds the edits that would fail —
    # the lost anchors are re-quoted from the book's own page text, and each
    # set of corrections that collided on one span is merged into a single
    # edit. Both go back through the real apply below like anything else.
    reanchored = merged = 0
    if second_look is not None and book_pages:
        provider, model = second_look
        lost, collisions = probe(read_stories(src_idml), edits, scope=scope)
        if lost:
            edits, reanchored = reanchor_edits(
                edits, lost, provider, model=model, usage=usage,
                book_pages=book_pages)
        if collisions:
            edits, merged = merge_overlaps(
                edits, collisions, provider, model=model, usage=usage,
                book_pages=book_pages)

    withheld: dict[str, str] = {}
    if sanity is not None:
        provider, model = sanity
        withheld = review_edits(edits, provider, model=model, usage=usage)

    corrected = out / (dest_name or corrected_name(src_idml))
    apply_report = apply_edits(src_idml, corrected, edits, withheld=withheld,
                               scope=scope)
    # Self-check: our own output, verified against the same list (withholding the
    # same edits, so a held-back one is not read as an unaccounted change). Clean
    # by construction unless `apply` has a bug — then the report says so instead
    # of the file going out unflagged.
    verify_report = verify(src_idml, corrected, edits, withheld=withheld,
                           scope=scope)

    dispositions = _reconcile_comments(comments or (), edits,
                                       _apply_status_of(apply_report))
    checks = _checks(apply_report)
    report_md, report_json = write_report(
        out, source_path=src_idml, after_path=corrected, parse=parsed,
        apply=apply_report, verify=verify_report, comments=dispositions,
        deterministic=(sanity is None and second_look is None),
        pages=(pages_placed, pages_total), checks=checks)
    log.info("Corrections applied to %s: %s; %d comment(s), %d unresolved; "
             "second look settled %d, re-anchored %d, merged %d; %d/%d page(s) "
             "placed; verify %s",
             Path(src_idml).name, apply_report.summary(), len(dispositions),
             sum(1 for d in dispositions if d.needs_human),
             settled, reanchored, merged, pages_placed, pages_total,
             "clean" if verify_report.clean else "has discrepancies")
    return CorrectionsOutputs(
        report_md=report_md, report_json=report_json, parse=parsed,
        verify=verify_report, corrected_idml=corrected, apply=apply_report,
        comments=dispositions, usage=usage, settled=settled,
        reanchored=reanchored, merged=merged,
        pages_placed=pages_placed, pages_total=pages_total, checks=checks)


def _verify_status_of(verify_report: VerifyReport):
    """A `status_of(edit_id)` over a verify run's reconciliations: an edit the
    after file carries exactly is applied; one carried differently or not at all
    still needs a human."""
    by_id = {r.edit.id: r for r in verify_report.reconciliations}

    def status_of(edit_id: str) -> tuple[str, str]:
        r = by_id.get(edit_id)
        if r is None:
            return DISP_NOT_EXTRACTED, ""
        if r.status == APPLIED_EXACTLY:
            if r.edit.kind == JUDGMENT and r.edit.find == r.edit.replace:
                return DISP_FLAGGED, ("a query for a person — the model proposed "
                                      "no concrete change")
            return DISP_APPLIED, ""
        return DISP_FLAGGED, (r.detail or r.status)

    return status_of


def verify_corrections(before_idml: str | Path, after_idml: str | Path,
                       corrections, out_dir: str | Path, *,
                       id_prefix: str = "c",
                       comments: Sequence | None = None) -> CorrectionsOutputs:
    """Audit an already-corrected IDML against the corrections list.

    No IDML is written — `after_idml` is the deliverable, produced elsewhere (a
    hand-run script, InDesign's own find/change). The report names every change
    in it that the list does not account for. `comments`, when given, is
    reconciled against the after file the same way apply mode does."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    parsed = parse_edits(corrections, id_prefix=id_prefix)
    edits = list(parsed.edits)
    verify_report = verify(before_idml, after_idml, edits)
    dispositions = _reconcile_comments(comments or (), edits,
                                       _verify_status_of(verify_report))
    report_md, report_json = write_report(
        out, source_path=before_idml, after_path=after_idml, parse=parsed,
        apply=None, verify=verify_report, comments=dispositions)
    log.info("Corrections verified for %s: %s",
             Path(after_idml).name,
             "clean" if verify_report.clean else
             f"{len(verify_report.discrepancies)} discrepancy(ies)")
    return CorrectionsOutputs(
        report_md=report_md, report_json=report_json, parse=parsed,
        verify=verify_report, corrected_idml=None, apply=None,
        comments=dispositions)
