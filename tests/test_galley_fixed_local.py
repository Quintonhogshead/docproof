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


@pytest.mark.parametrize("offset", [0, 4, 16])
def test_empty_quote_insertion_gets_exact_nonempty_comparison(offset):
    p = para("p1", "She walked home.")
    finding = Finding("f1", "sweep", p.para_id, "punctuation", "", offset + 1, "!",
                      "Insert punctuation.", "high")
    row = local._finding(finding, {p.para_id: p}, "local:sweeps")
    assert row["quote"] == p.text and row["occurrence"] == 1
    assert row["replacement"] == p.text[:offset] + "!" + p.text[offset:]
    assert row["local_evidence"]["start"] == row["local_evidence"]["end"] == offset


def test_missing_terminal_period_runs_through_all_local_checks(tmp_path):
    p = para("p1", "She walked down the quiet lane and went home before the rain arrived")
    rows, evidence = collect(tmp_path, prepared(p))
    found = [r for r in rows if r["category"] == "sweep_terminal_period"]
    assert len(found) == 1
    assert found[0]["quote"] == p.text and found[0]["replacement"] == p.text + "."
    assert p.text == "She walked down the quiet lane and went home before the rain arrived"
    local.validate_local_evidence(evidence, tmp_path / "local", IDENTITY)


@pytest.mark.parametrize("occurrence,replacement", [(0, "."), (99, "."), (True, "."), (1, "")])
def test_invalid_empty_quote_insertions_still_block(occurrence, replacement):
    p = para("p1", "Text")
    finding = Finding("f1", "sweep", p.para_id, "punctuation", "", occurrence, replacement,
                      "Insert punctuation.", "high")
    with pytest.raises(local.FixedLocalError):
        local._finding(finding, {p.para_id: p}, "local:sweeps")


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


def test_prepared_finding_tuples_roundtrip_without_repeating_local_scan(tmp_path, monkeypatch):
    p = para("p1", "The room was quiet.")
    source = prepared(p)
    source.sweep_findings = [Finding("prepared-1", "sweep", p.para_id, "punctuation",
        p.text, 1, p.text, "Review this site.", "high", provenance=(1, 2), force_query=True)]
    first, evidence = collect(tmp_path, source)
    saved = packet(evidence)
    assert saved["request"]["prepared_findings"]["sweep_findings"][0]["provenance"] == [1, 2]
    monkeypatch.setattr(local, "_language_tool", lambda *a, **k: pytest.fail("Java restarted"))
    assert collect(tmp_path, source) == (first, evidence)
    local.validate_local_evidence(evidence, tmp_path / "local", IDENTITY)


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


def test_failed_local_scan_recovers_after_code_repair_without_relabeling(tmp_path, monkeypatch):
    source = prepared(para("p1", "The room was quiet."))
    monkeypatch.setattr(local, "_versions", lambda: {"checker.py": "before"})
    tool = Tool(lambda text: (_ for _ in ()).throw(RuntimeError("incompleteResults")))
    with pytest.raises(local.FixedLocalError):
        collect(tmp_path, source, tool)
    failure = next((tmp_path / "local").glob("*.failure.json"))
    failure_bytes = failure.read_bytes()
    prior = json.loads(failure_bytes)
    assert prior["request"]["implementations"] == {"checker.py": "before"}
    monkeypatch.setattr(local, "_versions", lambda: {"checker.py": "repaired"})
    successful = Tool()
    _, evidence = collect(tmp_path, source, successful)
    assert successful.requests == ["The room was quiet."] and successful.closed
    assert failure.read_bytes() == failure_bytes
    recovery = json.loads(next((tmp_path / "local").glob("recovery-*.json")).read_text())
    assert recovery["previous_marker"]["request_sha256"] == prior["request_sha256"]
    assert recovery["next_marker"]["request_sha256"] == evidence["request_sha256"]
    local.validate_local_evidence(evidence, tmp_path / "local", IDENTITY)
    assert not (tmp_path / "local" / ("initial-" + prior["request_sha256"] + ".json")).exists()


@pytest.mark.parametrize("change", ["source", "policy", "assets", "missing_request", "tampered_request", "completed"])
def test_code_repair_cannot_bypass_changed_inputs_or_completed_evidence(tmp_path, monkeypatch, change):
    source = prepared(para("p1", "The room was quiet."))
    monkeypatch.setattr(local, "_versions", lambda: {"checker.py": "before"})
    if change == "completed":
        collect(tmp_path, source)
    else:
        tool = Tool(lambda text: (_ for _ in ()).throw(RuntimeError("incompleteResults")))
        with pytest.raises(local.FixedLocalError):
            collect(tmp_path, source, tool)
    monkeypatch.setattr(local, "_versions", lambda: {"checker.py": "after"})
    cfg = configuration()
    if change == "source":
        source.doc = replace(source.doc, paragraphs=(para("p1", "The room was cold."),))
    elif change == "policy":
        cfg.languagetool.scan_chars = 1
    elif change == "assets":
        monkeypatch.setattr("galley.local_assets.local_asset_identity", lambda *a: {"asset": "changed"})
    elif change in {"missing_request", "tampered_request"}:
        path = next((tmp_path / "local").glob("*.failure.json"))
        data = json.loads(path.read_text())
        if change == "missing_request":
            data.pop("request")
        else:
            data["request"]["implementations"] = {"checker.py": "tampered"}
        path.write_text(json.dumps(data))
    with pytest.raises(local.FixedLocalError, match="changed inside this run"):
        collect(tmp_path, source, cfg=cfg)
    assert not list((tmp_path / "local").glob("recovery-*.json"))


def test_interrupted_recovery_marker_replays_without_losing_old_failure(tmp_path, monkeypatch):
    source = prepared(para("p1", "The room was quiet."))
    monkeypatch.setattr(local, "_versions", lambda: {"checker.py": "before"})
    with pytest.raises(local.FixedLocalError):
        collect(tmp_path, source, Tool(lambda text: (_ for _ in ()).throw(RuntimeError("incompleteResults"))))
    failure = next((tmp_path / "local").glob("*.failure.json"))
    original = failure.read_bytes()
    atomic = local._atomic
    def interrupt(path, value):
        if path.name.endswith(".identity.json"):
            raise OSError("Interrupted recovery marker")
        atomic(path, value)
    monkeypatch.setattr(local, "_versions", lambda: {"checker.py": "repaired"})
    monkeypatch.setattr(local, "_atomic", interrupt)
    tool = Tool()
    with pytest.raises(OSError, match="Interrupted"):
        collect(tmp_path, source, tool)
    assert tool.requests == []
    monkeypatch.setattr(local, "_atomic", atomic)
    _, evidence = collect(tmp_path, source, tool)
    assert tool.requests == ["The room was quiet."]
    assert failure.read_bytes() == original
    assert len(list((tmp_path / "local").glob("recovery-*.json"))) == 1
    local.validate_local_evidence(evidence, tmp_path / "local", IDENTITY)


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


def test_recurrence_seeds_carry_casing_hyphenation_and_skip_sentence_capitals():
    original = {"p1": "the god of war and a band-aid on it. he ran. yesterday he left. a grown up man.",
                "p2": "unchanged."}
    current = {"p1": "the God of war and a Band-Aid on it. he ran. Yesterday he left. a grown-up man.",
               "p2": "unchanged."}
    seeds = local._recurrence_seeds(original, current, set())
    assert [(s.anchor.delete_text, s.anchor.insert_text) for s in seeds] == [
        ("god", "God"), ("band-aid", "Band-Aid"), ("grown up", "grown-up")]
    assert all(current["p1"][s.anchor.start:s.anchor.end] == s.anchor.insert_text for s in seeds)


def test_completion_propagates_a_casing_decision_to_exact_case_sites(tmp_path):
    source = prepared(para("p1", "She said thank god for the storm."),
                      para("p2", "A god among men, he said. God knows."),
                      para("p3", "GOD is great, she said."))
    original = {p.para_id: p.text for p in source.doc.paragraphs}
    current = {**original, "p1": "She said thank God for the storm."}
    rows, evidence = local.collect_completion_candidates(source, original, current, tmp_path / "local",
        identity=IDENTITY, stage="casing")
    recurrence = [r for r in rows if r["source"] == "local:completion:recurrences"]
    assert len(recurrence) == 1 and recurrence[0]["para_id"] == "p2" and recurrence[0]["action"] == "edit"
    assert "A God among men" in recurrence[0]["replacement"]
    assert 'changed to "God" at 1 other site(s)' in recurrence[0]["reason"]
    assert packet(evidence)["casing_seed_keys"] == ["god"]


def test_completion_seed_suppresses_the_case_split_scan_for_that_term(tmp_path):
    lower = [para(f"l{i}", f"Sentence {i} about easy speed on the water.") for i in range(6)]
    upper = [para(f"u{i}", f"Sentence {i} about Easy Speed on the water.") for i in range(2)]
    source = prepared(*lower, *upper)
    original = {p.para_id: p.text for p in source.doc.paragraphs}
    current = {**original, "l0": "Sentence 0 about Easy Speed on the water."}
    rows, evidence = local.collect_completion_candidates(source, original, current, tmp_path / "local",
        identity=IDENTITY, stage="seeded")
    assert not [r for r in rows if r["category"] == "case_split"]
    assert packet(evidence)["casing_seed_keys"] == ["easy speed"]
    recurrence = [r for r in rows if r["source"] == "local:completion:recurrences"]
    assert {r["para_id"] for r in recurrence} == {f"l{i}" for i in range(1, 6)}
    assert all("Easy Speed" in r["replacement"] for r in recurrence)


def test_a_term_changed_two_ways_sweeps_nowhere_and_asks_instead(tmp_path):
    """Cooper, 2026-09-17: “lamb chops” became “muttonchops” in one paragraph
    while “lambchops” was closed up to “lamb chops” in others. Neither swap is
    evidence for the other, so the term propagates nowhere and every site asks."""
    source = prepared(para("p1", "He ordered lamb chops for dinner."),
                      para("p2", "The lambchops were cold."),
                      para("p3", "She never ordered lambchops."))
    original = {p.para_id: p.text for p in source.doc.paragraphs}
    current = {**original, "p1": "He ordered muttonchops for dinner.",
               "p2": "The lamb chops were cold."}
    seeds = local._recurrence_seeds(original, current, set())
    kept, conflicts = local._swap_conflicts(seeds)
    assert len(seeds) == 2 and kept == []
    assert [c["key"] for c in conflicts] == ["lambchops"]
    assert [s["para_id"] for s in conflicts[0]["sites"]] == ["p1", "p2"]
    rows, evidence = local.collect_completion_candidates(source, original, current,
        tmp_path / "local", identity=IDENTITY, stage="conflict")
    # No propagation to the untouched third site, in either direction.
    assert not [r for r in rows if r["source"] == "local:completion:recurrences"]
    conflict_rows = [r for r in rows if r["source"] == "local:completion:swap_conflicts"]
    assert {r["para_id"] for r in conflict_rows} == {"p1", "p2"}
    assert all(r["action"] == "query" and r["category"] == "spelling" for r in conflict_rows)
    assert all(r["replacement"] == r["quote"] for r in conflict_rows)
    reason = conflict_rows[0]["reason"]
    assert "“lamb chops”/“lambchops” was changed" in reason
    assert "to “muttonchops” at p1" in reason and "to “lamb chops” at p2" in reason
    assert "one term must take one form" in reason
    assert packet(evidence)["swap_conflicts"][0]["key"] == "lambchops"


def test_one_form_of_a_swap_still_propagates(tmp_path):
    """Only a conflicted term stops sweeping; the ordinary case is unchanged."""
    source = prepared(para("p1", "He ordered lambchops for dinner."),
                      para("p2", "The lambchops were cold."))
    original = {p.para_id: p.text for p in source.doc.paragraphs}
    current = {**original, "p1": "He ordered lamb chops for dinner."}
    kept, conflicts = local._swap_conflicts(local._recurrence_seeds(original, current, set()))
    assert len(kept) == 1 and conflicts == []
    rows, evidence = local.collect_completion_candidates(source, original, current,
        tmp_path / "local", identity=IDENTITY, stage="single")
    recurrence = [r for r in rows if r["source"] == "local:completion:recurrences"]
    assert [r["para_id"] for r in recurrence] == ["p2"]
    assert "lamb chops" in recurrence[0]["replacement"]
    assert not [r for r in rows if r["source"] == "local:completion:swap_conflicts"]
    assert packet(evidence)["swap_conflicts"] == []


def test_two_homophone_fixes_of_one_grammar_word_are_not_a_conflict():
    """“their” corrected two ways is two readings of two sentences."""
    original = {"p1": "their goes the bell.", "p2": "their coming home."}
    current = {"p1": "there goes the bell.", "p2": "they're coming home."}
    kept, conflicts = local._swap_conflicts(local._recurrence_seeds(original, current, set()))
    assert conflicts == [] and len(kept) == 2


def test_verse_packet_runs_the_glyph_sweeps_over_poetry_only(tmp_path, monkeypatch):
    """The verse sweep touches only the classified poetry, runs no
    LanguageTool, dictionary or sentence-level check, and records its own
    check name so certification can tell it from the prose scan."""
    from galley.fixed_policy import configuration
    source = prepared(para("p1", "She waited -- and waited."),
                      para("verse", "teh Moon\n  waits -- quiet\n‘bout now\n‘We”"))
    # The prose scan still excludes the poem and never sees the verse sweep's checks.
    prose, prose_evidence = collect(tmp_path, source, poetry_ids={"verse"})
    assert prose_evidence["paragraph_ids"] == ["p1"] and prose_evidence["verse_ids"] == []
    monkeypatch.setattr(local, "_dictionary_rows", lambda *a, **k: pytest.fail("Dictionary generator started"))
    monkeypatch.setattr(local, "_language_tool", lambda *a, **k: pytest.fail("LanguageTool started"))
    texts = {p.para_id: p.text for p in source.doc.paragraphs}
    findings, evidence = local.collect_verse_candidates(source, texts, tmp_path / "local", identity=IDENTITY,
                                                        verse_ids={"verse"}, cfg=configuration(poetry=True))
    assert evidence["stage"] == "verse" and evidence["verse_ids"] == ["verse"]
    assert evidence["paragraph_ids"] == ["verse"] and evidence["excluded_poetry_ids"] == []
    assert [c["check"] for c in evidence["checks"]] == ["verse_sweeps"]
    assert all(f["para_id"] == "verse" and f["source"] == "local:verse" for f in findings)
    assert {f["category"] for f in findings} == {"sweep_dash", "sweep_elision_apostrophe", "sweep_quote_pair"}
    assert local.validate_local_evidence(evidence, tmp_path / "local", IDENTITY)["request"]["verse_ids"] == ["verse"]


# --- chapter and part labels are mechanics (Wilder running head, 2026-09-17) --

def _label_rows(*paragraphs):
    return local._chapter_label_rows(list(paragraphs), configuration())


def _header(pid, text):
    return ParagraphRef(pid, "word/header5.xml", "header", text, "Header")


def _chapters(*labels):
    """Heading lines with a body paragraph under each, as a real book has."""
    out = []
    for i, label in enumerate(labels):
        out += [para(f"h{i}", label, style="Heading1"), para(f"b{i}", f"Body of chapter {i}.")]
    return out


def test_running_head_label_is_restyled_to_the_body_sequence_and_keeps_its_case():
    body = _chapters(*(f"CHAPTER {n}" for n in range(2, 6)))
    rows = _label_rows(_header("rh", "CHAPTER ONE"), *body)
    [row] = rows
    assert row["para_id"] == "rh" and row["category"] == "chapter_label" and row["action"] == "edit"
    assert row["replacement"] == "CHAPTER 1" and row["source"] == "local:chapter_labels"
    assert "mechanics" in row["reason"]
    # Title case in the head is the typesetter's; only the number form moves.
    [row] = _label_rows(_header("rh", "Chapter One"), *body)
    assert row["replacement"] == "Chapter 1"


def test_running_head_that_matches_the_body_form_proposes_nothing():
    body = _chapters(*(f"CHAPTER {n}" for n in range(2, 6)))
    assert _label_rows(_header("rh", "CHAPTER 1"), *body) == []
    assert _label_rows(_header("rh", "12 | ANA AND ATLAS"), *body) == []


def test_body_sequence_is_renumbered_in_its_dominant_style():
    body = _chapters("Chapter Fifteen", "Chapter Seventeen", "Chapter Eighteen", "Chapter Nineteen",
                     "Chapter Twenty-One", "Chapter Twenty-Thirty")
    rows = _label_rows(*body)
    assert [(r["para_id"], r["replacement"]) for r in rows] == [
        ("h1", "Chapter Sixteen"), ("h2", "Chapter Seventeen"), ("h3", "Chapter Eighteen"),
        ("h4", "Chapter Nineteen"), ("h5", "Chapter Twenty")]
    assert all(r["category"] == "chapter_label" for r in rows)


def test_a_contents_list_and_a_lone_label_never_drive_renumbering():
    toc = [para(f"t{n}", f"CHAPTER {n}", style="Heading1") for n in (1, 2, 3)]
    body = _chapters("CHAPTER 1", "CHAPTER 2", "CHAPTER 3")
    assert _label_rows(*toc, *body) == []
    # With the list in place, a running head still follows the real headings.
    [row] = _label_rows(_header("rh", "CHAPTER ONE"), *toc, *body)
    assert row["para_id"] == "rh" and row["replacement"] == "CHAPTER 1"
    assert _label_rows(para("h1", "Chapter Two", style="Heading1"), para("b1", "Only one label.")) == []
    # A prose line that happens to open with a label word is not a label.
    assert _label_rows(para("p1", "Chapter three of the report says the bridge failed in the storm of that year."),
                       *_chapters("CHAPTER 2", "CHAPTER 3")) == []


def test_initial_and_completion_scans_carry_the_chapter_label_check(tmp_path):
    body = _chapters(*(f"CHAPTER {n}" for n in range(2, 5)))
    source = prepared(_header("rh", "CHAPTER ONE"), *body, para("p1", "She waited."))
    rows, evidence = collect(tmp_path, source)
    saved = packet(evidence)
    assert {c["check"] for c in saved["checks"]} >= {"chapter_labels", "normalization_and_speakers"}
    assert [r["replacement"] for r in rows if r["category"] == "chapter_label"] == ["CHAPTER 1"]
    texts = {p.para_id: p.text for p in source.doc.paragraphs}
    rows, evidence = local.collect_completion_candidates(source, texts, dict(texts), tmp_path / "local",
                                                          identity=IDENTITY, stage="completion")
    assert [r["replacement"] for r in rows if r["category"] == "chapter_label"] == ["CHAPTER 1"]
    assert "chapter_labels" in {c["check"] for c in packet(evidence)["checks"]}
