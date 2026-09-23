"""Native Word output and independently checked fixed-workflow handoff.

Every build starts from the frozen source package (the accepted intake baseline
when incoming revisions were present). Net changes are tracked; rejecting them
reproduces that source text, including headers and tables.
"""
from __future__ import annotations

import copy
import dataclasses
import difflib
import hashlib
import itertools
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from docproof.utils.files import write_atomic
from galley.manifest import sha256_file


class FixedDocumentError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _save(path, value):
    write_atomic(Path(path), json.dumps(value, ensure_ascii=False, indent=2))


def paragraph_views(path, view="accept"):
    from docproof.reassembler import paragraph_view_text
    from docproof.utils.xml_helpers import DocxPackage, walk_package
    return {p.para_id: paragraph_view_text(p.element, view) for p in walk_package(DocxPackage(path))}


def _back_span(original, current, lo, hi, *, exact=False):
    """Map a current range to source. Formatting requires an unchanged range."""
    ranges = []
    for tag, i, j, a, b in difflib.SequenceMatcher(a=original, b=current, autojunk=False).get_opcodes():
        if a < hi and b > lo:
            if exact and tag != "equal":
                raise FixedDocumentError("A format proposal overlaps a text correction")
            ranges.append((i + max(0, lo - a), i + min(b, hi) - a) if tag == "equal" else (i, j))
    if not ranges:
        raise FixedDocumentError("Cannot anchor a final comment or formatting change to the original")
    return min(a for a, _ in ranges), max(b for _, b in ranges)


def _diffs(original, corrected):
    # Word-sized revisions preserve meaningful accept/reject units. Whitespace
    # and punctuation remain independent tokens, so no whole paragraph rewrite.
    pattern = r"\w+|[^\w\s]|\s+"
    left, right = list(re.finditer(pattern, original)), list(re.finditer(pattern, corrected))
    for tag, i, j, a, b in difflib.SequenceMatcher(
            a=[m.group() for m in left], b=[m.group() for m in right], autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        start = left[i].start() if i < len(left) else len(original)
        end = left[j - 1].end() if j > i else start
        replacement = "".join(m.group() for m in right[a:b])
        yield start, end, replacement


def _comments(pkg):
    from docproof.utils.xml_helpers import qn
    if not pkg.has("word/comments.xml"):
        return {}
    return {x.get(qn("w:id")): etree.tostring(x, method="c14n")
            for x in pkg.tree("word/comments.xml") if x.tag == qn("w:comment")}


def write_manuscripts(source, destination, accepted, questions=(), formats=()):
    from docproof.models import Anchor, DocumentModel, Finding, ParagraphRef
    from docproof.reassembler import apply_tracked_changes, _MARKS
    from docproof.cleancopy import write_clean_copy
    from docproof.utils.xml_helpers import DocxPackage, walk_package, paragraph_text, qn
    from galley.fixed_policy import configuration
    from galley.fixed_workflow import _locate

    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    pkg = DocxPackage(source)
    walked = list(walk_package(pkg))
    original = {p.para_id: paragraph_text(p.element) for p in walked}
    if set(original) != set(accepted):
        raise FixedDocumentError("The final manuscript added or omitted paragraph identities")
    original_comments = _comments(pkg)
    cfg = configuration()
    cfg.comments = False
    cfg.query_comments = True
    cfg.not_applied_comments = False
    paragraphs = tuple(ParagraphRef(p.para_id, p.part, p.location, original[p.para_id], "", True) for p in walked)
    doc = DocumentModel(str(source), paragraphs)
    findings, details = [], []
    for pid, before in original.items():
        for start, end, replacement in _diffs(before, accepted[pid]):
            fid = "fixed-" + _hash([pid, start, end, replacement])[:20]
            anchor = Anchor(start, end, before[start:end], replacement)
            f = Finding(fid, "fixed-final", pid, "proofreading", before[start:end], 1,
                        replacement, "Clear proofreading correction.", "high", "validated",
                        anchor=anchor, silent=True)
            findings.append(f)
            details.append({**dataclasses.asdict(f), "applied": True, "queried": False})
    format_keys = set()
    for row in formats:
        pid = row["para_id"]
        lo, hi = _back_span(original[pid], row["snapshot"], row["start"], row["end"], exact=True)
        if row["format"] not in _MARKS:
            raise FixedDocumentError("Unsupported proofreading format operation")
        key = (pid, lo, hi, row["format"])
        if key in format_keys:
            continue
        format_keys.add(key)
        f = Finding("format-" + _hash(key)[:20], "fixed-final", pid, "title_italics",
                    original[pid][lo:hi], 1, original[pid][lo:hi], row["reason"], "high", "validated",
                    anchor=Anchor(lo, hi, original[pid][lo:hi], original[pid][lo:hi]),
                    format=row["format"], silent=True)
        findings.append(f)
        details.append({**dataclasses.asdict(f), "applied": True, "queried": False})
    # Word keeps comments in the body only. A question about a running head,
    # footer or note paragraph is carried by the first body paragraph, saying
    # where it really points, rather than blocking the whole delivery.
    where = {p.para_id: (p.part, p.location) for p in walked}
    body_ids = [p.para_id for p in walked if p.location == "body" and p.part.endswith("document.xml")
                and original[p.para_id].strip()]
    for q in questions:
        pid = q["para_id"]
        if not q.get("missing_knowledge") or not q.get("question"):
            raise FixedDocumentError("A final author query lacks its required evidence")
        lo, hi = _locate(accepted[pid], q["quote"], q.get("occurrence", 1))
        text = q["question"]
        relocated = None
        part, location = where[pid]
        if location != "body" or not part.endswith("document.xml"):
            if not body_ids:
                raise FixedDocumentError("A final author question about a header, footer or note has no body paragraph to carry it")
            place = {"header": "the running head", "footer": "the footer", "footnote": "a footnote",
                     "endnote": "an endnote", "textbox": "a text box"}.get(location, location)
            if part.startswith("word/header"):
                place = "the running head"
            elif part.startswith("word/footer"):
                place = "the footer"
            text = f"About {place} \u201c{q['quote']}\u201d: {q['question']}"
            relocated, pid = pid, body_ids[0]
            lo, hi = 0, len(accepted[pid])
        start, end = _back_span(original[pid], accepted[pid], lo, hi)
        if start == end:
            # A question about inserted text needs a real source anchor.
            start, end = max(0, start - 1), min(len(original[pid]), end + 1)
        if start == end:
            raise FixedDocumentError("A final question cannot be anchored to an empty paragraph")
        quote = original[pid][start:end]
        occurrence = sum(original[pid].startswith(quote, offset) for offset in range(start)) + 1
        f = Finding(q["id"], "fixed-final", pid, "author_question", quote, occurrence,
                    original[pid][start:end], text, "high", "query",
                    anchor=Anchor(start, end, original[pid][start:end], original[pid][start:end]), force_query=True)
        findings.append(f)
        details.append({**dataclasses.asdict(f), "applied": False, "queried": True,
                        **({"relocated_from": relocated} if relocated else {})})
    stats = apply_tracked_changes(pkg, doc, findings, cfg)
    expected_edits = {f.finding_id for f in findings if f.status == "validated"}
    actual_edits = set(stats.applied) | set(stats.already_set)
    if expected_edits != actual_edits or stats.skipped or stats.unplaced:
        raise FixedDocumentError("Not every approved correction and query could be written")
    if set(stats.queried) != {q["id"] for q in questions}:
        raise FixedDocumentError("Not every final author question received a Word comment")
    for row in details:
        row["applied"] = row["finding_id"] in stats.applied
        if row["finding_id"] in stats.already_set:
            row["already_set"] = True
    if any(_comments(pkg).get(cid) != value for cid, value in original_comments.items()):
        raise FixedDocumentError("An original author comment was changed")
    tracked = destination / f"{source.stem} - Atmosphere Press Proofreader.docx"
    pkg.save(tracked)
    if paragraph_views(tracked, "reject") != original or paragraph_views(tracked) != accepted:
        raise FixedDocumentError("Tracked output does not reproduce the original and corrected manuscript views")
    clean = destination / f"{source.stem} - Clean.docx"
    write_clean_copy(tracked, clean)
    if paragraph_views(clean) != accepted:
        raise FixedDocumentError("Clean copy differs from the accepted tracked manuscript")
    # Non-manuscript package members (images, styles, numbering, embedded files)
    # cannot change during proofreading. Only touched story/comment metadata may.
    from zipfile import ZipFile
    with ZipFile(source) as a, ZipFile(tracked) as b:
        allowed = {p.part for p in walked} | {"word/comments.xml", "[Content_Types].xml", "word/_rels/document.xml.rels"}
        for name in a.namelist():
            if name not in allowed and (name not in b.namelist() or a.read(name) != b.read(name)):
                raise FixedDocumentError(f"Proofreading changed a protected package member: {name}")
    return tracked, clean, details


# Recipe versions whose stages carry deterministic local-check evidence, and
# the packet stage each reading stage must certify. A version missing here
# would silently skip the poetry and local-evidence checks.
_LOCAL_EVIDENCE_VERSIONS = {
    "fixed-proofreading-v2": {"typed": "initial", "ensemble_sweep": "completion"},
    "fixed-proofreading-v3": {"typed": "initial", "ensemble_sweep": "completion",
                              "fable": "completion_fable", "astra": "completion_astra"},
    # v4 changes what the readers are sent (stage-specific policy, shared
    # window context, focused-site and metadata diet, no comma_boundary
    # generator); its local evidence is v3's.
    "fixed-proofreading-v4": {"typed": "initial", "ensemble_sweep": "completion",
                              "fable": "completion_fable", "astra": "completion_astra"},
    # v5: silent normalization at intake (fixed-intake-v3) and per-window
    # site labels for screening and number reads; local evidence is v3's.
    "fixed-proofreading-v5": {"typed": "initial", "ensemble_sweep": "completion",
                              "fable": "completion_fable", "astra": "completion_astra"},
    # v6: verse takes house mechanics — Sonnet and Luna typed passes, the
    # deterministic verse sweep packet (stage "verse"), the number stage and
    # the checks — never a change to its structure; local evidence is v3's.
    "fixed-proofreading-v6": {"typed": "initial", "ensemble_sweep": "completion",
                              "fable": "completion_fable", "astra": "completion_astra"},
    # v7: a second Astra reading closes the book. It is the only needs_human
    # gate (more than 25 core mechanical corrections still found, or a verified
    # publication blocker), it carries the press-method final audit, and its
    # own propagation pass is the last local evidence.
    "fixed-proofreading-v7": {"typed": "initial", "ensemble_sweep": "completion",
                              "fable": "completion_fable", "astra": "completion_astra",
                              "final_astra": "completion_final_astra"},
    # v8: Opus 5.5 replaces Opus 5 and Fable. The first final reading is
    # `opus_read`; the opening read and the Astra gate carry no local packet.
    "fixed-proofreading-v8": {"typed": "initial", "ensemble_sweep": "completion",
                              "opus_read": "completion_opus_read", "astra": "completion_astra",
                              "final_astra": "completion_final_astra"},
    # v9: v8's stages with GPT-6 Luna and Sol in every Luna and Sol role.
    "fixed-proofreading-v9": {"typed": "initial", "ensemble_sweep": "completion",
                              "opus_read": "completion_opus_read", "astra": "completion_astra",
                              "final_astra": "completion_final_astra"},
}
# From v6 a book with any poetry carries a verse sweep packet on its typed
# stage, and a poetry-only book runs the number stage and the checks.
_VERSE_EVIDENCE_VERSIONS = {"fixed-proofreading-v6", "fixed-proofreading-v7", "fixed-proofreading-v8",
                            "fixed-proofreading-v9"}
# The prose stage list, by recipe version, and which stage carries the
# press-method final audit (the last stage: its accepted text must be the
# delivered text).
_PROSE_STAGES = ["poetry", "story_sheet", "typed", "numbers", "broken_repair", "checks",
                 "ensemble_sweep", "continuity", "fable", "astra"]
_FINAL_GATE_VERSIONS = {"fixed-proofreading-v7", "fixed-proofreading-v8", "fixed-proofreading-v9"}
# v8 opens with Opus 5.5's reading of the original, renames the first final
# reading and closes with Opus 5.5's gate on Astra's changes.
_OPUS_RECIPE_VERSIONS = {"fixed-proofreading-v8", "fixed-proofreading-v9"}
_OPUS_PROSE_STAGES = ["poetry", "story_sheet", "opening_read", "typed", "numbers", "broken_repair", "checks",
                      "ensemble_sweep", "continuity", "opus_read", "astra", "final_astra", "astra_gate"]


def _prose_stages(version):
    if version in _OPUS_RECIPE_VERSIONS:
        return list(_OPUS_PROSE_STAGES)
    return _PROSE_STAGES + (["final_astra"] if version in _FINAL_GATE_VERSIONS else [])


def _audit_stage(version):
    if version in _OPUS_RECIPE_VERSIONS:
        return "astra_gate"
    return "final_astra" if version in _FINAL_GATE_VERSIONS else "astra"


def package_outcome(result) -> tuple[str, str]:
    """The delivery outcome and its reason for a completed fixed result.

    The outcome is the editorial verdict alone: under the final gate that is
    the second Astra reading's fixed rule, and nothing else sends a book to a
    person (Quinton, 2026-09-16: "the only thing that should skip a book is
    the Astra review"). Skipped model reviews are an operational fact: they
    are counted in the reason and in the report, never turned into a verdict.
    A subscription limit no longer produces skips at all; the lane pauses."""
    skipped = len(result.get("skipped_reads") or [])
    outcome = "needs_human" if result["editorial_verdict"] == "needs_human" else "done"
    review = result.get("final_review")
    if isinstance(review, dict):
        reason = review["reason"]
    else:
        reason = "Fixed proofreading complete; every required reading and output check passed."
    if skipped:
        reason += (f" {skipped} model review(s) were unavailable and skipped; their unverified suggestions "
                   "were discarded and the skipped reads are recorded in the review evidence.")
    return outcome, reason


def _verify_result(result, directory):
    if result.get("execution_mode") != "fixed" or result.get("status") != "completed":
        raise FixedDocumentError("The fixed workflow has not completed")
    if _hash({k: v for k, v in result.items() if k not in {"usage", "result_sha256"}}) != result.get("result_sha256"):
        raise FixedDocumentError("The fixed result's content no longer matches its receipt")
    from galley.fixed_skips import validate_skip
    skipped = []
    for marker in sorted((Path(directory) / "calls" / "calls").glob("*/skipped.json")):
        audit = validate_skip(marker.parent)
        skipped.append({k: audit[k] for k in ("status", "request_sha256", "stage", "model", "reason")})
    if (sorted(skipped, key=_json) != sorted(result.get("skipped_reads", []), key=_json)
            or skipped and result.get("review_complete") is not False):
        raise FixedDocumentError("The result misstates skipped model reviews or reading completeness")
    source = Path(result["source"])
    if "intake" in result["identity"]:
        from galley.fixed_intake import validate_intake
        validate_intake(directory, result["identity"]["intake"], source)
    if sha256_file(source) != result["identity"]["source_sha256"]:
        raise FixedDocumentError("The original manuscript changed after review")
    if paragraph_views(source) != result["original"] or set(result["original"]) != set(result["accepted"]):
        raise FixedDocumentError("The fixed result does not preserve the original paragraph identities and text")
    if result.get("editorial_verdict") not in {"ready", "needs_human"}:
        raise FixedDocumentError("The fixed result lacks an editorial verdict")
    marker = json.loads((Path(directory) / "workflow.json").read_text())
    if (marker.get("identity") != result["identity"] or marker.get("execution_mode") != "fixed"
            or marker.get("status") != "completed" or marker.get("result_sha256") != result["result_sha256"]):
        raise FixedDocumentError("The fixed workflow checkpoint does not match its completed result")
    stages = result["stages"]
    verse_version = result["identity"].get("version") in _VERSE_EVIDENCE_VERSIONS
    expected = ((["poetry", "typed", "numbers", "checks", "poetry_complete"] if verse_version
                 else ["poetry", "typed", "poetry_complete"]) if result["poetry_only"] else
                _prose_stages(result["identity"].get("version")))
    names = [s["stage"] for s in stages]
    # A completed run may be extended by receipted passes that put its final
    # readers' dropped questions to Astra's review (galley.fixed_reinstate).
    extra = names[len(expected):]
    if (names[:len(expected)] != expected or
            extra != [f"walkthrough_questions{'' if i == 0 else f'_{i + 1}'}" for i in range(len(extra))]):
        raise FixedDocumentError("A required fixed proofreading stage is missing or out of order")
    protected_poetry = set()
    for stage in stages:
        path = Path(stage["path"]).resolve()
        if not path.is_relative_to(Path(directory).resolve() / "stages"):
            raise FixedDocumentError("A fixed stage's reading evidence is outside its workspace")
        payload = json.loads(path.read_text())
        if _hash(payload) != stage["sha256"]:
            raise FixedDocumentError("A fixed stage's reading evidence changed")
        version = result["identity"].get("version")
        if stage["stage"] == "poetry" and version in _LOCAL_EVIDENCE_VERSIONS:
            poetry_ids = payload.get("evidence", {}).get("poetry_ids")
            if (not isinstance(poetry_ids, list) or any(pid not in result["original"] for pid in poetry_ids)
                    or len(poetry_ids) != len(set(poetry_ids))):
                raise FixedDocumentError("The fixed result lacks valid poetry protection evidence")
            protected_poetry = set(poetry_ids)
            if result["poetry_only"] != (protected_poetry == set(result["original"])):
                raise FixedDocumentError("The fixed result disagrees with its poetry classification")
        if version in _VERSE_EVIDENCE_VERSIONS and stage["stage"] == "typed" and protected_poetry:
            from galley.fixed_local import validate_local_evidence
            verse = payload.get("evidence", {}).get("verse_local")
            if not isinstance(verse, dict) or verse.get("stage") != "verse":
                raise FixedDocumentError("Required verse sweep evidence is missing")
            packet = validate_local_evidence(verse, Path(directory) / "local", result["identity"])
            request = packet["request"]
            reviewed = {p["para_id"]: p["text"] for p in request["paragraphs"]}
            if (request.get("stage") != "verse" or request.get("verse_ids") != sorted(protected_poetry)
                    or reviewed != {pid: result["original"][pid] for pid in protected_poetry}):
                raise FixedDocumentError("Verse sweep evidence does not cover the classified poetry")
        if (version in _LOCAL_EVIDENCE_VERSIONS and not result["poetry_only"]
                and stage["stage"] in _LOCAL_EVIDENCE_VERSIONS[version]):
            from galley.fixed_local import validate_local_evidence
            local = payload.get("evidence", {}).get("local")
            expected_local_stage = _LOCAL_EVIDENCE_VERSIONS[version][stage["stage"]]
            if not isinstance(local, dict) or local.get("stage") != expected_local_stage:
                raise FixedDocumentError("Required deterministic proofreading evidence is missing")
            packet = validate_local_evidence(local, Path(directory) / "local", result["identity"])
            request = packet["request"]
            initial = expected_local_stage == "initial"
            original_prose = {pid: text for pid, text in result["original"].items() if pid not in protected_poetry}
            reviewed = {p["para_id"]: p["text"] for p in request["paragraphs"]}
            if (request.get("stage") != expected_local_stage
                    or request.get("completion", False) is not (not initial)
                    or set(reviewed) != set(original_prose)
                    or request.get("excluded_poetry_ids") != sorted(protected_poetry)
                    or (initial and reviewed != original_prose)
                    or (not initial and request.get("original") != result["original"])):
                raise FixedDocumentError("Deterministic proofreading evidence does not cover its assigned stage and source")
        if result["identity"].get("press_prompt_sha256") and stage["stage"] == _audit_stage(version):
            audit = payload.get("evidence", {}).get("press_audit", {})
            expected_prose = set(result["original"]) - protected_poetry
            if (audit.get("accepted_sha256") != _hash(result["accepted"])
                    or set(audit.get("paragraph_ids", [])) != expected_prose
                    or not isinstance(audit.get("raw_signal_counts"), dict)):
                raise FixedDocumentError("The press-method final scan lacks current-text coverage")
    last_stage = json.loads(Path(stages[-1]["path"]).read_text())
    if last_stage.get("accepted_sha256") != _hash(result["accepted"]):
        raise FixedDocumentError("The last reviewed manuscript differs from the final accepted text")
    if result["identity"].get("version") in _FINAL_GATE_VERSIONS and not result["poetry_only"]:
        gate = json.loads((Path(directory) / "stages" / "final_astra.json").read_text())
        recorded = gate.get("evidence", {}).get("final_review")
        review = result.get("final_review")
        if (not isinstance(review, dict) or review != recorded
                or review.get("verdict") != result["editorial_verdict"]
                or (review["verdict"] == "needs_human") is not (
                    review.get("core_mechanical_errors", 0) > review.get("ceiling", 0)
                    or bool(review.get("publication_blockers")))):
            raise FixedDocumentError("The editorial verdict does not follow from the second Astra reading's recorded evidence")
    return source


def _report(result, details, receipt=None):
    edits = [x for x in details if x["applied"]]
    paragraphs = len({x["para_id"] for x in edits})
    scope = "House mechanics only, never structure (poetry)" if result["poetry_only"] else "Clear proofreading errors only"
    labels = {pid: f"Paragraph {index}" for index, pid in enumerate(paragraph_views(result["source"]), 1)}
    stage_labels = {"poetry": "Poetry classification", "story_sheet": "Story Sheet",
        "typed": "Proofreading detectors", "numbers": "Number style review",
        "broken_repair": "Broken sentence repair", "checks": "Meaning and correction checks",
        "opening_read": "Opening Opus reading of the original manuscript",
        "ensemble_sweep": "Opus and Sol complete readings", "continuity": "Whole-book continuity reading",
        "fable": "Fable final reading and comment review",
        "opus_read": "Opus final reading and comment review",
        "astra": "Astra final reading and comment review",
        "final_astra": "Second Astra reading: remaining errors, publication blockers, verdict",
        "astra_gate": "Opus meaning and correction gate on Astra's changes",
        "poetry_complete": "Verse mechanics proofread complete",
        "walkthrough_questions": "Final readers' questions put to Astra's review"}
    lines = ["# Galley proofreading report", "", f"Scope: {scope}.", "",
             f"{len(edits)} tracked corrections across {paragraphs} paragraphs; {len(result['questions'])} author questions.", "",
             "## Corrections", ""]
    for x in edits:
        change = ("Set in italics" if x.get("format") == "italic" else json.dumps(x['corrected_text'], ensure_ascii=False))
        lines += [f"- {labels[x['para_id']]}: {json.dumps(x['original_text'], ensure_ascii=False)} → {change}"]
    lines += ["", "## Author questions", ""]
    carried = {x["finding_id"]: x for x in details if x.get("relocated_from")}
    lines += [f"- {labels[q['para_id']]}: {q['question']}"
              + (f" (about {labels.get(carried[q['id']]['relocated_from'], 'a header, footer or note paragraph')}; "
                 f"the comment sits on {labels[carried[q['id']]['para_id']]} because Word keeps comments in the body)"
                 if q["id"] in carried else "")
              for q in result["questions"]] or ["None."]
    skipped = result.get("skipped_reads", [])
    lines += ["", "## Processing stages" if skipped else "## Completed reading stages", ""]
    if skipped:
        lines += [f"{len(skipped)} model reviews were unavailable and skipped. Unverified suggestions were discarded; review coverage is incomplete.", ""]
    lines += [f"- {stage_labels.get(s['stage'], stage_labels['walkthrough_questions'] + ' (pass ' + s['stage'].rsplit('_', 1)[-1] + ')' if s['stage'].startswith('walkthrough_questions') else s['stage'])}" for s in result["stages"]]
    review = result.get("final_review")
    if isinstance(review, dict):
        verdict = "Needs human proofreader" if review["verdict"] == "needs_human" else "Proofread complete"
        lines += ["", "## Second Astra reading and verdict", "", f"Verdict: {verdict}.", "", review["reason"], "",
                  f"- Core mechanical errors still found: {review['core_mechanical_errors']} (ceiling {review['ceiling']}).",
                  f"- Publication blockers: {len(review['publication_blockers'])}."]
        for blocker in review["publication_blockers"]:
            lines.append(f"  - {labels.get(blocker['para_id'], blocker['para_id'])}: {blocker['problem']} "
                         f"({json.dumps(blocker['quote'], ensure_ascii=False)})")
        if review.get("waived_blockers"):
            # Not a blocker — a placeholder to fill, a question to ask, a
            # correction to make — but somebody still has to do that, so each
            # is named with why it did not count rather than silently dropped.
            lines += ["", f"- Reported and not counted towards the verdict: "
                      f"{len(review['waived_blockers'])} item(s) a placeholder fill, a question for the "
                      f"author or a correction resolves."]
            for blocker in review["waived_blockers"]:
                lines.append(f"  - {labels.get(blocker['para_id'], blocker['para_id'])}: {blocker['problem']} "
                             f"({json.dumps(blocker['quote'], ensure_ascii=False)}) — {blocker.get('waived', '')}")
        if review.get("skipped_windows"):
            lines.append(f"- {review['skipped_windows']} reading window(s) were unavailable and are recorded as skipped.")
    rejected = [h for h in result["history"] if h.get("rejected_proposal")]
    if rejected:
        lines += ["", f"{len(rejected)} model suggestions were rejected because they failed proposal validation. "
                  "They produced no edits or author comments; "
                  "their original suggestions and reasons remain in the review evidence."]
    receipt = receipt or {}
    if result["identity"].get("intake") and (receipt.get("resolved_revision_elements") or not receipt):
        lines += ["", "Incoming tracked changes were accepted in a separate working baseline using Galley's intake policy. "
                  "The uploaded original is preserved with a verified receipt. Rejecting Galley's new corrections restores "
                  "that accepted baseline; it does not undo edits the manuscript arrived with."]
    normalization = receipt.get("normalization") or {}
    if any(normalization.get(k) for k in ("quotes", "spaces", "ellipses")):
        lines += ["", "## Silent normalization at intake", "",
                  f"House conventions were applied to the working baseline before reading, without revision markup: "
                  f"{normalization.get('quotes', 0)} straight quotation marks curled, "
                  f"{normalization.get('spaces', 0)} runs of spaces collapsed, "
                  f"{normalization.get('ellipses', 0)} ellipses set to the house form "
                  f"(… with a non-breaking space before it), across {normalization.get('paragraphs', 0)} paragraphs"
                  + (f"; {normalization['ambiguous_quotes']} marks were left straight as ambiguous"
                     if normalization.get("ambiguous_quotes") else "")
                  + ". These are not corrections and are not listed below; rejecting Galley's corrections "
                  "restores the normalized baseline."]
    joins = receipt.get("runover_joins") or []
    if joins:
        continuation_lines = sum(len(j["absorbed"]) for j in joins)
        lines += ["", "## Page-runover paragraphs joined at intake", "",
                  f"{len(joins)} paragraphs that the typeset export had split across page boundaries "
                  f"({continuation_lines} continuation lines) were rejoined in the working baseline before reading. "
                  "The delivered manuscript carries them as single paragraphs; this structural join is not a tracked "
                  "change, and rejecting Galley's corrections restores the joined baseline, not the split export.", ""]
        for j in joins:
            label = labels.get(j.get("baseline_para_id"), j.get("baseline_para_id"))
            seams = ", ".join(str(o) for o in j.get("seam_offsets", []))
            lines += [f"- {label}: joined {len(j['absorbed'])} continuation line(s) (seam at {seams})"]
    if result["identity"].get("press_prompt_sha256"):
        from collections import Counter
        stages = {s["stage"]: json.loads(Path(s["path"]).read_text())["evidence"] for s in result["stages"]}
        sheet = stages.get("story_sheet", {}).get("sheet", {})
        lines += ["", "## Story Sheet and style assumptions", "",
                  sheet.get("narration", "Story Sheet unavailable." if skipped else "Poetry: house mechanics only; prose style and tense passes do not apply.")]
        lines += ["- " + note for note in sheet.get("notes", [])]
        if sheet and not sheet.get("notes"):
            lines.append("No additional variant or register assumptions were recorded; no independent authority lookup is claimed.")
        lines += ["", "## Reading and verification counts", ""]
        applied = Counter(h["stage"] for h in result["history"] if h.get("applied"))
        # The opening read's findings are screened and applied with the typed
        # stage's; the gate applies nothing and only restores.
        applied["opening_read"] = sum(1 for h in result["history"]
                                      if h.get("applied") and h["applied"].get("origin") == "opening_read")
        for name, label in stage_labels.items():
            if name == "astra_gate" and name in stages:
                gate = stages[name].get("gate", {})
                restored = sum(len(v) for v in gate.get("rejected", {}).values())
                lines.append(f"- {label}: {gate.get('changed_paragraphs', 0)} changed paragraphs judged, "
                             f"{restored} returned to their pre-Astra text.")
            elif name in stages:
                lines.append(f"- {label}: {applied[name]} accepted proposals at that stage (later reviews may revise them).")
        for name in ("opening_read", "fable", "opus_read", "astra", "final_astra"):
            counts = Counter()
            for window in stages.get(name, {}).get("coverage", []):
                counts.update(window.get("focused_counts", {}))
            if counts:
                lines.append(f"- {name.replace('_', ' ').title()} acknowledged focused sites: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) + ".")
        audit = stages.get(_audit_stage(result["identity"].get("version")), {}).get("press_audit", {})
        if audit:
            lines += ["", "## Final scripted signals", "", audit["interpretation"]]
            lines += [f"- {key}: {value}" for key, value in sorted(audit["raw_signal_counts"].items())]
            lines += [f"- {key} candidates: {value}" for key, value in sorted(audit["focused_counts"].items())]
            profile = audit["tense_profile"]
            lines.append(f"- Observed narration profile: {profile['baseline']}, {profile['person']}; present-dominant share {profile['present_share']:.1%}. This is heuristic evidence, not intended tense.")
        lines += ["", "## Notes coverage and limits", ""]
        from docproof.utils.xml_helpers import DocxPackage, walk_package
        note_parts = Counter(p.part for p in walk_package(DocxPackage(result["source"]))
                             if p.part in {"word/footnotes.xml", "word/endnotes.xml"})
        lines.append("Real note paragraphs included in reading and accept/reject audits: " +
                     (", ".join(f"{part}: {count}" for part, count in sorted(note_parts.items())) if note_parts else "none present") + ".")
        lines += [("Some required reads were skipped; incomplete coverage is recorded in the review evidence. " if skipped else "All required paragraph reads completed. ") + "Poetry classification uses distributed samples with a whole-document fallback for mixed/uncertain books. Structure and citation indexes are partial reading aids; no external fact-check or exhaustive dictionary lookup is claimed.",
                  "Focused checks and local patterns are not a guarantee of finding every error. The final signal scan does not repeat LanguageTool or start another model pass.",
                  "Reject-all audit passed: original text restored exactly, including real notes. Accept-all and clean-copy text agree. Smart quotes and internal-space changes remain tracked."]
        dropped = [h for h in result["history"] if h.get("decision", {}).get("action") == "drop"]
        lines += ["", "## Deliberately left unchanged", "",
                  f"{len(dropped)} adjudicated proposals were dropped. Full anchored proposals and reasons are retained in the certified review evidence; dropped model or tool concerns are not author comments."]
    return "\n".join(lines) + "\n"


# Where each fixed-lane artifact goes. The author folder receives the redline
# alone; everything else — the report, the review evidence, the certificate,
# the verdict DocWatch reads, and the clean reading copy — is DocProof's own
# record and is filed in the Drive archive only (Quinton, 2026-09-16).
HANDOFF = "handoff"
ARCHIVE = "archive"
ARTIFACT_DESTINATIONS = {"tracked": HANDOFF, "report": ARCHIVE, "evidence": ARCHIVE,
                         "certificate": ARCHIVE, "outcome": ARCHIVE, "clean": ARCHIVE}
# Pre-split packages (four artifacts, all beside the book) are still validated
# and delivered as they were frozen; a release must never re-cut a delivery
# that is already half uploaded.
_LEGACY_ROLES = ("tracked", "report", "evidence", "certificate")
ARCHIVE_SUBDIR = "archive"


def artifact_destination(row):
    return row.get("destination", HANDOFF)


def intake_normalization(receipt):
    """The findings-envelope form of the intake receipt's silent normalization:
    what the Author Letter's preparation disclosure reads."""
    normalization = (receipt or {}).get("normalization") if isinstance(receipt, dict) else None
    if not isinstance(normalization, dict):
        return {"ran": False}
    counts = {k: int(normalization.get(k) or 0) for k in ("quotes", "spaces", "ellipses", "paragraphs")}
    return {"ran": any(counts[k] for k in ("quotes", "spaces", "ellipses")), **counts,
            "ellipsis_style": normalization.get("ellipsis_style", "nbsp"),
            "policy": normalization.get("policy", "")}


def package_result(driver, result):
    """Build once, freeze artifact hashes, and preserve exact package on resume."""
    from galley.state_machine import RunStateMachine
    from galley.verify import build_fingerprints
    from galley.fixed_calls import validate_fixed_call_evidence
    from app.watch.naming import (CLEAN_SUFFIX, OUTCOME_SUFFIX, pre_proofread_base,
                                  pre_proofread_name)
    directory = driver.workspace / "runs/fixed"
    package_path = driver.workspace / "runs/driver/package.json"
    if package_path.exists():
        package = json.loads(package_path.read_text())
        if package.get("packet_sha256") != result["result_sha256"]:
            raise FixedDocumentError("A different corrected manuscript is already packaged")
        if driver.handoff_dir:
            frozen = Path(driver.handoff_dir).resolve()
            allowed = {frozen, frozen / ARCHIVE_SUBDIR}
            if any(Path(row["path"]).resolve().parent not in allowed for row in package.get("artifacts", [])):
                raise FixedDocumentError("This proofread already has a frozen handoff directory; resume with its original --handoff setting")
        validate_delivery_package(package)
        return package
    source = _verify_result(result, directory)
    call_evidence = validate_fixed_call_evidence(directory / "calls", identity=result["identity"])
    run = driver.workspace / "runs/final"
    tracked, clean, details = write_manuscripts(source, run, result["accepted"], result["questions"], result["formats"])
    from galley.fixed_calls import _load
    receipt = (_load(directory / "intake/receipt.json") if "intake" in result["identity"] else None)
    findings = {"source": str(source), "source_sha256": result["identity"]["source_sha256"],
                "execution_mode": "fixed", "findings": details,
                # The silent intake normalization (quotes, spaces, ellipses) is
                # invisible in the redline; the Author Letter and decision log
                # read it from here so the count is stated somewhere a reviewer
                # looks (Wilder, 2026-09-17: 48 ellipses with no trace).
                "normalization": intake_normalization(receipt)}
    _save(run / "findings.json", findings)
    report = run / f"{source.stem} - Proofreading report.md"
    write_atomic(report, _report(result, details, receipt))
    evidence = run / f"{source.stem} - Review evidence.json"
    _save(evidence, result)
    outcome = run / f"{source.stem} - outcome.json"
    outcome_value, reason = package_outcome(result)
    review = result.get("final_review") if isinstance(result.get("final_review"), dict) else None
    from galley.outcome import hubspot_fields
    record = {"schema_version": 1, "outcome": outcome_value, "reason": reason,
              "set_by": "Galley fixed proofreading", "execution_mode": "fixed",
              "author_questions": len(result["questions"]),
              # DocWatch's own default values; the watcher substitutes its
              # configured ones when it applies the verdict.
              "hubspot": hubspot_fields(outcome_value)}
    if review:
        record["final_review"] = {k: review[k] for k in ("stage", "verdict", "core_mechanical_errors", "ceiling",
                                                          "skipped_windows")}
        record["final_review"]["publication_blockers"] = len(review["publication_blockers"])
        record["final_review"]["waived_blockers"] = len(review.get("waived_blockers", []))
    _save(outcome, record)
    _save(run / "outcome.json", json.loads(outcome.read_text()))
    certificate_path = run / "fixed-certificate.json"
    certificate = {"schema_version": 1, "execution_mode": "fixed", "delivery_ready": True,
                   "source_id": driver.source_id or driver.slug,
                   "source_sha256": result["identity"]["source_sha256"],
                   "packet_sha256": result["result_sha256"], "result_path": str(directory / "result.json"),
                   "result_file_sha256": sha256_file(directory / "result.json"),
                   "workflow_sha256": sha256_file(directory / "workflow.json"),
                   "call_evidence": call_evidence,
                   "tracked": {"path": str(tracked), "sha256": sha256_file(tracked)},
                   "clean": {"path": str(clean), "sha256": sha256_file(clean)},
                   "review": {"editorial_verdict": result["editorial_verdict"],
                              **({"final_review": {k: review[k] for k in ("verdict", "core_mechanical_errors", "ceiling")}}
                                 if review else {})},
                   "checks": ["complete stage evidence", "source identity", "reject-all original text",
                              "accept-all corrected text", "clean-copy equality", "comment placement", "protected package members"]}
    _save(certificate_path, certificate)
    out = Path(driver.handoff_dir) if driver.handoff_dir else driver.workspace / "handoff"
    archive_out = out / ARCHIVE_SUBDIR
    archive_out.mkdir(parents=True, exist_ok=True)
    artifacts = []
    base = pre_proofread_base(source.stem)
    # The fixed lane hands back a pre-proofread, not a finished stage: a person
    # reads the redline before the author does. The author folder therefore
    # receives the redline ALONE — "<surname> - Book One - Pre-Proofread.docx".
    # The report, the review evidence, the certificate, the verdict DocWatch
    # reads to move the HubSpot property, and the clean reading copy are the
    # press's record: they are filed in the Drive archive only (`handoff/
    # archive/` locally), under the same base.
    sources = [("tracked", tracked, pre_proofread_name(source.name)),
               ("report", report, f"{base} - proofreading report.md"),
               ("evidence", evidence, f"{base} - review evidence.json"),
               ("certificate", certificate_path, f"{base} - fixed certificate.json"),
               ("outcome", outcome, f"{base}{OUTCOME_SUFFIX}.json"),
               ("clean", clean, f"{base}{CLEAN_SUFFIX}.docx")]
    for role, path, name in sources:
        destination = ARTIFACT_DESTINATIONS[role]
        target = (out if destination == HANDOFF else archive_out) / name
        shutil.copyfile(path, target)
        artifacts.append({"role": role, "name": target.name, "path": str(target.resolve()),
                          "origin": str(path.resolve()), "sha256": sha256_file(target),
                          "destination": destination})
    fingerprints = build_fingerprints(run)
    package = {"schema_version": 1, "execution_mode": "fixed", "kind": "human_review" if outcome_value == "needs_human" else "proofread",
               "source_id": driver.source_id or driver.slug, "outcome": outcome_value, "reason": reason,
               # The archive folder the record is filed under (Proofing/<month>/<this>).
               "archive_name": base,
               "packet_sha256": result["result_sha256"], "build_sha256": fingerprints["build_sha256"],
               "run": str(run.resolve()), "certificate": str(certificate_path.resolve()),
               "certificate_sha256": sha256_file(certificate_path), "artifacts": artifacts}
    _save(package_path, package)
    _save(driver.workspace / "runs/driver/final-run.json",
          {"run": "runs/final", "source_sha256": result["identity"]["source_sha256"]})
    state = RunStateMachine.load(driver.workspace / "state.json")
    if not state.reached("certified"):
        state.advance("certified", by="Galley fixed proofreading", source_sha256=result["identity"]["source_sha256"],
                      config_sha256=_hash(result["identity"]["configuration"]), results_run="runs/final")
        state.save(driver.workspace / "state.json")
    return package


def validate_delivery_package(package):
    """Shared by initial delivery and the worker's interrupted-upload recovery."""
    from galley.verify import build_fingerprints
    from galley.fixed_calls import validate_fixed_call_evidence
    if package.get("execution_mode") != "fixed":
        raise FixedDocumentError("This is not a fixed-workflow package")
    run = Path(package["run"]).resolve()
    certificate_path = Path(package["certificate"]).resolve()
    if certificate_path != run / "fixed-certificate.json" or sha256_file(certificate_path) != package["certificate_sha256"]:
        raise FixedDocumentError("Fixed delivery certificate changed")
    certificate = json.loads(certificate_path.read_text())
    path = Path(certificate["result_path"]).resolve()
    directory = run.parent / "fixed"
    if path != directory / "result.json" or sha256_file(path) != certificate["result_file_sha256"]:
        raise FixedDocumentError("Fixed review result changed after certification")
    result = json.loads(path.read_text())
    _verify_result(result, directory)
    if sha256_file(directory / "workflow.json") != certificate.get("workflow_sha256"):
        raise FixedDocumentError("Fixed workflow checkpoint changed after certification")
    calls = validate_fixed_call_evidence(directory / "calls", identity=result["identity"])
    if calls != certificate.get("call_evidence"):
        raise FixedDocumentError("Fixed model-call evidence changed after certification")
    if (certificate.get("delivery_ready") is not True or package["packet_sha256"] != result["result_sha256"]
            or certificate["packet_sha256"] != result["result_sha256"]):
        raise FixedDocumentError("Fixed package belongs to another reviewed manuscript")
    expected_outcome, _ = package_outcome(result)
    if (package.get("source_id") != certificate.get("source_id")
            or certificate.get("review", {}).get("editorial_verdict") != result["editorial_verdict"]
            or package.get("outcome") != expected_outcome
            or package.get("kind") != ("human_review" if expected_outcome == "needs_human" else "proofread")):
        raise FixedDocumentError("Fixed delivery metadata contradicts its reviewed result")
    for key in ("tracked", "clean"):
        row = certificate[key]
        if Path(row["path"]).resolve().parent != run or sha256_file(row["path"]) != row["sha256"]:
            raise FixedDocumentError("The certified manuscript changed")
        if paragraph_views(row["path"]) != result["accepted"]:
            raise FixedDocumentError("The delivered manuscript differs from the reviewed text")
    if paragraph_views(certificate["tracked"]["path"], "reject") != result["original"]:
        raise FixedDocumentError("The tracked manuscript no longer reproduces the original")
    if build_fingerprints(run)["build_sha256"] != package["build_sha256"]:
        raise FixedDocumentError("Fixed final build changed")
    source = Path(result["source"])
    artifacts = package.get("artifacts", [])
    legacy = artifacts and all("destination" not in row for row in artifacts)
    # The redline goes to the author folder; the record goes to the archive
    # (see package_result). A pre-split package delivered its four files beside
    # the book and is validated as it was frozen.
    expected_origins = {"tracked": Path(certificate["tracked"]["path"]),
        "report": run / f"{source.stem} - Proofreading report.md", "evidence": run / f"{source.stem} - Review evidence.json",
        "certificate": certificate_path}
    if not legacy:
        expected_origins.update({"outcome": run / f"{source.stem} - outcome.json",
                                 "clean": Path(certificate["clean"]["path"])})
    if (len(artifacts) != len(expected_origins) or {x.get("role") for x in artifacts} != set(expected_origins)
            or len({x["path"] for x in artifacts}) != len(artifacts)
            or len({x["name"] for x in artifacts}) != len(artifacts)):
        raise FixedDocumentError("Fixed delivery lacks its manuscript and supporting artifacts")
    for row in artifacts:
        if (Path(row.get("origin", "")).resolve() != expected_origins[row["role"]].resolve()
                or Path(row["path"]).name != row["name"]
                or sha256_file(row["path"]) != row["sha256"]
                or sha256_file(row["origin"]) != row["sha256"]):
            raise FixedDocumentError("A fixed handoff artifact changed")
        if not legacy and artifact_destination(row) != ARTIFACT_DESTINATIONS[row["role"]]:
            raise FixedDocumentError("A fixed handoff artifact is routed to the wrong Drive folder")
    if not legacy:
        for row in artifacts:
            if row["role"] == "outcome":
                verdict = json.loads(Path(row["path"]).read_text())
                if verdict.get("outcome") != expected_outcome or verdict.get("reason") != package.get("reason"):
                    raise FixedDocumentError("The delivered verdict contradicts the package")
    return certificate
