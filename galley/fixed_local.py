"""Local proofreading evidence for the fixed recipe; never edits or model calls.

Every actionable result is only a proposal for the fixed Opus/Luna gates.
LanguageTool failures are operational failures, never empty clean reads. Fully
completed, source/configuration/version-bound packets are reused on resume.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from bisect import bisect_right
import copy
from dataclasses import asdict, replace
import hashlib
import inspect
import itertools
import json
from pathlib import Path
from typing import Any

from docproof.models import Anchor, DocumentModel, Finding, ParagraphRef
from galley.fixed_calls import _atomic, _hash, _json, _load, _locked

VERSION = "fixed-local-v1"
_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENTATIONS = (
    "galley/fixed_local.py", "galley/fixed_policy.py", "galley/local_runtime.py",
    "docproof/adjudicate.py", "docproof/candidate_generators.py", "docproof/languagetool.py",
    "docproof/consistency.py", "docproof/continuity.py", "docproof/genrescans.py",
    "docproof/normalize.py", "docproof/residuals.py", "docproof/speakersplit.py", "docproof/sweeps.py",
    "docproof/agreement.py", "docproof/candidate_models.py", "docproof/function_words.py",
    "docproof/headings.py", "docproof/models.py", "docproof/site_generators.py",
    "docproof/spellscan.py", "docproof/toccheck.py", "docproof/validator.py",
    "docproof/variants.py", "docproof/utils/xml_helpers.py",
)


class FixedLocalError(RuntimeError):
    """A required local check has not completed with valid evidence."""


LocalCheckError = FixedLocalError


def default_lt_factory(dictionary):
    """Production transport seam; starts only the verified pinned local engine."""
    from docproof import languagetool as lt
    if not lt.AVAILABLE:
        raise FixedLocalError("LanguageTool is unavailable; the required local scan cannot run")
    from galley.local_runtime import pinned_languagetool
    return pinned_languagetool(dictionary)


def _config(cfg):
    from galley.fixed_policy import configuration
    return (cfg or configuration()).model_copy(deep=True)


def _versions():
    return {name: hashlib.sha256((_ROOT / name).read_bytes()).hexdigest()
            for name in _IMPLEMENTATIONS if (_ROOT / name).is_file()}


def _paragraphs(prepared, texts, poetry_ids):
    if not isinstance(texts, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in texts.items()):
        raise FixedLocalError("Local checks require a complete paragraph-id/text mapping")
    known = {p.para_id: p for p in prepared.doc.paragraphs}
    # Retain real locations/styles for paragraphs omitted from model chunking.
    if getattr(prepared, "pkg", None) is not None:
        from docproof.utils.xml_helpers import walk_package, qn
        for p in walk_package(prepared.pkg):
            style_node = p.element.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
            style = style_node.get(qn("w:val"), "Normal") if style_node is not None else "Normal"
            known.setdefault(p.para_id, ParagraphRef(p.para_id, p.part, p.location,
                                                   texts.get(p.para_id, ""), style))
    return [replace(known[pid], text=text) if pid in known else
            ParagraphRef(pid, "word/document.xml", "body", text, "Normal")
            for pid, text in texts.items() if pid not in set(poetry_ids)]


def _request(prepared, texts, identity, cfg, poetry_ids, stage, **extra):
    from galley.local_assets import local_asset_identity
    paragraphs = _paragraphs(prepared, texts, poetry_ids)
    request = {"version": VERSION, "stage": stage, "identity": identity,
        "configuration": cfg.model_dump(mode="json"), "implementations": _versions(),
        "paragraphs": [asdict(p) for p in paragraphs],
        "excluded_poetry_ids": sorted(set(texts) & set(poetry_ids)),
        "lexicon": list(getattr(prepared.spell, "lexicon", ())),
        "near_duplicates": [asdict(p) for p in getattr(prepared.spell, "near_duplicates", ())],
        "variant": asdict(prepared.variant),
        "local_assets": local_asset_identity(cfg, prepared) if paragraphs else None,
        **extra}
    return paragraphs, json.loads(_json(request))


def _packet(directory, request, build):
    # Prepared Finding dataclasses add tuples after _request's initial JSON
    # normalization. Compare and persist the same JSON representation on both
    # the first scan and resume, including provenance and anchor metadata.
    request = json.loads(_json(request))
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    sha = _hash(request)
    path = directory / (request["stage"] + "-" + sha + ".json")
    stage_key = _hash({"stage": request["stage"], "identity": request["identity"]})
    marker_path = directory / ("stage-" + stage_key + ".identity.json")
    marker = {"stage": request["stage"], "identity_sha256": _hash(request["identity"]), "request_sha256": sha}
    with _locked(directory / ("stage-" + stage_key + ".lock")):
        if marker_path.exists():
            if _load(marker_path) != marker:
                _recover_failed_scan(directory, marker_path, marker, request)
        else:
            _atomic(marker_path, marker)
        if path.exists():
            saved = _load(path)
            _validate_packet(saved, request)
        else:
            try:
                findings, checks, diagnostics, extra = build()
                saved = {"status": "completed", "request": request, "request_sha256": sha,
                    "findings": findings, "checks": checks, "diagnostics": diagnostics, **extra}
                saved["result_sha256"] = _hash({k: v for k, v in saved.items() if k != "result_sha256"})
                _validate_packet(saved, request)
                _atomic(path, saved)
            except Exception as exc:
                _atomic(directory / (sha + ".failure.json"), {"request_sha256": sha,
                    "request": request, "status": "failed", "error_type": type(exc).__name__})
                raise
    evidence = {"kind": "fixed_local", "version": VERSION, "stage": request["stage"],
        "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "marker_path": str(marker_path), "marker_sha256": hashlib.sha256(marker_path.read_bytes()).hexdigest(),
        "request_sha256": sha, "identity_sha256": _hash(request["identity"]),
        "paragraph_ids": [p["para_id"] for p in request["paragraphs"]],
        "excluded_poetry_ids": request["excluded_poetry_ids"], "checks": saved["checks"],
        "proposal_count": len(saved["findings"]), "diagnostic_count": len(saved["diagnostics"])}
    return saved["findings"], evidence


def _recover_failed_scan(directory, marker_path, marker, request):
    """Retry an unpublished failed scan after a code repair, under its lock.

    No completed packet is ever reused or relabeled. Source, policy, runtime
    assets and prepared findings must be identical; only implementation hashes
    may change. Model receipts and their original budgets remain untouched.
    """
    message = ("The local-check inputs, configuration or dependencies changed inside this run; "
               "start a new run to use changed local evidence")
    prior = _load(marker_path)
    sha = prior.get("request_sha256", "")
    # A valid marker must name a hash, never a path supplied by a damaged file.
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise FixedLocalError(message)
    failure_path = directory / (sha + ".failure.json")
    if (directory / (request["stage"] + "-" + sha + ".json")).exists() or not failure_path.is_file():
        raise FixedLocalError(message)
    failure = _load(failure_path)
    old = failure.get("request")
    if (not isinstance(old, dict) or _hash(old) != sha or
            failure.get("status") != "failed" or failure.get("request_sha256") != sha or
            prior != {"stage": request["stage"], "identity_sha256": _hash(request["identity"]),
                      "request_sha256": sha} or
            {k: v for k, v in old.items() if k != "implementations"} !=
            {k: v for k, v in request.items() if k != "implementations"} or
            not isinstance(old.get("implementations"), dict) or
            not isinstance(request.get("implementations"), dict)):
        raise FixedLocalError(message)
    # Persist the transition before replacing the marker: either side of an
    # interruption can replay safely. The failure and its inputs stay intact.
    _atomic(directory / ("recovery-" + sha + "-" + marker["request_sha256"] + ".json"),
            {"status": "rescan_required", "reason": "implementation repair after unpublished failure",
             "previous_marker": prior, "next_marker": marker,
             "failure_sha256": hashlib.sha256(failure_path.read_bytes()).hexdigest()})
    _atomic(marker_path, marker)


def _validate_packet(saved, request):
    if (saved.get("status") != "completed" or saved.get("request") != request or
            saved.get("request_sha256") != _hash(request) or
            saved.get("result_sha256") != _hash({k: v for k, v in saved.items() if k != "result_sha256"})):
        raise FixedLocalError("Local-check cache has changed or did not complete")
    paragraphs = {p["para_id"]: p["text"] for p in request["paragraphs"]}
    if len(paragraphs) != len(request["paragraphs"]):
        raise FixedLocalError("Local-check paragraph inventory is duplicated")
    expected = ({"house_sweeps", "consistency", "residuals", "recurrences", "calendar", "normalization_and_speakers"}
                if request.get("completion") else
                {"sweeps", "consistency", "genre", "calendar", "dictionary", "candidate_generators", "normalization_and_speakers", "languagetool"}) if paragraphs else set()
    checks = saved.get("checks", [])
    if len(checks) != len(expected) or {c.get("check") for c in checks} != expected:
        raise FixedLocalError("Local-check evidence is missing a required check")
    for check in saved.get("checks", []):
        if check.get("status") != "completed" or check.get("paragraph_ids") != list(paragraphs):
            raise FixedLocalError("A local check has incomplete paragraph coverage")
    for row in saved.get("findings", []):
        pid = row.get("para_id")
        if pid not in paragraphs or row.get("action") not in {"edit", "query"}:
            raise FixedLocalError("A local proposal is outside its reviewed evidence")
        _locate(paragraphs[pid], row.get("quote"), row.get("occurrence"))
    if not isinstance(saved.get("diagnostics"), list):
        raise FixedLocalError("Local-check diagnostic evidence is incomplete")


def validate_local_evidence(evidence, directory, identity):
    """Read-only certificate validation. Never starts Java, a model or a check."""
    path, directory = Path(evidence["path"]).resolve(), Path(directory).resolve()
    if not path.is_relative_to(directory) or path.suffix != ".json":
        raise FixedLocalError("Local evidence path is outside its workflow directory")
    if hashlib.sha256(path.read_bytes()).hexdigest() != evidence.get("sha256"):
        raise FixedLocalError("Certified local-check evidence has changed")
    saved = _load(path)
    request = saved.get("request", {})
    if request.get("identity") != identity or evidence.get("identity_sha256") != _hash(identity):
        raise FixedLocalError("Local-check evidence belongs to another source or recipe")
    _validate_packet(saved, request)
    stage_key = _hash({"stage": request["stage"], "identity": request["identity"]})
    marker_path = directory / ("stage-" + stage_key + ".identity.json")
    if (Path(evidence.get("marker_path", "")).resolve() != marker_path or
            hashlib.sha256(marker_path.read_bytes()).hexdigest() != evidence.get("marker_sha256") or
            _load(marker_path) != {"stage": request["stage"], "identity_sha256": _hash(identity),
                                  "request_sha256": saved["request_sha256"]}):
        raise FixedLocalError("The local-check stage identity has changed or is missing")
    if (evidence.get("request_sha256") != saved["request_sha256"] or
            evidence.get("kind") != "fixed_local" or evidence.get("version") != request.get("version") or
            evidence.get("stage") != request.get("stage") or
            evidence.get("paragraph_ids") != [p["para_id"] for p in request["paragraphs"]] or
            evidence.get("excluded_poetry_ids") != request.get("excluded_poetry_ids") or
            evidence.get("checks") != saved["checks"] or evidence.get("proposal_count") != len(saved["findings"]) or
            evidence.get("diagnostic_count") != len(saved["diagnostics"])):
        raise FixedLocalError("Local-check certificate does not match its source packet")
    return saved


def _locate(text, quote, occurrence):
    if not isinstance(quote, str) or not quote or type(occurrence) is not int or occurrence < 1:
        raise FixedLocalError("A local proposal needs a nonempty exact quote")
    at = -1
    for _ in range(occurrence):
        at = text.find(quote, at + 1)
        if at < 0:
            raise FixedLocalError("A local proposal does not match its source paragraph")
    return at


def _span(para, start, end, replacement, category, reason, source, *, metadata=None):
    if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(para.text) or
            replacement is not None and not isinstance(replacement, str)):
        raise FixedLocalError("Local checker returned an invalid text span or replacement")
    if not para.text:
        raise FixedLocalError("A local checker proposed a correction in an empty paragraph")
    # Concrete edits retain the full comparison for deterministic shrinking.
    # Queries anchor the actual signal, so distinct sites in a paragraph survive.
    quote, occurrence = para.text, 1
    correction = para.text[:start] + replacement + para.text[end:] if replacement is not None else para.text
    if replacement is None:
        quote_start, quote_end = (start, end) if end > start else (max(0, start - 30), min(len(para.text), start + 30))
        quote = correction = para.text[quote_start:quote_end]
        occurrence, at = 0, -1
        while at < quote_start:
            at = para.text.find(quote, at + 1)
            occurrence += 1
    row = {"para_id": para.para_id, "quote": quote, "replacement": correction,
        "occurrence": occurrence, "category": category, "action": "edit" if replacement is not None else "query",
        "reason": reason, "missing_knowledge": "", "source": source,
        "local_evidence": {"start": start, "end": end, **(metadata or {})}}
    return row


def _finding(finding, by_id, source):
    if finding.para_id not in by_id:
        return None
    para = by_id[finding.para_id]
    # Legacy sweep findings encode a pure insertion as an empty quotation:
    # occurrence_of(text, "", offset) is offset + 1. Give the fixed readers a
    # nonempty comparison without changing the insertion position or content.
    if finding.original_text == "":
        if (type(finding.occurrence) is not int or
                not 1 <= finding.occurrence <= len(para.text) + 1 or
                not isinstance(finding.corrected_text, str) or not finding.corrected_text):
            raise FixedLocalError("A local insertion needs an exact offset and replacement")
        offset = finding.occurrence - 1
        return _span(para, offset, offset, None if finding.force_query else finding.corrected_text,
            finding.error_type, finding.explanation, source,
            metadata={"finding_id": finding.finding_id, "producer": finding.error_type,
                      "original_quote": "", "source_occurrence": finding.occurrence})
    _locate(para.text, finding.original_text, finding.occurrence)
    return {"para_id": finding.para_id, "quote": finding.original_text,
        "replacement": finding.corrected_text, "occurrence": finding.occurrence,
        "category": finding.error_type, "action": "query" if finding.force_query or finding.original_text == finding.corrected_text else "edit",
        "reason": finding.explanation, "missing_knowledge": "", "source": source,
        "local_evidence": {"finding_id": finding.finding_id, "producer": finding.error_type}}


def _deduplicate(rows):
    found = {}
    for row in rows:
        if row is None:
            continue
        fields = ("para_id", "quote", "replacement", "occurrence", "action", "category")
        if row["action"] == "query":
            fields += ("reason", "missing_knowledge")
        key = _hash({k: row[k] for k in fields})
        if key in found:
            found[key]["local_evidence"].setdefault("sources", [found[key]["source"]]).append(row["source"])
        else:
            found[key] = row
    return [found[key] for key in sorted(found)]


def _check(name, paragraphs, count, **details):
    return {"check": name, "status": "completed", "paragraph_ids": [p.para_id for p in paragraphs],
            "proposal_count": count, **details}


def _dictionary(prepared, cfg):
    from docproof.spellscan import _dictionary as load_dictionary
    language = cfg.spellcheck.dictionary or getattr(prepared.variant, "dictionary", None) or "en_US"
    if load_dictionary(language) is None:
        raise FixedLocalError(f"The local {language} dictionary is unavailable; spelling candidate scan did not run")
    return language


def _dictionary_rows(paragraphs, prepared, cfg):
    from docproof.adjudicate import generate, site_word_candidates, _match_case
    from docproof.spellscan import _WORD
    language = _dictionary(prepared, cfg)
    # The number of tokens is an upper bound on sites; no logged-only truncation.
    ceiling = max(1, sum(len(_WORD.findall(p.text)) for p in paragraphs))
    candidates = site_word_candidates({nd.word: nd.suggestion for nd in prepared.spell.near_duplicates},
                                      paragraphs, kind="near_dup")
    candidates += generate(paragraphs, protected=prepared.spell.lexicon, denylist=cfg.spellcheck.denylist,
        respell=getattr(prepared.variant, "respell_map", {}), dictionary=language,
        near_miss_gap=cfg.adjudicate.near_miss_gap, min_len=cfg.adjudicate.min_word_len,
        max_candidates=ceiling)
    candidates += list(getattr(prepared, "adjudicate_candidates", ()))
    by_id = {p.para_id: p for p in paragraphs}
    rows = []
    for c in candidates:
        if c.para_id not in by_id:
            continue
        p = by_id[c.para_id]
        rows.append(_span(p, c.start, c.end, _match_case(p.text[c.start:c.end], c.suggestion),
            "spelling", "Local dictionary/frequency signal; context must establish a clear typo before any correction.",
            "local:dictionary", metadata={"kind": c.kind}))
    return _deduplicate(rows), {"dictionary": language, "site_ceiling": ceiling, "capped": False}


def _scan_group(tool, texts, counts):
    """Strict batching with an explicit count for synthetic join artifacts."""
    from docproof.languagetool import _JOIN
    joined = _JOIN.join(texts)
    found = tool.check(joined)
    if not isinstance(found, list):
        raise FixedLocalError("LanguageTool returned incomplete paragraph results")
    starts, at = [], 0
    for text in texts:
        starts.append(at)
        at += len(text) + len(_JOIN)
    out = [[] for _ in texts]
    for match in found:
        if (type(match.offset) is not int or type(match.error_length) is not int or
                not 0 <= match.offset <= match.offset + match.error_length <= len(joined)):
            raise FixedLocalError("LanguageTool returned an out-of-range anchor")
        counts["raw_matches"] += 1
        index = bisect_right(starts, match.offset) - 1
        offset = match.offset - starts[index]
        if offset + match.error_length > len(texts[index]):
            counts["batch_boundary_artifact"] += 1
            continue
        local = copy.copy(match)
        local.offset = offset
        out[index].append(local)
    return out


def _language_tool(paragraphs, prepared, cfg, tool_factory, progress):
    from docproof import languagetool as lt
    if tool_factory is None:
        tool_factory = default_lt_factory
    rows, counts = [], Counter()
    texts = [p.text for p in paragraphs]
    lexicon = {w.strip("'’\".,").lower() for w in tuple(prepared.spell.lexicon) + tuple(cfg.spellcheck.allowlist)}
    disabled_rules = lt.all_disabled_rules(cfg.languagetool.disabled_rules)
    tool = None
    error = None
    try:
        tool = tool_factory(cfg.languagetool.dictionary)
        if hasattr(tool, "picky"):
            tool.picky = cfg.languagetool.picky
        done = 0
        for group in lt._groups(texts, cfg.languagetool.scan_chars):
            matches = _scan_group(tool, [texts[i] for i in group], counts)
            if len(matches) != len(group) or any(not isinstance(m, list) for m in matches):
                raise FixedLocalError("LanguageTool returned incomplete paragraph results")
            for index, found in zip(group, matches):
                para = paragraphs[index]
                for m in found:
                    if m.rule_id in disabled_rules or m.rule_issue_type in lt.DEFAULT_DISABLED_ISSUE_TYPES:
                        counts["style_or_artifact"] += 1
                        continue
                    start, end = m.offset, m.offset + m.error_length
                    if not 0 <= start <= end <= len(para.text):
                        raise FixedLocalError("LanguageTool returned an out-of-range anchor")
                    original = para.text[start:end]
                    if original.strip("'’\".,").lower() in lexicon:
                        counts["protected_name"] += 1
                        continue
                    replacements = [x for x in (m.replacements or []) if x is not None]
                    correction = replacements[0] if replacements else None
                    if correction is not None and lt.classify_replacement(original, correction) == "deaccent":
                        counts["deaccent"] += 1
                        continue
                    row = _span(para, start, end, correction, "grammar",
                        str(getattr(m, "message", "Local grammar rule matched; verify in literary context.")),
                        "local:languagetool", metadata={"rule_id": m.rule_id, "issue_type": m.rule_issue_type,
                            "word_change_requires_context": correction is not None and lt.classify_replacement(original, correction) == "word"})
                    rows.append(row)
            done += len(group)
            if progress:
                progress(done, len(paragraphs))
        if done != len(paragraphs):
            raise FixedLocalError("LanguageTool did not review every assigned paragraph")
    except Exception as exc:
        error = exc
        raise FixedLocalError("The local LanguageTool scan did not complete; no clean result was recorded") from exc
    finally:
        if tool is not None:
            try:
                tool.close()
            except Exception as exc:
                if error is None:
                    raise FixedLocalError("LanguageTool failed to shut down after its scan") from exc
    runtime = getattr(tool, "runtime", None)
    details = {**dict(counts), "runtime": runtime} if isinstance(runtime, dict) else dict(counts)
    return _deduplicate(rows), details


def _normalize_and_structure(paragraphs, prepared):
    from docproof.agreement import canonical_anchors
    from docproof.normalize import normalize_text
    from docproof.speakersplit import find_split_offsets
    rows = []
    for para in paragraphs:
        normalized = normalize_text(para.text, quotes=True, spaces=True, variant=prepared.variant)
        for anchor in canonical_anchors(para.text, normalized):
            rows.append(_span(para, anchor.start, anchor.end, anchor.insert_text, "punctuation",
                "Existing local quote/space normalization proposes this change; apply only when the fixed house rules and manuscript intent support it.", "local:normalization"))
        for offset in find_split_offsets(para.text, prepared.variant):
            rows.append(_span(para, offset, offset, None, "grammar",
                "Possible speaker change within one paragraph. The detector cannot establish speaker identity; do not split or rewrite prose automatically.",
                "local:speaker_boundary", metadata={"split_offset": offset, "structural_only": True}))
    return rows


def _house_findings(paragraphs, prepared, cfg):
    from docproof.sweeps import run_sweeps, unclosed_quote_findings, heading_case_findings, heading_vocab_findings
    found, reports = run_sweeps(paragraphs, cfg.sweeps, prepared.variant, ellipsis_style=cfg.style.ellipsis)
    if cfg.style.unclosed_quote_queries:
        found += unclosed_quote_findings(paragraphs, prepared.variant)
    if cfg.style.heading_title_case:
        heading, report = heading_case_findings(paragraphs, cfg.skip)
        found += heading
        reports.append(report)
    if cfg.style.heading_vocab_queries:
        found += heading_vocab_findings(paragraphs, cfg.skip)
    return found, [asdict(report) for report in reports]


def _consistency_findings(paragraphs, prepared, cfg):
    from docproof.consistency import find_inconsistencies, to_findings
    options = {k: v for k, v in cfg.consistency.model_dump().items() if k in inspect.signature(find_inconsistencies).parameters}
    options.update(enabled=True, max_queries_per_kind=max(1, sum(len(p.text) for p in paragraphs)),
        respell=getattr(prepared.variant, "respell_map", {}),
        protected=tuple(prepared.spell.lexicon) + tuple(cfg.consistency.seeded_names),
        dictionary=cfg.spellcheck.dictionary or getattr(prepared.variant, "dictionary", None) or "en_US")
    return to_findings(find_inconsistencies(paragraphs, **options), paragraphs)


def _genre_findings(paragraphs, cfg):
    from docproof.genrescans import run_genre_scans
    settings = cfg.genre_scans.model_copy(deep=True)
    ceiling = max(1, sum(len(p.text) for p in paragraphs))
    for name in ("anachronism", "citation_format", "reading_level"):
        getattr(settings, name).max_queries = ceiling
    return run_genre_scans(paragraphs, settings)


def collect_local_candidates(prepared, texts, directory, *, identity, poetry_ids=(), cfg=None,
                             lt_factory=None, progress=None):
    """Initial local scans, including prepared findings and diagnostic-only sites."""
    cfg = _config(cfg)
    paragraphs, request = _request(prepared, texts, identity, cfg, poetry_ids, "initial")
    # Prepared outputs are evidence too; their changed config/content cannot
    # accidentally reuse an older local packet.
    request["prepared_findings"] = {name: [asdict(f) for f in getattr(prepared, name, ())]
        for name in ("sweep_findings", "consistency_findings", "genre_findings", "adjudicate_candidates")}

    def build():
        from docproof.candidate_generators import generate_initial_candidates
        from docproof.continuity import calendar_findings
        from docproof.toccheck import structure_extract
        from galley.fixed_policy import LOCAL_CANDIDATE_TYPES, DIAGNOSTIC_ONLY_TYPES
        by_id = {p.para_id: p for p in paragraphs}
        rows, checks, diagnostics = [], [], []
        if not paragraphs:
            return rows, checks, diagnostics, {"skipped": "poetry_only"}
        # Rescan cheap house/consistency checks over the complete input map so
        # paragraph styles skipped by the typed passes remain covered.
        house, reports = _house_findings(paragraphs, prepared, cfg)
        consistency = _consistency_findings(paragraphs, prepared, cfg)
        for name, found in (("sweeps", house + list(prepared.sweep_findings)),
                            ("consistency", consistency + list(prepared.consistency_findings)),
                            ("genre", _genre_findings(paragraphs, cfg) + list(getattr(prepared, "genre_findings", ()))),
                            ("calendar", calendar_findings(paragraphs, itertools.count(1)))):
            before = len(rows)
            for finding in found:
                row = _finding(finding, by_id, "local:" + name)
                if row and finding.error_type in DIAGNOSTIC_ONLY_TYPES:
                    diagnostics.append({"kind": finding.error_type, "evidence": row})
                elif row:
                    rows.append(row)
            checks.append(_check(name, paragraphs, len(rows) - before,
                **({"anachronism_era": cfg.genre_scans.anachronism.era,
                    "anachronism_applicability": "no_stated_era" if cfg.genre_scans.anachronism.era is None else "stated_era",
                    "capped": False} if name == "genre" else {})))
        dictionary_rows, dictionary_evidence = _dictionary_rows(paragraphs, prepared, cfg)
        rows.extend(dictionary_rows)
        checks.append(_check("dictionary", paragraphs, len(dictionary_rows), **dictionary_evidence))
        doc = DocumentModel(str(getattr(prepared.doc, "source_path", "")), tuple(paragraphs))
        generated = generate_initial_candidates(doc, paragraphs, candidate_types=LOCAL_CANDIDATE_TYPES)
        screening_count = 0
        for candidate in generated:
            screening = candidate.evidence.get("local_screening", {})
            snapshot = candidate.model_dump(mode="json", exclude={"created_at"})
            if candidate.candidate_type in DIAGNOSTIC_ONLY_TYPES or screening.get("decision") == "pass":
                diagnostics.append({"kind": candidate.candidate_type, "evidence": snapshot})
                continue
            anchor = candidate.anchors[0]
            if anchor.paragraph_id not in by_id or anchor.start_offset is None or anchor.end_offset is None:
                raise FixedLocalError("A local candidate generator returned an unsupported anchor")
            if not by_id[anchor.paragraph_id].text:
                diagnostics.append({"kind": "empty_anchor", "evidence": snapshot})
                continue
            rows.append(_span(by_id[anchor.paragraph_id], anchor.start_offset, anchor.end_offset,
                candidate.candidate_correction, candidate.candidate_type,
                screening.get("explanation", "Local signal requires contextual judgment."),
                "local:" + candidate.generator_id, metadata={"generator_evidence": snapshot}))
            screening_count += 1
        checks.append(_check("candidate_generators", paragraphs, screening_count,
            types=list(LOCAL_CANDIDATE_TYPES), generated=len(generated),
            delegated_types={"number_style": "dedicated_number_sweep", "currency_style": "dedicated_number_sweep"}))
        structure = _normalize_and_structure(paragraphs, prepared)
        rows.extend(structure)
        checks.append(_check("normalization_and_speakers", paragraphs, len(structure)))
        lt_rows, lt_evidence = _language_tool(paragraphs, prepared, cfg, lt_factory, progress)
        rows.extend(lt_rows)
        checks.append(_check("languagetool", paragraphs, len(lt_rows), **lt_evidence))
        return _deduplicate(rows), checks, diagnostics, {"sweep_reports": reports,
            "structure_extract": structure_extract(paragraphs, cfg.skip)}

    return _packet(directory, request, build)


def _recurrence_seeds(original, current, excluded):
    import re
    from difflib import SequenceMatcher
    seeds = []
    # Word-token deltas preserve whole typo surfaces (recieved -> received),
    # whereas character-minimal diffs would trim them to ie -> ei.
    token = re.compile(r"\w+(?:['’]\w+)*|\W+", re.UNICODE)
    for pid, before in original.items():
        if pid in excluded or pid not in current or before == current[pid]:
            continue
        after = current[pid]
        a, b = list(token.finditer(before)), list(token.finditer(after))
        for tag, i, j, k, l in SequenceMatcher(a=[m.group() for m in a], b=[m.group() for m in b], autojunk=False).get_opcodes():
            if tag != "replace" or i == j or k == l:
                continue
            old = before[a[i].start():a[j - 1].end()]
            new = after[b[k].start():b[l - 1].end()]
            if (not 1 <= len(old.split()) <= 2 or not 1 <= len(new.split()) <= 2 or
                    not all(word.isalpha() for word in old.split() + new.split()) or
                    old != old.strip() or new != new.strip()):
                continue
            # Seeds sit at the corrected word's CURRENT location, so claimed
            # spans cannot accidentally mask a later occurrence after edits.
            seeds.append(Finding(f"local-seed-{len(seeds) + 1}", "local_approved_word", pid, "spelling",
                old, 1, new, "Previously accepted minimal word correction.", "high", status="validated",
                anchor=Anchor(b[k].start(), b[l - 1].end(), old, new)))
    return seeds


def collect_completion_candidates(prepared, original, current, directory, *, identity, stage,
                                  poetry_ids=(), cfg=None):
    """One bounded local completion scan; all residuals remain proposals."""
    if not isinstance(stage, str) or not stage or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in stage):
        raise FixedLocalError("Completion stage requires a safe stable name")
    if set(original) != set(current):
        raise FixedLocalError("Completion scan requires identical original/current paragraph coverage")
    cfg = _config(cfg)
    paragraphs, request = _request(prepared, current, identity, cfg, poetry_ids, stage,
                                   original=dict(original), completion=True)

    def build():
        from docproof.adjudicate import propagate_recurrences
        from docproof.residuals import residual_queries
        from docproof.continuity import calendar_findings
        by_id = {p.para_id: p for p in paragraphs}
        if not paragraphs:
            return [], [], [], {"skipped": "poetry_only"}
        ceiling = max(1, sum(len(p.text) for p in paragraphs))
        language = _dictionary(prepared, cfg)
        seeds = _recurrence_seeds(original, current, set(poetry_ids))
        house, reports = _house_findings(paragraphs, prepared, cfg)
        groups = {"house_sweeps": house, "consistency": _consistency_findings(paragraphs, prepared, cfg),
            "residuals": residual_queries(paragraphs, [], max_per_rule=ceiling),
            "recurrences": propagate_recurrences(seeds, paragraphs, dictionary=language,
                protected=prepared.spell.lexicon, max_sites_per_surface=ceiling),
            "calendar": calendar_findings(paragraphs, itertools.count(1))}
        rows, checks = [], []
        for name, found in groups.items():
            proposed = [_finding(f, by_id, "local:completion:" + name) for f in found]
            rows.extend(proposed)
            checks.append(_check(name, paragraphs, len(proposed)))
        structure = _normalize_and_structure(paragraphs, prepared)
        rows.extend(structure)
        checks.append(_check("normalization_and_speakers", paragraphs, len(structure)))
        return _deduplicate(rows), checks, [], {"sweep_reports": reports,
            "recurrence_seed_count": len(seeds), "uncapped_site_ceiling": ceiling,
            "recurrence_guard": "Context-dependent common-word floods retain the existing exclusion guard; no propagation is applied directly."}

    return _packet(directory, request, build)
