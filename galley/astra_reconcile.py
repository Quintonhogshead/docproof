"""Apply Astra's exact plan once, then prove it by replaying from its source.

The original API review remains immutable. A separate application receipt is
not another editorial verdict: it proves only that the authorized operations
were applied, with no additional document or evidence changes.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import itertools
import os
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

from docproof.attribution import PROOFREADER_AUTHOR, PROOFREADER_INITIALS
from galley.astra_review import (AstraReviewError, MODEL, PACKET_FILE, RECEIPT_FILE,
                                REASONING_EFFORT, _atomic, _hash, _load, _node,
                                _now, _revision, _views, build_packet,
                                validate_review, finding_explanation_repairs)

RECONCILIATION_FILE = "astra-reconciliation.json"
SOURCE_FILE = "astra-evidence/source.docx"
AUTHOR = "Astra review"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _members(path):
    with zipfile.ZipFile(path) as z:
        if len(z.namelist()) != len(set(z.namelist())):
            raise AstraReviewError("Duplicate DOCX package members")
        return {n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()}


def _plan(review):
    """Every nontrivial decision needs an operation with the right meaning."""
    actions = review["actions"]

    def kinds(key, value):
        return {a["kind"] for a in actions if value in a[key]}

    for d in review["comment_decisions"]:
        actual = kinds("comment_ids", d["comment_id"])
        required = {
            "drop": {"remove_comment"},
            "replace_question": {"replace_comment"},
            "resolve_with_edit": {"remove_comment"},
            "retain_author_question": set(),
        }.get(d["action"])
        if required is None or not required <= actual:
            raise AstraReviewError("Comment decision has no executable matching disposition")
        if d["action"] == "resolve_with_edit" and not actual & {"edit_text", "revert_revision"}:
            raise AstraReviewError("Resolved comment has no concrete manuscript correction")
        if d["action"] == "retain_author_question" and actual & {"remove_comment", "replace_comment"}:
            raise AstraReviewError("Comment operations contradict Astra's retain decision")
        if {"remove_comment", "replace_comment"} <= actual:
            raise AstraReviewError("Astra both removes and replaces the same comment")
    for d in review["revision_review"]["exceptions"]:
        actual = kinds("revision_ids", d["revision_id"])
        required = {"revert": {"revert_revision"}, "repair": {"edit_text", "revert_revision"},
                    "author_query": {"add_author_query", "replace_comment"}}[d["action"]]
        if not actual & required:
            raise AstraReviewError("Revision exception has no executable matching disposition")
    for d in review["finding_review"]["exceptions"]:
        actual = kinds("finding_ids", d["finding_id"])
        required = {"edit": {"edit_text", "revert_revision"},
                    "author_query": {"add_author_query", "replace_comment"},
                    "internal_repair": {"internal_repair"}, "drop": set()}.get(d["action"])
        if required is None or (required and not actual & required):
            raise AstraReviewError("Finding exception has no executable matching disposition")
    for d in review.get("issue_decisions", []):
        actual = kinds("issue_ids", d["issue_id"])
        required = {"edit": {"edit_text", "revert_revision"},
                    "author_query": {"add_author_query", "replace_comment"}, "drop": set()}.get(d["action"])
        if required is None or (required and not actual & required):
            raise AstraReviewError("Verification issue has no executable matching disposition")


def _safe_span(p, start, end):
    """Existing tracked content must be explicitly reverted, never nested."""
    from docproof.reassembler import _text_spans
    from docproof.utils.xml_helpers import qn
    covered = [(t, s, e) for t, s, e in _text_spans(p) if s < end and e > start]
    if not covered:
        raise AstraReviewError("Astra edit has no supported text span")
    for el, _s, _e in covered:
        run = el.getparent()
        if run.tag != qn("w:r") or any(_revision(n) for n in run.iter()):
            raise AstraReviewError("Astra edit intersects tracked formatting or unsupported run content")
        parent = run.getparent()
        while parent is not None and parent is not p:
            if _revision(parent):
                raise AstraReviewError("Astra edit intersects an existing revision; explicitly revert it first")
            if parent.tag not in {qn("w:hyperlink")}:
                raise AstraReviewError("Astra edit intersects an unsupported text container")
            parent = parent.getparent()


def _revert(el):
    from docproof.utils.xml_helpers import qn
    parent = el.getparent()
    if parent is None:
        raise AstraReviewError("Overlapping Astra revision reversions")
    name = etree.QName(el).localname
    if name in {"ins", "del"} and any(_revision(c) for c in el.iterdescendants()):
        raise AstraReviewError("Nested tracked content requires internal repair")
    if name == "ins" and parent.tag not in {qn("w:rPr"), qn("w:pPr"), qn("w:trPr")}:
        parent.remove(el)
    elif name == "del" and parent.tag not in {qn("w:rPr"), qn("w:pPr"), qn("w:trPr")}:
        at = parent.index(el)
        for t in el.iter(qn("w:delText")):
            t.tag = qn("w:t")
        for t in el.iter(qn("w:delInstrText")):
            t.tag = qn("w:instrText")
        for child in list(el):
            parent.insert(at, child)
            at += 1
        parent.remove(el)
    elif name in {"rPrChange", "pPrChange"}:
        old = el.find(parent.tag)
        owner = parent.getparent()
        if old is None or owner is None or any(_revision(c) for c in old.iter()):
            raise AstraReviewError("Unsupported nested property reversion")
        owner.replace(parent, copy.deepcopy(old))
    else:
        raise AstraReviewError(f"Unsupported Astra revision reversion: {name}")


def _replay(source, output, frozen, review, date):
    """Deterministic surgery. Any unsupported/ambiguous step aborts in memory."""
    from docproof.models import Anchor
    from docproof.reassembler import _Comments, apply_replacement
    from docproof.utils.xml_helpers import DocxPackage, qn, set_text, walk_package
    _plan(review)
    finding_explanation_repairs(review, frozen)
    pkg = DocxPackage(source)
    paras = {p.para_id: p for p in walk_package(pkg)}
    source_text = {pid: _views(p.element)[0] for pid, p in paras.items()}
    revision_rows = {r["id"]: r for r in frozen["revisions"]}
    revision_elements = {}
    for rid in {r for a in review["actions"] if a["kind"] == "revert_revision" for r in a["revision_ids"]}:
        row = revision_rows[rid]
        root = pkg.tree(row["part"])
        namespaces = {k: v for k, v in root.nsmap.items() if k}
        matches = root.xpath(row["path"], namespaces=namespaces)
        if len(matches) != 1 or _node(matches[0]) != row["data"]:
            raise AstraReviewError("Reviewed revision no longer matches its immutable source")
        revision_elements[rid] = matches[0]
    selected = set(revision_elements.values())
    if any(any(p in selected for p in el.iterancestors()) for el in selected):
        raise AstraReviewError("Nested revision reversions require internal repair")
    seen = set()
    for action in review["actions"]:
        if action["kind"] != "revert_revision":
            continue
        for rid in action["revision_ids"]:
            if rid in seen:
                raise AstraReviewError("Astra reverts the same revision more than once")
            _revert(revision_elements[rid])
            pkg.mark_modified(revision_rows[rid]["part"])
            seen.add(rid)

    existing = [int(el.get(qn("w:id"))) for name in pkg.names()
                if name.startswith("word/") and name.endswith(".xml")
                for el in pkg.tree(name).iter() if _revision(el) and (el.get(qn("w:id")) or "").isdigit()]
    ids = itertools.count(max(existing, default=-1) + 1)
    edits = {}
    for action in review["actions"]:
        if action["kind"] != "edit_text":
            continue
        p = paras[action["para_id"]].element
        text = _views(p)[1]
        quote = action["quote"]
        if not quote or text.count(quote) != 1:
            raise AstraReviewError("Astra edit quote is ambiguous after revision reconciliation")
        start, end = text.index(quote), text.index(quote) + len(quote)
        edits.setdefault(action["para_id"], []).append((start, end, action))
    for pid, rows in edits.items():
        rows.sort(key=lambda r: r[0], reverse=True)
        for left, right in zip(rows, rows[1:]):
            if right[1] > left[0]:
                raise AstraReviewError("Overlapping Astra text actions require internal repair")
        p = paras[pid].element
        for start, end, action in rows:
            before = _views(p)[1]
            if before[start:end] != action["quote"]:
                raise AstraReviewError("Astra text anchor changed during reconciliation")
            _safe_span(p, start, end)
            apply_replacement(p, Anchor(start, end, action["quote"], action["replacement"]), AUTHOR, date, ids)
            if _views(p)[1] != before[:start] + action["replacement"] + before[end:]:
                raise AstraReviewError("Tracked Astra correction did not produce the exact proposed text")
        pkg.mark_modified(paras[pid].part)

    changed_comments = set()
    comments = None
    for action in review["actions"]:
        kind = action["kind"]
        if kind not in {"remove_comment", "replace_comment", "add_author_query"}:
            continue
        # Modern threaded metadata requires coordinated IDs not supported by
        # this classic-comment writer. Never silently orphan those records.
        if any(pkg.has(n) for n in ("word/commentsExtended.xml", "word/commentsIds.xml", "word/commentsExtensible.xml")):
            raise AstraReviewError("Threaded comment reconciliation requires internal repair")
        for cid in action["comment_ids"] if kind != "add_author_query" else []:
            if cid in changed_comments:
                raise AstraReviewError("Astra changes the same comment more than once")
            changed_comments.add(cid)
            root = pkg.tree("word/comments.xml")
            matches = [c for c in root.findall(qn("w:comment")) if c.get(qn("w:id")) == cid]
            if len(matches) != 1:
                raise AstraReviewError("Astra comment target is missing")
            comment = matches[0]
            if kind == "replace_comment":
                for child in list(comment):
                    comment.remove(child)
                p = etree.SubElement(comment, qn("w:p"))
                r = etree.SubElement(p, qn("w:r"))
                set_text(etree.SubElement(r, qn("w:t")), action["replacement"])
                comment.set(qn("w:author"), PROOFREADER_AUTHOR)
                comment.set(qn("w:initials"), PROOFREADER_INITIALS)
                comment.set(qn("w:date"), date)
            else:
                root.remove(comment)
                for part in pkg.names():
                    if not part.startswith("word/") or not part.endswith(".xml"):
                        continue
                    tree = pkg.tree(part)
                    for el in list(tree.iter()):
                        if el.tag in {qn("w:commentRangeStart"), qn("w:commentRangeEnd"), qn("w:commentReference")} and el.get(qn("w:id")) == cid:
                            el.getparent().remove(el)
                            pkg.mark_modified(part)
            pkg.mark_modified("word/comments.xml")
        if kind == "add_author_query":
            p = paras[action["para_id"]].element
            text = _views(p)[1]
            quote = action["quote"] or text
            if not quote or text.count(quote) != 1:
                raise AstraReviewError("Astra author question has no unambiguous surviving anchor")
            start, end = text.index(quote), text.index(quote) + len(quote)
            _safe_span(p, start, end)
            comments = comments or _Comments(pkg, date)
            if not comments.attach_to_span(p, start, end, action["replacement"]):
                raise AstraReviewError("Astra author question could not be anchored")
            pkg.mark_modified(paras[action["para_id"]].part)
    if {pid: _views(p.element)[0] for pid, p in paras.items()} != source_text:
        raise AstraReviewError("Astra reconciliation changed the reject-all source text")
    pkg.save(output)


def _repaired_artifacts(frozen, review):
    artifacts = copy.deepcopy(frozen["artifacts"])
    repairs = finding_explanation_repairs(review, frozen)
    for fid, row in zip(frozen["finding_ids"], artifacts["findings.json"]["findings"]):
        if fid in repairs:
            row["explanation"] = repairs[fid]
    return artifacts


def _check_unchanged_evidence(frozen, current, artifacts):
    for key in ("book_context", "missing_artifacts", "finding_ids", "document_name",
                "source_sha256", "review_contract_sha256", "issue_sha256", "issue_index"):
        if frozen.get(key) != current.get(key):
            raise AstraReviewError("Post-Astra review evidence changed outside the approved document plan")
    if (current["artifacts"] != artifacts or
            current["findings_sha256"] != _hash(artifacts["findings.json"]["findings"])):
        raise AstraReviewError("Post-Astra review evidence changed outside the approved metadata plan")


def _evidence(frozen, current, review):
    from galley.verify import accepted_fingerprint, paragraph_fingerprints
    before = {p["id"]: p["text"] for p in frozen["accepted_paragraphs"]}
    after = {p["id"]: p["text"] for p in current["accepted_paragraphs"]}
    if set(before) != set(after):
        raise AstraReviewError("Astra reconciliation changed paragraph topology")
    old_hashes, new_hashes = paragraph_fingerprints(before), paragraph_fingerprints(after)
    changes = [{"para_id": pid, "before_sha256": old_hashes[pid], "after_sha256": new_hashes[pid],
                "action_ids": [a["id"] for a in review["actions"] if a["para_id"] == pid]}
               for pid in before if before[pid] != after[pid]]
    if any(not p["action_ids"] for p in changes):
        raise AstraReviewError("Reconciled paragraph has no Astra authorization")
    result = {"original_accepted_sha256": accepted_fingerprint(before),
            "current_accepted_sha256": accepted_fingerprint(after), "paragraph_changes": changes,
            "source_docx_sha256": frozen["document_sha256"],
            "current_docx_sha256": current["document_sha256"],
            "current_packet_sha256": current["packet_sha256"], "actions_sha256": _hash(review["actions"])}
    if finding_explanation_repairs(review, frozen):
        result["metadata_changes"] = [
            {"action_id": a["id"], "finding_id": a["finding_ids"][0],
             "artifact": "findings.json", "field": "explanation",
             "before_sha256": _hash(a["quote"]), "after_sha256": _hash(a["replacement"])}
            for a in review["actions"] if a["kind"] == "internal_repair"]
        result["original_findings_sha256"] = frozen["findings_sha256"]
        result["current_findings_sha256"] = current["findings_sha256"]
    return result


def validate_reconciliation(run_dir, receipt, frozen, current):
    """Verify complete replay, including unchanged evidence and all ZIP parts."""
    run = Path(run_dir)
    review = validate_review(receipt["review"], frozen)
    if review["editorial_verdict"] != "ready":
        raise AstraReviewError("Only Astra can clear the manuscript for delivery")
    proof = _load(run / RECONCILIATION_FILE)
    if proof.get("status") not in {"prepared", "completed"} or proof.get("review_sha256") != _hash(review):
        raise AstraReviewError("Missing or mismatched Astra application receipt")
    if _hash({k: v for k, v in frozen.items() if k != "packet_sha256"}) != frozen["packet_sha256"]:
        raise AstraReviewError("Frozen Astra packet has changed")
    _check_unchanged_evidence(frozen, current, _repaired_artifacts(frozen, review))
    if current.get("coverage_issues"):
        raise AstraReviewError("Reconciled document has incomplete structural coverage")
    source = run / SOURCE_FILE
    if not source.is_file() or _sha(source) != frozen["document_sha256"]:
        raise AstraReviewError("Immutable Astra source is missing or changed")
    inputs = receipt.get("inputs", {})
    target = Path(proof["docx_path"])
    if inputs.get("docx_path") and target.resolve() != Path(inputs["docx_path"]).resolve():
        raise AstraReviewError("Astra application receipt points to another manuscript")
    if _sha(target) != current["document_sha256"]:
        raise AstraReviewError("Reconciliation target and current packet differ")
    with tempfile.TemporaryDirectory(prefix="astra-replay-") as folder:
        candidate = Path(folder) / target.name
        _replay(source, candidate, frozen, review, proof["applied_at"])
        if _members(candidate) != _members(target):
            raise AstraReviewError("Document differs from the exact authorized Astra operations")
    expected = _evidence(frozen, current, review)
    if any(proof.get(k) != v for k, v in expected.items()):
        raise AstraReviewError("Astra application receipt does not match the verified final document")
    return dict(receipt, repair_required=False, delivery_ready=True, reconciliation=proof)


def reconcile_run(run_dir, *, docx_path=None, context_paths=()):
    """Atomic, resumable local application; never calls or changes the model."""
    run = Path(run_dir).resolve()
    with (run / ".astra-reconcile.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AstraReviewError("Another Astra reconciliation owns this run") from exc
        return _reconcile_run(run, docx_path=docx_path, context_paths=context_paths)


def _reconcile_run(run_dir, *, docx_path=None, context_paths=()):
    run = Path(run_dir).resolve()
    receipt = _load(run / RECEIPT_FILE)
    if (receipt.get("status") != "completed" or receipt.get("model") != MODEL or
            receipt.get("reasoning_effort") != REASONING_EFFORT):
        raise AstraReviewError("Reconciliation requires a completed Astra-high review")
    frozen = _load(run / PACKET_FILE)
    if receipt.get("packet_sha256") != frozen.get("packet_sha256"):
        raise AstraReviewError("Astra packet and receipt disagree")
    review = validate_review(receipt["review"], frozen)
    if review["editorial_verdict"] != "ready":
        raise AstraReviewError("Astra requested human PR; local application cannot override it")
    inputs = receipt.get("inputs", {})
    from galley.astra_review import _choose_docx
    target = _choose_docx(run, docx_path or inputs.get("docx_path"))
    context_paths = context_paths or inputs.get("context_paths", ())
    current = build_packet(run, docx_path=target, context_paths=context_paths)
    previous = _load(run / RECONCILIATION_FILE) if (run / RECONCILIATION_FILE).exists() else {}
    if previous and previous.get("review_sha256") != _hash(review):
        raise AstraReviewError("Another Astra plan already owns reconciliation")
    if current["packet_sha256"] != frozen["packet_sha256"] and previous.get("status") != "prepared":
        return validate_reconciliation(run, receipt, frozen, current)
    artifacts = _repaired_artifacts(frozen, review)
    # A prepared transaction may have installed either file before a crash.
    # Accept only the full original or the full authorized metadata, never a
    # third state or a partial edit of a finding.
    if current["artifacts"] not in (frozen["artifacts"], artifacts):
        raise AstraReviewError("Review evidence changed outside the approved metadata plan")
    _check_unchanged_evidence(frozen, current, current["artifacts"])
    source = run / SOURCE_FILE
    if source.exists():
        if _sha(source) != frozen["document_sha256"]:
            raise AstraReviewError("Immutable reviewed source has changed")
    else:
        if current["document_sha256"] != frozen["document_sha256"]:
            raise AstraReviewError("Immutable Astra source is missing after document replacement")
        source.parent.mkdir(parents=True, exist_ok=True)
        with source.open("xb") as f:
            f.write(target.read_bytes())
            f.flush()
            os.fsync(f.fileno())
    date = previous.get("applied_at") or _now()
    with tempfile.TemporaryDirectory(prefix=".astra-apply-", dir=run) as folder:
        candidate = Path(folder) / target.name
        _replay(source, candidate, frozen, review, date)
        after = build_packet(run, docx_path=candidate, context_paths=context_paths)
        after["artifacts"] = artifacts
        after["findings_sha256"] = _hash(artifacts["findings.json"]["findings"])
        after["packet_sha256"] = _hash({k: v for k, v in after.items() if k != "packet_sha256"})
        if after.get("coverage_issues"):
            raise AstraReviewError("Astra application produced invalid comment or revision coverage")
        proof = {"schema_version": 1, "status": "prepared", "applied_at": date,
                 "review_sha256": _hash(review), "packet_sha256": frozen["packet_sha256"],
                 "docx_path": str(target), **_evidence(frozen, after, review)}
        if previous.get("status") == "prepared" and previous != proof:
            raise AstraReviewError("Prepared Astra application receipt differs from the exact replay")
        if (_members(target) != _members(source) and _members(target) != _members(candidate)):
            raise AstraReviewError("Document differs from the original or authorized Astra operations")
        # A durable plan before replacement lets a crash resume on either side
        # of the atomic file replacement without paying for another review.
        _atomic(run / RECONCILIATION_FILE, proof)
        if build_packet(run, docx_path=target, context_paths=context_paths)["packet_sha256"] != current["packet_sha256"]:
            raise AstraReviewError("Review evidence changed during reconciliation")
        os.replace(candidate, target)
        if artifacts != frozen["artifacts"]:
            _atomic(run / "findings.json", artifacts["findings.json"])
    proof["status"] = "completed"
    _atomic(run / RECONCILIATION_FILE, proof)
    final = build_packet(run, docx_path=target, context_paths=context_paths)
    return validate_reconciliation(run, receipt, frozen, final)
