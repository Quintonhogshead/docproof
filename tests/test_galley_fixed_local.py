"""Local candidate checks use fake Java transport and no model or network."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from docproof.models import DocumentModel, Finding, ParagraphRef
from docproof.spellscan import SpellScan
from docproof.variants import load_variant
from galley import fixed_local as local
from galley.fixed_policy import configuration


IDENTITY = {"source_sha256": "source", "recipe": "fixed"}


def para(pid, text, *, reviewable=True, style="Normal"):
    return ParagraphRef(pid, "word/document.xml", "body", text, style, reviewable)


def prepared(*paragraphs, **kwargs):
    return SimpleNamespace(doc=DocumentModel("source.docx", tuple(paragraphs)), pkg=None,
        variant=load_variant("us"), spell=SpellScan(), sweep_findings=[], consistency_findings=[],
        genre_findings=[], adjudicate_candidates=[], **kwargs)


def match(text, original, replacement, *, rule="FAKE_GRAMMAR", issue="grammar", offset=None):
    start = text.index(original) if offset is None else offset
    return SimpleNamespace(rule_id=rule, rule_issue_type=issue, offset=start,
        error_length=len(original), replacements=[] if replacement is None else [replacement],
        matched_text=original, message="A local grammar rule matched.")


class Tool:
    def __init__(self, callback=lambda text: []):
        self.callback, self.requests, self.closed, self.picky = callback, [], False, False

    def check(self, text):
        self.requests.append(text)
        return self.callback(text)

    def close(self):
        self.closed = True


def collect(tmp_path, source, tool=None, **kwargs):
    texts = {p.para_id: p.text for p in source.doc.paragraphs}
    return local.collect_local_candidates(source, texts, tmp_path / "local", identity=kwargs.pop("identity", IDENTITY),
                                          lt_factory=lambda _: tool or Tool(), **kwargs)


def packet(evidence):
    return json.loads(Path(evidence["path"]).read_text())


def test_full_local_coverage_includes_heading_and_excludes_poetry(tmp_path):
    source = prepared(para("p1", "She have letters."), para("title", "a small heading", reviewable=False, style="Heading1"),
                      para("verse", "She have a star."))
    tool = Tool(lambda text: [match(text, "have", "has")])
    findings, evidence = collect(tmp_path, source, tool, poetry_ids={"verse"})
    assert tool.closed
    assert "a small heading" in tool.requests[0]
    assert "star" not in "\n".join(tool.requests)
    assert evidence["paragraph_ids"] == ["p1", "title"]
    assert all(check["paragraph_ids"] == ["p1", "title"] for check in evidence["checks"])
    assert all(f["para_id"] != "verse" for f in findings)
    lt = [f for f in findings if f["source"] == "local:languagetool"]
    assert lt[0]["quote"] == "She have letters." and lt[0]["replacement"] == "She has letters."
    assert source.doc.paragraphs[0].text == "She have letters."


def test_cache_reuse_skips_every_local_generator_and_java_start(tmp_path, monkeypatch):
    source = prepared(para("p1", "The room was quiet."))
    first, evidence = collect(tmp_path, source)
    monkeypatch.setattr(local, "_language_tool", lambda *a, **k: pytest.fail("Java restarted"))
    monkeypatch.setattr(local, "_dictionary_rows", lambda *a, **k: pytest.fail("Dictionary reran"))
    assert collect(tmp_path, source) == (first, evidence)
    assert local.validate_local_evidence(evidence, tmp_path / "local", IDENTITY)["status"] == "completed"


@pytest.mark.parametrize("change", ["source", "config", "version"])
def test_changed_input_or_config_or_version_requires_new_run(tmp_path, monkeypatch, change):
    source = prepared(para("p1", "The room was quiet."))
    _, first = collect(tmp_path, source)
    cfg = configuration()
    if change == "source":
        source.doc = replace(source.doc, paragraphs=(para("p1", "The room was cold."),))
    elif change == "config":
        cfg.languagetool.scan_chars = 1
    else:
        monkeypatch.setattr(local, "VERSION", "changed-version")
    with pytest.raises(local.FixedLocalError, match="changed inside this run"):
        collect(tmp_path, source, cfg=cfg)
    _, second = collect(tmp_path, source, cfg=cfg, identity={**IDENTITY, "new_run": True})
    assert first["path"] != second["path"]


def test_language_tool_failure_closes_runtime_and_never_caches_clean_result(tmp_path):
    def fail(text):
        raise RuntimeError("incompleteResults")
    source = prepared(para("p1", "The room was quiet."))
    tool = Tool(fail)
    with pytest.raises(local.FixedLocalError, match="did not complete"):
        collect(tmp_path, source, tool)
    assert tool.closed
    assert not list((tmp_path / "local").glob("initial-*.json"))
    assert json.loads(next((tmp_path / "local").glob("*.failure.json")).read_text())["status"] == "failed"


def test_shutdown_failure_does_not_certify_a_complete_check(tmp_path):
    source = prepared(para("p1", "The room was quiet."))
    tool = Tool()
    tool.close = lambda: (_ for _ in ()).throw(RuntimeError("still running"))
    with pytest.raises(local.FixedLocalError, match="shut down"):
        collect(tmp_path, source, tool)
    assert not list((tmp_path / "local").glob("initial-*.json"))


@pytest.mark.parametrize("callback", [lambda text: None, lambda text: [match(text, "room", "hall", offset=999)]])
def test_missing_results_and_bad_offsets_fail_closed(tmp_path, callback):
    source = prepared(para("p1", "The room was quiet."))
    tool = Tool(callback)
    with pytest.raises(local.FixedLocalError):
        collect(tmp_path, source, tool)
    assert tool.closed


def test_poetry_only_starts_no_runtime_or_generators(tmp_path, monkeypatch):
    source = prepared(para("verse", "A star, a star."))
    monkeypatch.setattr(local, "_dictionary_rows", lambda *a, **k: pytest.fail("Dictionary generator started"))
    monkeypatch.setattr(local, "_language_tool", lambda *a, **k: pytest.fail("LanguageTool started"))
    findings, evidence = collect(tmp_path, source, poetry_ids={"verse"})
    assert findings == [] and evidence["paragraph_ids"] == []
    assert packet(evidence)["skipped"] == "poetry_only"


def test_dictionary_candidate_cap_cannot_hide_later_occurrences(tmp_path):
    source = prepared(para("p1", "alot " * 505 + "."))
    cfg = configuration()
    cfg.spellcheck.denylist = {"alot": "a lot"}
    cfg.adjudicate.max_candidates = 1
    findings, evidence = collect(tmp_path, source, cfg=cfg)
    dictionary = [f for f in findings if f["source"] == "local:dictionary"]
    assert len(dictionary) == 505
    assert next(c for c in evidence["checks"] if c["check"] == "dictionary")["capped"] is False


def test_protected_names_style_and_deaccent_stay_out_of_proposals(tmp_path):
    source = prepared(para("p1", "Zerafin had café."))
    source.spell = SpellScan(lexicon=("Zerafin",))
    tool = Tool(lambda text: [match(text, "Zerafin", "Zerafina"), match(text, "café", "cafe"),
                             match(text, "had", "possessed", issue="style")])
    findings, evidence = collect(tmp_path, source, tool)
    assert not [f for f in findings if f["source"] == "local:languagetool"]
    lt = next(c for c in evidence["checks"] if c["check"] == "languagetool")
    assert lt["protected_name"] == lt["style_or_artifact"] == lt["deaccent"] == 1


def test_word_echo_and_reading_level_are_internal_diagnostics(tmp_path):
    text = "The lantern was bright. The lantern was blue."
    source = prepared(para("p1", text))
    source.genre_findings = [Finding("gr1", "genre", "p1", "reading_level", text, 1, text, "High reading score", "low", force_query=True)]
    findings, evidence = collect(tmp_path, source)
    assert not {"reading_level", "word_echo"} & {f["category"] for f in findings}
    diagnostics = packet(evidence)["diagnostics"]
    assert {"word_echo", "reading_level"} <= {d["kind"] for d in diagnostics}


def test_numeric_candidate_generators_are_explicitly_delegated(tmp_path):
    _, evidence = collect(tmp_path, prepared(para("p1", "She counted 42 people.")))
    check = next(c for c in evidence["checks"] if c["check"] == "candidate_generators")
    assert check["delegated_types"] == {"number_style": "dedicated_number_sweep", "currency_style": "dedicated_number_sweep"}


def test_calendar_contradiction_is_only_an_internal_candidate(tmp_path):
    source = prepared(para("p1", "Tuesday, June 3, 2019, was the day she left."))
    rows, _ = collect(tmp_path, source)
    calendar = [r for r in rows if r["source"] == "local:calendar"]
    assert len(calendar) == 1 and calendar[0]["action"] == "query"
    assert calendar[0]["replacement"] == calendar[0]["quote"]


def test_completion_proposes_late_residuals_and_recurring_typo_without_applying(tmp_path):
    source = prepared(para("p1", "She recieved a letter."), para("p2", "He recieved 42 letters."))
    original = {p.para_id: p.text for p in source.doc.paragraphs}
    current = {**original, "p1": "She received a letter."}
    rows, evidence = local.collect_completion_candidates(source, original, current, tmp_path / "local",
        identity=IDENTITY, stage="pre_fable")
    recurrence = [r for r in rows if r["source"] == "local:completion:recurrences"]
    assert len(recurrence) == 1 and "received" in recurrence[0]["replacement"]
    assert any(r["source"] == "local:completion:residuals" for r in rows)
    assert current["p2"] == "He recieved 42 letters."
    assert packet(evidence)["recurrence_seed_count"] == 1
    assert local.collect_completion_candidates(source, original, current, tmp_path / "local",
        identity=IDENTITY, stage="pre_fable") == (rows, evidence)


def test_missing_check_or_changed_cache_cannot_pass_certificate(tmp_path):
    _, evidence = collect(tmp_path, prepared(para("p1", "The room was quiet.")))
    path = Path(evidence["path"])
    saved = packet(evidence)
    saved["checks"] = []
    saved["result_sha256"] = local._hash({k: v for k, v in saved.items() if k != "result_sha256"})
    path.write_text(json.dumps(saved))
    with pytest.raises(local.FixedLocalError, match="changed"):
        local.validate_local_evidence(evidence, tmp_path / "local", IDENTITY)
    evidence["sha256"] = local.hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(local.FixedLocalError, match="required check"):
        local.validate_local_evidence(evidence, tmp_path / "local", IDENTITY)


def test_equal_frequency_dictionary_neighbors_have_stable_tie_break(monkeypatch):
    from docproof import adjudicate
    monkeypatch.setattr(adjudicate, "edits1", lambda word: {"zebra", "apple"})
    monkeypatch.setattr(adjudicate, "zipf", lambda word: 4.0)
    monkeypatch.setattr(adjudicate, "_known", lambda dictionary, word: True)
    assert adjudicate._best_neighbour("xxxxx", object(), min_len=4) == ("apple", 4.0)


def test_query_signals_keep_exact_repeated_sites_and_distinct_reasons():
    p = para("p1", "A word and another word.")
    first = local._span(p, 2, 6, None, "grammar", "First signal", "local:test")
    second = local._span(p, 19, 23, None, "grammar", "First signal", "local:test")
    additional = {**second, "reason": "Another signal"}
    assert first["quote"] == second["quote"] == "word"
    assert first["occurrence"] == 1 and second["occurrence"] == 2
    assert len(local._deduplicate([first, second, additional])) == 3
    insertion = local._span(p, 2, 2, None, "grammar", "Possible missing mark", "local:test")
    assert insertion["quote"] and insertion["local_evidence"]["start"] == 2
    local._locate(p.text, insertion["quote"], insertion["occurrence"])


def test_batch_artifacts_are_counted_but_bad_batch_offsets_fail(tmp_path):
    source = prepared(para("p1", "The room was quiet."), para("p2", "The hall was silent."))
    boundary = len(source.doc.paragraphs[0].text) - 1
    tool = Tool(lambda text: [SimpleNamespace(rule_id="FAKE", rule_issue_type="grammar",
        offset=boundary, error_length=4, replacements=[], message="Across artificial join")])
    _, evidence = collect(tmp_path, source, tool)
    check = next(c for c in evidence["checks"] if c["check"] == "languagetool")
    assert check["raw_matches"] == check["batch_boundary_artifact"] == 1
    tool = Tool(lambda text: [match(text, "room", "hall", offset=len(text) + 20)])
    with pytest.raises(local.FixedLocalError):
        collect(tmp_path / "different", source, tool)


def test_runtime_identity_and_blank_heading_coverage_are_preserved(tmp_path):
    source = prepared(para("h1", "Chapter One", style="Heading1"),
                      para("h3", "", style="Heading3"), para("p1", "The room was quiet."))
    tool = Tool()
    tool.runtime = {"engine_version": "6.8", "distribution_sha256": "fixture"}
    _, evidence = collect(tmp_path, source, tool)
    check = next(c for c in evidence["checks"] if c["check"] == "languagetool")
    assert check["runtime"] == tool.runtime
    assert "h3" in check["paragraph_ids"]
    assert any(d["kind"] == "empty_anchor" for d in packet(evidence)["diagnostics"])


def test_completion_short_phrase_recurrence_is_a_proposal(tmp_path):
    source = prepared(para("p1", "She slept some where."), para("p2", "He slept some where."))
    original = {p.para_id: p.text for p in source.doc.paragraphs}
    current = {**original, "p1": "She slept somewhere."}
    rows, _ = local.collect_completion_candidates(source, original, current, tmp_path / "local",
        identity=IDENTITY, stage="pre_fable")
    recurrence = [r for r in rows if r["source"] == "local:completion:recurrences"]
    assert len(recurrence) == 1 and recurrence[0]["replacement"] == "He slept somewhere."
    assert current["p2"] == "He slept some where."
