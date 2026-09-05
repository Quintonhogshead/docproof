from __future__ import annotations

import dataclasses
import logging
import re

from .models import (Anchor, CONFIDENCE_RANK, DocumentModel, Finding,
                     index_paragraphs)

log = logging.getLogger("docproof.validator")


def find_nth(haystack: str, needle: str, n: int) -> int:
    """Start offset of the n-th (1-based) occurrence, or -1."""
    start = -1
    for _ in range(n):
        start = haystack.find(needle, start + 1)
        if start == -1:
            return -1
    return start


# Curly quotes, dashes and non-breaking spaces, each folded to its ASCII form
# ONE character for one. The manuscript's normalizer curls quotes before ingest,
# so the canonical text holds “ ” ‘ ’; a weaker model routinely re-types the
# sentence with "straight" punctuation, and an exact substring search then fails
# and the finding is discarded as unanchorable even though it is the same
# sentence character for character. Folding both sides recovers it. The fold is
# deliberately length-preserving — ellipsis (…→...) and whitespace runs are NOT
# folded — so an offset found in the folded text still indexes the real text,
# and the tracked change lands on the manuscript's own characters. The
# continuity read's two-quote guardrail locates with this same fold
# (continuity._locate), so the two passes never disagree about what counts as
# the same sentence.
_ANCHOR_FOLD = str.maketrans({
    "“": '"', "”": '"',      # “ ”
    "‘": "'", "’": "'",      # ‘ ’
    "–": "-", "—": "-",      # – —
    " ": " ",                     # non-breaking space
})


def fold_punct(s: str) -> str:
    return s.translate(_ANCHOR_FOLD)


def anchor_offset(haystack: str, needle: str, n: int) -> int:
    """Start offset of the n-th occurrence of `needle` in `haystack`, or -1.

    Exact first; on failure, a punctuation-tolerant retry that folds curly
    quotes, dashes and nbsp on both sides (see `_ANCHOR_FOLD`). Because the fold
    never moves a character, the offset it returns indexes the ORIGINAL
    `haystack`, so callers slice the real text for the edit."""
    s = find_nth(haystack, needle, n)
    if s != -1:
        return s
    return find_nth(fold_punct(haystack), fold_punct(needle), n)


def shrink(original: str, corrected: str) -> tuple[int, str, str]:
    """Trim common prefix and suffix; return (prefix_len, deleted, inserted).
    shrink('It was late, we left.', 'It was late. We left.')
      -> (11, ', w', '. W')"""
    pre = 0
    while (pre < len(original) and pre < len(corrected)
           and original[pre] == corrected[pre]):
        pre += 1
    suf = 0
    while (suf < len(original) - pre and suf < len(corrected) - pre
           and original[len(original) - 1 - suf] == corrected[len(corrected) - 1 - suf]):
        suf += 1
    return pre, original[pre:len(original) - suf], corrected[pre:len(corrected) - suf]


# Two changed regions closer together than this many equal characters are one
# conceptual edit ("was going"->"went" leaves a 1-char island); keeping them as
# one region avoids confetti in the tracked-changes view without re-widening a
# genuinely separate second fix back into the first one's span.
_REGION_JOIN_GAP = 3


def minimal_regions(original: str, corrected: str
                    ) -> list[tuple[int, str, str]]:
    """Split one original->corrected pair into its minimal changed regions.

    Returns ``[(offset_in_original, deleted, inserted), ...]`` in ascending
    offset order. A single-touch pair returns exactly what `shrink` would; a
    paragraph-sized row carrying several distant fixes returns one region per
    fix, so the author reviews "w"->"W", not a 300-character block replace.
    Regions separated by fewer than `_REGION_JOIN_GAP` unchanged characters are
    merged, so a tight compound touch still reads as one change."""
    import difflib

    pre, deleted, inserted = shrink(original, corrected)
    if not deleted and not inserted:
        return []
    # Fast path: a small single touch needs no matcher.
    if len(deleted) <= _REGION_JOIN_GAP and len(inserted) <= _REGION_JOIN_GAP:
        return [(pre, deleted, inserted)]
    sm = difflib.SequenceMatcher(None, deleted, inserted, autojunk=False)
    regions: list[tuple[int, int, str]] = []       # (start, end, replacement)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        regions.append((i1, i2, inserted[j1:j2]))
    if not regions:
        return [(pre, deleted, inserted)]
    merged: list[tuple[int, int, str]] = [regions[0]]
    for a, b, repl in regions[1:]:
        pa, pb, prepl = merged[-1]
        if a - pb < _REGION_JOIN_GAP:
            merged[-1] = (pa, b, prepl + deleted[pb:a] + repl)
        else:
            merged.append((a, b, repl))
    if len(merged) > 24:      # confetti guard (and the id-suffix alphabet cap)
        return [(pre, deleted, inserted)]
    return [(pre + a, deleted[a:b], repl) for a, b, repl in merged]


# A row that came through the import / replay path — a curated human edit or a
# replayed prior decision — as opposed to a machine floor/model row generated
# this run. build_findings stamps every such row's finding_id with an "import"
# or "replay" prefix and (unless it kept its own type) the "imported_edit" type;
# either signal identifies it. Used only to give a hand-made decision claim
# precedence over the machine floor on a contested span (see validate_findings).
_IMPORT_ID_PREFIXES = ("import", "replay", "curated")


# --- number labels are not prose numbers -------------------------------------
#
# The typed number_style pass spells a numeral out when it reads as a count in a
# sentence ("she had 3 dogs" -> "three dogs"). A LABEL is not that: "Mindset
# Number 23" is the name of a section, and the Redding Book 1 run rewrote it as
# "Mindset Number twenty-three" — an edit no house style asks for and no reader
# would accept. A numbered label is identified three ways: a labelling noun
# followed by its number, a list marker opening the line, and a heading.
_NUMBER_LABEL = re.compile(
    r"\b(Mindset|Chapter|Part|Number|Step|Day|Week|Lesson|Figure|Table"
    r"|Section)\s+\d+")
_LIST_LABEL = re.compile(r"^\s*\d+\)")
_HEADING_STYLES = ("heading", "title", "subtitle")


def _line_at(text: str, pos: int) -> tuple[int, str]:
    """The line containing `pos`, and where it starts. A canonical paragraph
    keeps its w:br line breaks, so a label can sit on its own line inside an
    otherwise ordinary paragraph."""
    start = text.rfind("\n", 0, pos) + 1
    stop = text.find("\n", pos)
    stop = len(text) if stop == -1 else stop
    return start, text[start:stop]


def _looks_like_heading(style: str, line: str) -> bool:
    st = (style or "").strip().lower()
    if st.startswith(_HEADING_STYLES):
        return True
    t = line.strip().rstrip("\"'”’)")
    if not t or len(t) > 60 or t[-1] in ".!?…,;:":
        return False
    words = t.split()
    if len(words) > 8:
        return False
    lead = sum(1 for w in words if w[:1].isupper() or w[:1].isdigit())
    return lead / len(words) > 0.6


def _is_number_label(para, start: int, end: int) -> bool:
    """Whether the span [start, end) of this paragraph is part of a label
    rather than a number in a sentence."""
    text = para.text
    if any(_overlaps(start, end, m.start(), m.end())
           for m in _NUMBER_LABEL.finditer(text)):
        return True
    line_start, line = _line_at(text, start)
    m = _LIST_LABEL.match(line)
    if m and start < line_start + m.end():
        return True
    return _looks_like_heading(getattr(para, "style", ""), line)


_DOUBLED_WORD_RE = re.compile(r"\b([A-Za-z]+)[ \u00a0]+\1\b", re.IGNORECASE)


def doubled_words(text: str) -> dict[str, int]:
    """Each word that appears twice in a row (case-insensitive), with how
    many times it does."""
    out: dict[str, int] = {}
    for m in _DOUBLED_WORD_RE.finditer(text):
        w = m.group(1).lower()
        out[w] = out.get(w, 0) + 1
    return out


def introduces_doubled_word(before: str, after: str) -> str | None:
    """The doubled word `after` carries more of than `before` does, or None.
    A Luna chapter-sweep proposal itself contained "and and," (Georgis,
    2026-09-04); a fix that ships a doubled word the author did not write is
    the artifact, whatever else it repairs. Compared as counts so a
    deliberate doubling the original already had ("that that") passes."""
    had = doubled_words(before)
    for w, n in doubled_words(after).items():
        if n > had.get(w, 0):
            return w
    return None


def _word_continues(text: str, pos: int) -> bool:
    """Whether the character at `pos` continues a word begun just before it:
    a letter/digit, or an apostrophe that a letter follows ("apple’s")."""
    if pos < 0 or pos >= len(text):
        return False
    c = text[pos]
    if c.isalnum():
        return True
    return c in "’'" and pos + 1 < len(text) and text[pos + 1].isalnum()


def anchors_midword(para_text: str, s: int, original: str,
                    pre: int, deleted: str) -> bool:
    """Whether a quote that anchored at `s` cuts a word in half at an edge the
    minimal diff (`pre`, `deleted`) touches.

    `import-findings` anchors by plain substring, so "our bond born" anchors
    inside "our bond borne" and an edit to its last word splices INSIDE
    "borne" (Georgis, 2026-09-04: it composed "was was born"). A quote may
    still start or end mid-word when the edit sits elsewhere in it — only
    the cut word the edit reaches is unsafe."""
    if not original:
        return False
    end = s + len(original)
    hi = pre + len(deleted)
    # the cut word at the END of the quote
    if original[-1].isalnum() and _word_continues(para_text, end):
        wstart = len(original)
        while wstart > 0 and original[wstart - 1].isalnum():
            wstart -= 1
        if hi > wstart or (not deleted and pre >= wstart):
            return True
    # the cut word at the START of the quote
    if original[0].isalnum() and s > 0 and (
            para_text[s - 1].isalnum()
            or (para_text[s - 1] in "’'" and s > 1
                and para_text[s - 2].isalnum())):
        wend = 0
        while wend < len(original) and original[wend].isalnum():
            wend += 1
        if pre < wend or (not deleted and pre <= wend):
            return True
    return False


_NEIGHBOUR_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?")


def duplicates_neighbour(para_text: str, start: int, end: int,
                         inserted: str) -> str | None:
    """The word the inserted/replacement text repeats from IMMEDIATELY before
    or after its span in the paragraph, or None. A `missing_word` row inserted
    "Kalamata." before a sentence beginning "Kalamata, the capital…" (source
    "arrived in. Kalamata,", Georgis 2026-09-04) — read inside the edit it
    looked fine; read in the paragraph it doubled the name. Case-insensitive,
    punctuation ignored; a word the deleted span itself carried is not a
    repeat (re-typing "Kalamata" as "Kalamata." is not a doubling)."""
    words = [w.lower() for w in _NEIGHBOUR_WORD_RE.findall(inserted)]
    if not words:
        return None
    deleted = {w.lower() for w in _NEIGHBOUR_WORD_RE.findall(para_text[start:end])}
    before = _NEIGHBOUR_WORD_RE.findall(para_text[max(0, start - 60):start])
    after = _NEIGHBOUR_WORD_RE.findall(para_text[end:end + 60])
    if before and words[0] == before[-1].lower() and words[0] not in deleted:
        return before[-1]
    if after and words[-1] == after[0].lower() and words[-1] not in deleted:
        return after[0]
    return None


# A currency_style / number_style "correction" that produces a sub-dollar
# amount in numerals: "a dime" -> "$0.10", "5 cents" -> "$0.05", "90 cents"
# -> "$0.90" all shipped from the Georgis ladder. Chicago 9.20 keeps amounts
# under a dollar in words; the prompt now says so, and this is the
# deterministic backstop for a model that ignores it.
_SUB_DOLLAR_RE = re.compile(r"\$0\.\d{2}\b")
_MONEY_STYLE_TYPES = ("currency_style", "number_style")


def _is_imported(f: Finding) -> bool:
    return (f.finding_id.split("-", 1)[0] in _IMPORT_ID_PREFIXES
            or f.error_type in ("imported_edit", "galley_settle"))


def validate_findings(findings: list[Finding], doc: DocumentModel,
                      min_confidence: str,
                      query_types: frozenset[str] = frozenset(),
                      format_types: dict[str, str] | None = None,
                      edit_guard=None,
                      guard_exempt: frozenset[str] = frozenset(),
                      sweep_guard=None) -> list[Finding]:
    """Anchor every finding, and decide which channel it goes down.

    `sweep_guard` (a docproof.sweepguard.SweepGuard built from the run's
    configured sweeps) refuses any edit the house sweeps would re-fire on
    inside its changed span — the typed `capitalization` pass lowercasing
    "2:30 AM" to "2:30 am" (Georgis, 2026-09-04) — with status
    `rejected_undoes_house_style:<sweep key>`. None (the default) skips the
    check, so a bare call behaves as before.

    `query_types` are error types that ask rather than correct. They skip the
    edit machinery entirely: there is no minimal diff to compute, no confidence
    gate to pass (a question is the output, not a fallback from one), and no
    span to claim — a query may sit on text a tracked change also touches,
    because a comment and an edit are different things to a reader.

    `format_types` maps an error type to the run formatting it applies. Those
    findings change how text is set rather than what it says, so there is no
    diff to shrink either, and they claim spans in a register of their own —
    italicising a title and fixing a typo inside it are not in conflict.

    `edit_guard` (an EditGuardConfig) is the overreach safety net: a correction
    that re-types a large span or adds a lot of new text is the model rewriting,
    not proofreading, and is rejected before it can become a tracked change. It
    applies only to the shrink-to-a-diff path — a query or a formatting mark is
    not an edit — and never trips a sweep, whose changes are punctuation-sized.
    None disables it, which is the default so a bare call stays unguarded.

    `guard_exempt` names error types the edit guard does not apply to. The one
    caller is the repair channel: a sentence repair legitimately inserts a
    dropped clause or word that the 16-char growth cap would refuse as
    fabrication, and its fabrication defence is elsewhere — the cluster judge
    ruled the whole sentence meaning-preserved before any member reached here.
    The guard still runs on every other source, so exempting repair does not
    loosen the net for the model's own token edits. Empty by default."""
    format_types = format_types or {}
    paras = index_paragraphs(doc)
    threshold = CONFIDENCE_RANK[min_confidence]
    accepted_spans: dict[str, list[tuple[int, int]]] = {}
    formatted_spans: dict[str, list[tuple[int, int]]] = {}
    seen: set[tuple] = set()
    out: list[Finding] = []

    # Claim precedence. The loop below is first-come-wins on any contested span,
    # so INPUT ORDER is precedence — and the caller's order already encodes the
    # machine hierarchy (sweeps > consistency > repair cluster > sapling > model
    # > promoted > smoothing). What that order does NOT capture is a hand-made
    # row: a curated/imported/replayed edit is a human decision and must outrank
    # any machine floor or model row it contends with — on the Purpura replay a
    # floor row silently beat a curated fix (terd->turd lost to a junk terd->nerd)
    # purely because it happened to be listed first. So promote imported rows
    # ahead of the machine rows here, shorter span first among them (the minimal
    # fix wins a genuine overlap between two human rows), and keep every other
    # row in the caller's exact relative position. A run with no imported rows is
    # therefore reordered by nothing and behaves byte-for-byte as before.
    findings = sorted(
        findings, key=lambda f: ((0, len(f.original_text)) if _is_imported(f)
                                 else (1, 0)))

    for f in findings:
        para = paras.get(f.para_id)
        if para is None:
            out.append(_status(f, "rejected_no_anchor"))
            log.debug("%s: unknown paragraph %s", f.finding_id, f.para_id)
            continue

        # 1 — anchor the quote, tolerating a model that re-typed the
        # manuscript's curly punctuation as straight (see anchor_offset)
        s = anchor_offset(para.text, f.original_text, f.occurrence)
        if s == -1:
            out.append(_status(f, "rejected_no_anchor"))
            log.debug("%s: quote not found in %s (occurrence %d): %r",
                      f.finding_id, f.para_id, f.occurrence, f.original_text[:80])
            continue

        if f.error_type in format_types:
            # The text does not change at all, so there is no diff to shrink.
            # corrected_text is the span to mark — a title set in roman that
            # should be italic — and it has to be inside the quoted sentence.
            off = f.original_text.find(f.corrected_text)
            if not f.corrected_text or off == -1:
                out.append(_status(f, "rejected_no_anchor"))
                log.debug("%s: %r is not inside the quoted sentence",
                          f.finding_id, f.corrected_text[:60])
                continue
            start = s + off
            end = start + len(f.corrected_text)
            # The span comes from the real paragraph, not the model's quote, so
            # a fold-recovered match still marks the manuscript's own characters.
            marked = para.text[start:end]
            # Formatting claims its own register: marking a title italic and
            # correcting a typo inside it are not in conflict, but marking the
            # same title twice is.
            spans = formatted_spans.setdefault(f.para_id, [])
            if any(_overlaps(start, end, a, b) for a, b in spans):
                out.append(_status(f, "rejected_duplicate"))
                continue
            if CONFIDENCE_RANK[f.confidence] < threshold:
                out.append(_status(f, "skipped_low_confidence", Anchor(
                    start=start, end=end, delete_text=marked,
                    insert_text=marked)))
                continue
            spans.append((start, end))
            out.append(dataclasses.replace(
                f, status="validated", format=format_types[f.error_type],
                anchor=Anchor(start=start, end=end,
                              delete_text=marked, insert_text=marked)))
            continue

        if f.error_type in query_types or f.force_query:
            # The whole quoted sentence is the anchor: a question is about a
            # passage, not about the characters someone would have changed.
            # force_query is a downgrade — the overseer-verifier's, or the
            # meaning gate's — a finding neither would keep as a change but
            # neither rejected, sent to the margin instead.
            # The error type is part of the key so two *different* questions
            # about one sentence — a term-consistency query and a speaker-change
            # query, say — both survive; only the same question asked twice is a
            # duplicate. A downgrade carries a specific correction that was
            # withheld, so the correction is part of what makes it a distinct
            # question: two different fixes for one sentence, both held back,
            # are two things to tell the author, and keying them the same way
            # would silently drop the second — the one outcome a downgrade must
            # never produce.
            key = (f.para_id, s, "query", f.error_type,
                   f.corrected_text if f.force_query else "")
            if key in seen:
                out.append(_status(f, "rejected_duplicate"))
                continue
            seen.add(key)
            end = s + len(f.original_text)
            out.append(_status(f, "query", Anchor(
                start=s, end=end,
                delete_text=para.text[s:end], insert_text="")))
            continue

        # 2 — shrink to the minimal edit
        pre, deleted, inserted = shrink(f.original_text, f.corrected_text)
        if not deleted and not inserted:
            out.append(_status(f, "rejected_noop"))
            continue
        start, end = s + pre, s + pre + len(deleted)
        # delete_text is the manuscript's own characters at that span, not the
        # model's quote: when the anchor matched only after folding punctuation
        # the two differ (straight vs curly), and the reassembler re-asserts the
        # slice before it edits. The fold is length-preserving, so this is the
        # same characters in the manuscript's form. (For an exact match it is
        # byte-identical to `deleted`, so nothing changes on that path.)
        anchor = Anchor(start=start, end=end,
                        delete_text=para.text[start:end], insert_text=inserted)

        # 2.4 — a quote that cuts a word in half at the edge the edit
        # touches anchored INSIDE a longer word; applied, it would splice
        # that word ("our bond born" over "our bond borne" -> "was was born",
        # Georgis 2026-09-04). Refused with its own status so the report
        # names the cause rather than filing it as a landed edit.
        if anchors_midword(para.text, s, f.original_text, pre, deleted):
            out.append(_status(f, "rejected_anchor_midword", anchor))
            log.info("%s (%s): refused — the quote cuts a word at the edge "
                     "the edit touches: %r in %r", f.finding_id, f.error_type,
                     f.original_text[-40:], para.text[max(0, s - 10):end + 12])
            continue

        # 2.42 — a fix that ships a doubled word the paragraph did not have
        # ("and and,") is itself the artifact, whatever else it repairs.
        composed = para.text[:start] + inserted + para.text[end:]
        dup = introduces_doubled_word(para.text, composed)
        if dup is not None:
            out.append(_status(f, "rejected_doubled_word_in_fix", anchor))
            log.info("%s (%s): refused — the correction doubles %r: %r",
                     f.finding_id, f.error_type, dup, inserted[:60])
            continue

        # 2.43 — a fix whose words repeat the word immediately before or after
        # its span ("Kalamata." inserted before "Kalamata, the capital").
        nb = duplicates_neighbour(para.text, start, end, inserted)
        if nb is not None:
            out.append(_status(f, "rejected_duplicates_neighbour", anchor))
            log.info("%s (%s): refused — the correction repeats its neighbour "
                     "%r: %r", f.finding_id, f.error_type, nb, inserted[:60])
            continue

        # 2.44 — house style is not a lane's to undo. The configured sweeps
        # are run over the paragraph AS IT WOULD READ; a sweep that fires
        # inside the changed span and did not fire there before is the row
        # writing a form the house removes ("2:30 AM" -> "2:30 am").
        if sweep_guard is not None and getattr(sweep_guard, "sweeps", None):
            key = sweep_guard.refires(para.text, (start, end), composed,
                                      (start, start + len(inserted)))
            if key:
                out.append(_status(f, f"rejected_undoes_house_style:{key}",
                                   anchor))
                log.info("%s (%s): refused — %s would re-fire on %r",
                         f.finding_id, f.error_type, key, inserted[:60])
                continue

        # 2.45 — number labels. A number_style edit that lands on a LABEL — a
        # "Mindset Number 23", a "1)" list marker, a heading — is refused
        # outright: the numeral is part of a name, not a count in a sentence,
        # and spelling it out renames the section. Its own status, so the
        # report says why rather than filing it with the anchor failures.
        if f.error_type == "number_style" and _is_number_label(para, start, end):
            out.append(_status(f, "rejected_policy", anchor))
            log.info("%s (number_style): refused — %r is a label, not a "
                     "number in a sentence", f.finding_id,
                     para.text[max(0, start - 20):end + 10])
            continue
        # 2.46 — sub-dollar amounts stay in words (Chicago 9.20): "a dime",
        # "5 cents", "90 cents" are never restyled to "$0.NN".
        if f.error_type in _MONEY_STYLE_TYPES and _SUB_DOLLAR_RE.search(inserted) \
                and not _SUB_DOLLAR_RE.search(deleted):
            out.append(_status(f, "rejected_policy", anchor))
            log.info("%s (%s): refused — a sub-dollar amount stays in words, "
                     "not %r", f.finding_id, f.error_type, inserted[:40])
            continue

        # 2.5 — overreach guard. A proofreading fix is minimal; an edit that
        # re-types a large span or adds a lot of new text is the model
        # fabricating content or rewriting a passage, and is rejected before it
        # can reach the manuscript. Checked ahead of the confidence gate so an
        # over-large edit is never merely "skipped, low confidence" — it is
        # refused outright, and counted.
        if (edit_guard is not None and edit_guard.enabled
                and f.error_type not in guard_exempt
                and _oversteps(deleted, inserted, edit_guard)):
            out.append(_status(f, "rejected_oversized", anchor))
            log.warning("%s (%s): rejected as an over-large edit — deletes %d, "
                        "inserts %d chars, not a minimal proofreading fix",
                        f.finding_id, f.error_type, len(deleted), len(inserted))
            continue

        # 2.6 — compound-merge guard. An edit whose whole effect is deleting
        # one space between two words ("blood work" -> "bloodwork") is a
        # compound-styling judgment, not a proofreading fix — the consistency
        # scan exists precisely to ASK about open-versus-closed compounds, and
        # which form a book uses is the author's to settle. Demoted to a
        # margin query, never silently applied. (The Purpura head proofreader
        # reversed exactly this merge.)
        if (not inserted and deleted in (" ", " ")
                and start > 0 and end < len(para.text)
                and para.text[start - 1].isalpha()
                and para.text[end].isalpha()
                and not _dictionary_closes(para.text, start, end)):
            key = (f.para_id, s, "query", f.error_type, f.corrected_text)
            if key in seen:
                out.append(_status(f, "rejected_duplicate", anchor))
                continue
            seen.add(key)
            qend = s + len(f.original_text)
            out.append(_status(
                dataclasses.replace(f, force_query=True), "query",
                Anchor(start=s, end=qend, delete_text=para.text[s:qend],
                       insert_text="")))
            log.info("%s (%s): space-deletion merge demoted to a query — "
                     "closing an open compound is the author's call",
                     f.finding_id, f.error_type)
            continue

        # 3 — confidence gate (anchored first, so the report stays informative)
        if CONFIDENCE_RANK[f.confidence] < threshold:
            out.append(_status(f, "skipped_low_confidence", anchor))
            continue

        # 4 — exact duplicate. Asked before overlap on purpose: an identical
        # edit necessarily overlaps the accepted copy of itself, so the other
        # order reports every duplicate as an overlap and this status never
        # fires.
        key = (f.para_id, start, end, inserted)
        if key in seen:
            out.append(_status(f, "rejected_duplicate", anchor))
            continue

        # 5 — overlap with already-accepted edits in this paragraph. Claims and
        # conflicts use the WHOLE shrunk span deliberately, even though the
        # emission below may split it: two findings quoting one span with
        # different corrections are alternative versions of that span, and
        # letting their minimal regions interleave just because they touch
        # different characters composes a garbled third version no one wrote.
        spans = accepted_spans.setdefault(f.para_id, [])
        if any(_overlaps(start, end, a, b) for a, b in spans):
            out.append(_status(f, "rejected_overlap", anchor))
            continue

        seen.add(key)
        spans.append((start, end))

        # 5.5 — emit, split into minimal regions. A row that quotes a wide span
        # but touches it in a few distinct places becomes one tracked change
        # PER touch, so the author reviews "w"->"W", never a block replace
        # whose actual change is impossible to see. Claiming (above) and the
        # guard both ruled on the whole span; this is display granularity only.
        for i, (off, r_del, r_ins) in enumerate(
                minimal_regions(f.original_text, f.corrected_text)):
            r_start = s + off
            r_end = r_start + len(r_del)
            sub = f if i == 0 else dataclasses.replace(
                f, finding_id=f"{f.finding_id}{chr(ord('b') + i - 1)}")
            out.append(_status(sub, "validated",
                               Anchor(start=r_start, end=r_end,
                                      delete_text=para.text[r_start:r_end],
                                      insert_text=r_ins)))

    n_ok = sum(1 for f in out if f.status == "validated")
    log.info("Validated %d/%d findings (%s)", n_ok, len(out),
             _tally(out))
    return out


def _dictionary_closes(text: str, start: int, end: int) -> bool:
    """Whether deleting text[start:end] (one space) joins two words into a
    word the dictionary already carries ("wash cloth" -> "washcloth"). A
    closed compound Merriam-Webster lists is a spelling correction, applied;
    only a join the dictionary does not know is the author's compound-
    styling call (Georgis head-to-head, 2026-09-04). No dictionary = False,
    the conservative answer."""
    from .spellscan import dictionary_knows
    a = start
    while a > 0 and text[a - 1].isalpha():
        a -= 1
    b = end
    while b < len(text) and text[b].isalpha():
        b += 1
    joined = text[a:start] + text[end:b]
    if len(joined) < 5:
        return False
    return bool(dictionary_knows(joined))


def to_query(f: Finding, doc: DocumentModel) -> Finding:
    """Re-cut an already-validated edit as a margin question, in place.

    The meaning gate needs to withdraw a change AFTER the run has been
    arbitrated, and it must do so without re-running the validator: a second
    arbitration re-opens every span, which can promote an edit that was set
    aside as overlapping and let it evict a change the gate had just approved.
    So the gate withdraws its findings one at a time, through here — the span
    stays claimed, nothing else moves, and the only difference to the run is
    that this correction became a question. Same anchoring the query branch of
    `validate_findings` uses.

    A finding whose quote no longer anchors (it did when it validated, so this
    is belt and braces) falls back to the whole paragraph as its span. It has to
    get one. A query with no anchor is NOT "still reported, not applied" — both
    reassemblers place a margin comment from `Finding.anchor` and drop a query
    that has none, so an anchorless question survives in findings.json and in
    summary.md while being absent from the file the author opens, which is the
    one place summary.md promises it will be. The paragraph is the coarsest
    honest answer to "where", it is always attachable, and it still edits
    nothing — so the question reaches the reader and the safe direction holds.

    Only an unknown paragraph comes back with no anchor, because there is then
    nothing at all to hang a comment on; the reassemblers count that as
    `unplaced` rather than dropping it quietly."""
    para = index_paragraphs(doc).get(f.para_id)
    if para is None:
        log.warning("%s: unknown paragraph %s — withdrawn to the margin with "
                    "no anchor, so no comment can be written for it.",
                    f.finding_id, f.para_id)
        return dataclasses.replace(f, status="query", anchor=None,
                                   force_query=True, withheld=True)
    s = anchor_offset(para.text, f.original_text, f.occurrence)
    if s == -1:
        log.warning("%s: quote no longer anchors in %s — the question is hung "
                    "on the whole paragraph.", f.finding_id, f.para_id)
        s, end = 0, len(para.text)
    else:
        end = s + len(f.original_text)
    return dataclasses.replace(
        f, status="query", force_query=True, withheld=True,
        anchor=Anchor(start=s, end=end, delete_text=para.text[s:end],
                      insert_text=""))


def _oversteps(deleted: str, inserted: str, guard) -> bool:
    """Whether a minimal diff is too big to be a proofreading fix: it re-types a
    span wider than the size cap, or it adds more net new text than the growth
    cap. Pure deletions never trip the growth cap — removing text is not
    fabrication.

    The span is measured from the first changed character to the last, so a
    correction that rewrites a sentence end to end trips even when each individual
    touch is small (a run-on made grammatical scatters capitals and punctuation
    across the whole line). A COUPLED repair whose two touches sit far apart is
    kept out of this path a different way — the analyzer bundles only touches
    close together, and lets a wide pair travel as two separate findings the
    judge gates then rule on as one sentence — so this stays the blunt, safe
    rewrite-catcher it has always been."""
    if len(deleted) > guard.max_edit_chars or len(inserted) > guard.max_edit_chars:
        return True
    return len(inserted) - len(deleted) > guard.max_added_chars


def _overlaps(s1: int, e1: int, s2: int, e2: int) -> bool:
    """True if two edits conflict and cannot both apply to one paragraph.

    Two insertions conflict at the same point OR one character apart: two
    rows inserting at the same seam — a comma after "luck" from the ladder
    and from the fleet, ":00" from the time sweep and from the number audit —
    compose into ",," and "7:00 :00 AM" whether their points coincide or sit
    either side of the one space between them (Georgis, 2026-09-04). An
    insertion conflicts with
    a replacement or deletion when its point is the span's START or lies inside
    it — the half-open range [start, end) — because both then act at the same
    offset and compose order-dependently into duplicated or garbled text:
    inserting "to " at the start of a "could"->"to" replacement composes to
    "to to", and a comma inserted at the start of a " they were"->", he was"
    replacement composes to ",,". An insertion that merely abuts a span's END
    sits after it and composes cleanly ("colour"->"color" with a "," inserted at
    the close gives "color,"), so it does not conflict. Two non-empty spans
    conflict when their intervals intersect; spans that only touch end-to-start
    (a [3, 5] beside a [5, 8]) do not."""
    if s1 == e1 and s2 == e2:          # two insertions: same point or adjacent
        return abs(s1 - s2) <= 1
    if s1 == e1:                        # s1 an insertion into the s2..e2 span
        return s2 <= s1 < e2
    if s2 == e2:                        # s2 an insertion into the s1..e1 span
        return s1 <= s2 < e1
    return s1 < e2 and s2 < e1         # two spans: half-open interval overlap


def _status(f: Finding, status: str, anchor: Anchor | None = None) -> Finding:
    return dataclasses.replace(f, status=status, anchor=anchor)


def _tally(findings: list[Finding]) -> str:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))