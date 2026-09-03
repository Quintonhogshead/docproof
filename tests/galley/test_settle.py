"""galley/settle.py — residual settlement end to end, on real .docx builds.

Every test builds a run the way a Galley run does (a $0 mock review or an
import-findings replay), plants verify artifacts by hand (what `galley verify`
would have written), runs `galley settle`, and checks the deliverable, the
settlement record, and certify. No model call is made: the deterministic lane
settles what it can and a scripted provider stands in for the judge / delta
verifier where a test needs one.
"""
from __future__ import annotations

import json
from pathlib import Path

import docx
import yaml

import docproof.__main__ as m
from docproof.__main__ import main
from docproof.config import load_config
from docproof.ingest import build_document_model, preflight
from docproof.providers import NormalizedUsage, ProviderResult
from galley.manifest import certify_run
from galley.settle import Settlement, open_items, terminal_state
from galley.verify import paragraph_views, problem_id, residual_id

CONFIG = str(Path("config/default.yaml"))

PARAGRAPHS = [
    "The lamp on teh desk flickered, then finally caught, and Maria sighed.",
    "Its light was thin, but it was enough to recieve the letter by tonight.",
    "David counted the coins twice, sure the total would not change again.",
    "The garden gate creaked open, and the old dog limped out to greet her.",
    "She wrote Mom a note and left it under the lamp before she slept.",
]


class _Provider:
    """Replays one scripted parsed body per call; records every call."""

    def __init__(self, *bodies):
        self._bodies = list(bodies)
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        body = self._bodies.pop(0) if self._bodies else {}
        return ProviderResult(parsed=body, usage=NormalizedUsage(10, 5),
                              stop_reason="ok")


def _manuscript(tmp_path, paragraphs=PARAGRAPHS, name="book.docx"):
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    src = tmp_path / name
    d.save(src)
    return src


def _para_ids(src):
    cfg = load_config(CONFIG)
    doc = build_document_model(preflight(str(src), cfg.tracked_changes_policy),
                               cfg)
    return [p.para_id for p in doc.paragraphs], doc


def _replay_config(tmp_path, **extra):
    """A $0 rebuild config: the shipped defaults with no typed passes and
    every paid stage off (what final-replay materializes)."""
    cfg = yaml.safe_load(Path(CONFIG).read_text("utf-8"))
    cfg["error_types"] = []
    for k in ("languagetool", "spellcheck", "consistency", "glossary",
              "residuals", "meaning_check", "fix_check", "factcheck",
              "continuity", "chapter_continuity", "chapter_sweep",
              "smoothing", "rewrite", "repair", "storysheet"):
        cfg.setdefault(k, {})
        if isinstance(cfg[k], dict):
            cfg[k]["enabled"] = False
    cfg["ensemble"] = {"detectors": [], "verify_policy": "none"}
    cfg["candidate_screening"] = {"mode": "off"}
    cfg.update(extra)
    path = tmp_path / "replay.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(path)


def _build(tmp_path, src, rows, out="run"):
    """A finished build from import-findings rows: findings.json, the docx,
    editmap.json."""
    rows_path = tmp_path / "rows.json"
    rows_path.write_text(json.dumps(rows), encoding="utf-8")
    run = tmp_path / out
    rc = main(["import-findings", str(rows_path), str(src), "--config",
               _replay_config(tmp_path), "--out", str(run)])
    assert rc == 0
    assert (run / "editmap.json").exists()
    return run


def _walk(run, residuals, problems=()):
    (run / "finished_walk.json").write_text(json.dumps({
        "generated_at": "2099-01-01T00:00:00+00:00", "ran": True,
        "residuals": list(residuals)}), encoding="utf-8")
    (run / "change_verify.json").write_text(json.dumps({
        "generated_at": "2099-01-01T00:00:00+00:00", "ran": True,
        "applied_edits": 0, "problems": list(problems)}), encoding="utf-8")


def _settle(tmp_path, run, src, *extra):
    return main(["galley", "settle", str(run), "--source", str(src),
                 "--config", _replay_config(tmp_path), "--engine", "none",
                 "--no-verify", *extra])


def _accepted(run):
    return paragraph_views(run)[1]


def _records(run):
    st = Settlement.load(run)
    assert st is not None
    return {r.residual_id: r for r in st.latest().values()}, st


# --- T1: every residual class settles to a terminal state --------------------

def test_settle_closes_owned_unowned_zone_note_and_fact_residuals(tmp_path):
    src = _manuscript(tmp_path)
    ids, doc = _para_ids(src)
    p0, p1, p2, p4 = ids[0], ids[1], ids[2], ids[4]
    # An applied edit that is itself wrong (teh -> thee) owns a span in p0.
    run = _build(tmp_path, src, [
        {"para_id": p0, "original_text": "teh", "corrected_text": "thee",
         "explanation": "typo", "confidence": "high"},
    ])
    zones = tmp_path / "zones.json"
    zones.write_text(json.dumps({"zones": [
        {"label": "Mom caps", "category": "declared_caps",
         "permission": "locked", "terms": ["Mom"]}]}), encoding="utf-8")
    acc = _accepted(run)
    assert "thee desk" in acc[p0]
    _walk(run, [
        # (a) inside an owned span: the owner's replacement is revised
        {"para_id": p0, "quote": "thee", "problem": "wrong word",
         "suggestion": "the", "severity": "high"},
        # (b) untouched text: a new edit, with an editorial note stripped
        {"para_id": p1, "quote": "recieve", "problem": "misspelling",
         "suggestion": "receive (spell correctly)", "severity": "high"},
        # (c) a fact change: a query, never an edit
        {"para_id": p2, "quote": "coins twice", "problem": "count?",
         "suggestion": "coins 3 times", "severity": "low"},
        # (d) inside a locked intent zone: dropped
        {"para_id": p4, "quote": "Mom", "problem": "capitalization",
         "suggestion": "mom", "severity": "medium"},
        # (e) a quote the accepted text does not hold: dropped(unanchorable)
        {"para_id": p1, "quote": "zzzz", "problem": "?", "suggestion": "y",
         "severity": "low"},
    ])
    cfgpath = _replay_config(tmp_path, intent_zones_file=str(zones))
    rc = main(["galley", "settle", str(run), "--source", str(src), "--config",
               cfgpath, "--engine", "none", "--no-verify"])
    assert rc == 0

    recs, st = _records(run)
    assert recs[residual_id(p0, "thee")].action == "absorb"
    assert recs[residual_id(p1, "recieve")].action == "add"
    q = recs[residual_id(p2, "coins twice")]
    assert q.action == "query" and q.reason.startswith("fact:")
    z = recs[residual_id(p4, "Mom")]
    assert z.action == "drop" and z.reason.startswith("intent_zone")
    u = recs[residual_id(p1, "zzzz")]
    assert u.action == "drop" and u.reason == "unanchorable"
    assert st.open == [] and st.rounds == 1

    # the deliverable: composite revised, new edit landed, zone untouched
    acc = _accepted(run)
    assert "the desk" in acc[p0] and "thee" not in acc[p0]
    assert "receive" in acc[p1] and "spell correctly" not in acc[p1]
    assert "coins twice" in acc[p2]
    assert "Mom" in acc[p4]
    # I6: reject-all is the source, byte for byte
    original = paragraph_views(run)[0]
    for p in doc.paragraphs:
        assert original[p.para_id] == p.text
    # the fact query reached the margin
    env = json.loads((run / "findings.json").read_text("utf-8"))
    queries = [r for r in env["findings"] if r.get("queried")]
    assert any(r["para_id"] == p2 and "3 times" in r["explanation"]
               for r in queries)
    # I1: every row terminal, and stamped
    assert all(r.get("state") in ("applied", "dropped", "query")
               for r in env["findings"])
    composite = [r for r in env["findings"]
                 if r.get("chunk_id") == f"settle:{residual_id(p0, 'thee')}"
                 and r.get("applied")]
    assert composite and composite[0]["error_type"] == "galley_settle"
    assert composite[0]["absorbed"]
    # nothing is open; certify's settlement checks pass
    assert open_items(run) == []
    cert = certify_run(run)
    by = {c.name: c for c in cert.checks}
    assert by["residual settlement"].status == "pass"
    assert by["finished-text walk"].status == "pass"
    assert by["change verifier"].status == "pass"
    assert by["terminal states"].status == "pass"
    assert by["outcome"].status == "pass" and "done" in by["outcome"].detail
    assert (run / "outcome.json").exists()
    oc = json.loads((run / "outcome.json").read_text("utf-8"))
    assert oc["outcome"] == "done" and oc["hubspot"]["property"] == "docproof"


# --- edit damage: the change verifier's own items ------------------------------

def test_settle_reverts_a_flagged_edit_and_revises_another(tmp_path):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    p0, p1 = ids[0], ids[1]
    run = _build(tmp_path, src, [
        {"para_id": p0, "original_text": "teh", "corrected_text": "tea",
         "confidence": "high"},                                # voice damage
        {"para_id": p1, "original_text": "recieve", "corrected_text": "recieves",
         "confidence": "high"},                                # still wrong
    ])
    _walk(run, [], [
        {"para_id": p0, "original_text": "teh", "corrected_text": "tea",
         "verdict": "breaks_meaning", "detail": "not a drink", "fix": "teh"},
        {"para_id": p1, "original_text": "recieve", "corrected_text": "recieves",
         "verdict": "breaks_grammar", "detail": "misspelled", "fix": "receive"},
    ])
    assert _settle(tmp_path, run, src) == 0
    recs, st = _records(run)
    a = recs[problem_id(p0, "teh", "tea")]
    assert a.action == "drop" and a.reason.startswith("edit_damage")
    b = recs[problem_id(p1, "recieve", "recieves")]
    assert b.action == "revise" and b.after_replacement == "receive"
    acc = _accepted(run)
    assert "teh desk" in acc[p0]                # reverted to the source
    assert "receive the letter" in acc[p1]      # revised in place
    cert = certify_run(run)
    by = {c.name: c for c in cert.checks}
    assert by["change verifier"].status == "pass"
    assert by["residual settlement"].status == "pass"


# --- T3: a residual inside a repair cluster member keeps the cluster ----------

def test_settle_keeps_a_cluster_applied_when_one_member_is_revised(tmp_path):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    p0 = ids[0]
    run = _build(tmp_path, src, [
        {"para_id": p0, "original_text": "teh", "corrected_text": "thee",
         "confidence": "high", "cluster_id": "rep-1"},
        {"para_id": p0, "original_text": "sighed", "corrected_text": "sighs",
         "confidence": "high", "cluster_id": "rep-1"},
    ])
    acc = _accepted(run)
    assert "thee desk" in acc[p0] and "sighs" in acc[p0]
    _walk(run, [{"para_id": p0, "quote": "thee", "problem": "wrong word",
                 "suggestion": "the", "severity": "high"}])
    assert _settle(tmp_path, run, src) == 0
    recs, _st = _records(run)
    assert recs[residual_id(p0, "thee")].action == "absorb"
    acc = _accepted(run)
    assert "the desk" in acc[p0] and "sighs" in acc[p0]    # sibling stayed
    env = json.loads((run / "findings.json").read_text("utf-8"))
    applied = [r for r in env["findings"] if r.get("applied")]
    assert {r["cluster_id"] for r in applied} == {"rep-1"}
    assert len(applied) == 2


# --- T4: bounded rounds — what cannot be decided ships as a question ---------

def test_settle_turns_an_undecidable_residual_into_a_query(tmp_path):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    p2 = ids[2]
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    _walk(run, [{"para_id": p2, "quote": "sure the total", "problem":
                 "garbled — two plausible repairs", "suggestion": "",
                 "severity": "medium"}])
    assert _settle(tmp_path, run, src, "--rounds", "1") == 0
    recs, st = _records(run)
    r = recs[residual_id(p2, "sure the total")]
    assert r.action == "query" and r.question
    assert st.open == []
    env = json.loads((run / "findings.json").read_text("utf-8"))
    assert any(row.get("queried") and row["para_id"] == p2
               and "two plausible repairs" in row["explanation"]
               for row in env["findings"])
    # state machine: settled is reachable now, and refuses while open
    ws = tmp_path / "ws"
    ws.mkdir()
    assert main(["galley", "state", str(ws), "--advance", "intake"]) == 0
    assert main(["galley", "state", str(ws), "--advance", "settled",
                 "--results", str(run)]) == 0


def test_state_refuses_settled_while_items_are_open(tmp_path):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    _walk(run, [{"para_id": ids[1], "quote": "recieve", "problem": "sp",
                 "suggestion": "receive", "severity": "high"}])
    ws = tmp_path / "ws"
    ws.mkdir()
    assert main(["galley", "state", str(ws), "--advance", "intake"]) == 0
    assert main(["galley", "state", str(ws), "--advance", "settled",
                 "--results", str(run)]) == 7
    cert = certify_run(run)
    by = {c.name: c for c in cert.checks}
    assert by["residual settlement"].status == "fail"
    assert by["finished-text walk"].status == "fail"


# --- T5: an unanchorable walker row is recorded, never silently lost ---------

def test_unanchorable_rows_are_recorded_as_dropped(tmp_path):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    _walk(run, [{"para_id": ids[3], "quote": "not in the text", "problem": "?",
                 "suggestion": "x", "severity": "low"},
                {"para_id": "body-9999", "quote": "gate", "problem": "?",
                 "suggestion": "x", "severity": "low"}])
    assert _settle(tmp_path, run, src) == 0
    recs, _st = _records(run)
    assert recs[residual_id(ids[3], "not in the text")].reason == "unanchorable"
    assert recs[residual_id("body-9999", "gate")].action == "drop"
    fw = json.loads((run / "finished_walk.json").read_text("utf-8"))
    assert {r["settled"] for r in fw["residuals"]} == {"drop"}
    assert fw["unsettled"] == []


# --- T2: the delta verifier flags a composite -> revert + query ---------------

def test_a_verifier_flag_on_a_composite_reverts_it_to_a_query(tmp_path,
                                                             monkeypatch):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    p0 = ids[0]
    run = _build(tmp_path, src, [
        {"para_id": p0, "original_text": "teh", "corrected_text": "thee",
         "confidence": "high"}])
    _walk(run, [{"para_id": p0, "quote": "thee", "problem": "wrong word",
                 "suggestion": "the", "severity": "high"}])
    # round 1 delta verify: the change verifier flags the composite (index 1
    # of the paragraph's applied edits), the walk is clean; nothing after.
    prov = _Provider(
        {"problems": [{"index": 1, "verdict": "voice_damage",
                       "detail": "author wrote thee on purpose",
                       "fix": "thee"}]},
        {"findings": []})
    monkeypatch.setattr(m, "_resolve_engine",
                        lambda args, cfg, default_model=None:
                        ("provider", prov, "fake-model"))
    rc = main(["galley", "settle", str(run), "--source", str(src),
               "--config", _replay_config(tmp_path), "--engine", "provider"])
    assert rc == 0
    recs, st = _records(run)
    r = recs[residual_id(p0, "thee")]
    assert r.action == "query" and r.reason == "verifier_reverted"
    acc = _accepted(run)
    assert "thee desk" in acc[p0]               # the owner's edit stands
    env = json.loads((run / "findings.json").read_text("utf-8"))
    assert any(row.get("queried") and row["para_id"] == p0
               for row in env["findings"])
    assert len(prov.calls) == 2
    assert certify_run(run).checks and {
        c.name: c.status for c in certify_run(run).checks
    }["residual settlement"] == "pass"


# --- the judge: a residual with no suggestion, decided by the model ----------

def test_the_judge_settles_a_residual_with_no_suggestion(tmp_path, monkeypatch):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    p1 = ids[1]
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    _walk(run, [{"para_id": p1, "quote": "recieve", "problem": "misspelling",
                 "suggestion": "", "severity": "high"}])
    prov = _Provider(
        {"action": "add", "replacement": "receive", "reason": "", "question": ""},
        {"problems": []},                                    # delta verify
        {"findings": []})
    monkeypatch.setattr(m, "_resolve_engine",
                        lambda args, cfg, default_model=None:
                        ("provider", prov, "fake-model"))
    rc = main(["galley", "settle", str(run), "--source", str(src),
               "--config", _replay_config(tmp_path), "--engine", "provider"])
    assert rc == 0
    recs, _st = _records(run)
    assert recs[residual_id(p1, "recieve")].action == "add"
    assert "receive the letter" in _accepted(run)[p1]
    assert "FLAGGED SPAN: 'recieve'" in prov.calls[0]["user"]


# --- residuals / outcome verbs -------------------------------------------------

def test_residuals_verb_lists_open_items_with_owners(tmp_path, capsys):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "thee",
         "confidence": "high"}])
    _walk(run, [{"para_id": ids[0], "quote": "thee", "problem": "w",
                 "suggestion": "the", "severity": "high"}])
    assert main(["galley", "residuals", str(run), "--source", str(src),
                 "--config", _replay_config(tmp_path), "--json"]) == 0
    out = capsys.readouterr().out
    assert "1 open item(s)" in out
    line = [l for l in out.splitlines() if l.startswith("{")][-1]
    item = json.loads(line)["open"][0]
    assert item["owner_finding_id"] and item["resolution"] == "resolvable"


def test_outcome_needs_human_when_most_paragraphs_need_rewrite(tmp_path, capsys):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    # three edits in four of five paragraphs -> rewrite share 80%
    rows = []
    for pid, words in ((ids[0], ("lamp", "desk", "caught")),
                       (ids[1], ("light", "thin", "letter")),
                       (ids[2], ("coins", "total", "change")),
                       (ids[3], ("gate", "dog", "greet"))):
        for w in words:
            rows.append({"para_id": pid, "original_text": w,
                         "corrected_text": w + "s", "confidence": "high"})
    run = _build(tmp_path, src, rows)
    assert main(["galley", "outcome", str(run), "--json"]) == 0
    out = capsys.readouterr().out
    oc = json.loads([l for l in out.splitlines() if l.startswith("{")][-1])
    assert oc["outcome"] == "needs_human"
    assert "rewrite-class" in oc["reason"]
    assert oc["hubspot"]["value"] == "Needs Human Proofreader"
    # a human overrule is recorded as such
    assert main(["galley", "outcome", str(run), "--set", "done",
                 "--reason", "reviewed by hand"]) == 0
    assert json.loads((run / "outcome.json").read_text("utf-8"))["set_by"] \
        == "human"


# --- import-findings --anchor accepted -----------------------------------------

def test_import_findings_anchor_accepted_revises_the_owner(tmp_path):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    p0, p1 = ids[0], ids[1]
    run = _build(tmp_path, src, [
        {"para_id": p0, "original_text": "teh", "corrected_text": "thee",
         "confidence": "high"}])
    rows = tmp_path / "accepted_rows.json"
    rows.write_text(json.dumps([
        {"para_id": p0, "quote": "thee", "replacement": "the",
         "explanation": "wrong word"},
        {"para_id": p1, "quote": "recieve", "replacement": "receive"},
    ]), encoding="utf-8")
    out = tmp_path / "run2"
    rc = main(["import-findings", str(rows), str(src), "--anchor", "accepted",
               "--run", str(run), "--config", _replay_config(tmp_path),
               "--out", str(out)])
    assert rc == 0
    acc = _accepted(out)
    assert "the desk" in acc[p0] and "receive the letter" in acc[p1]
    original = paragraph_views(out)[0]
    assert "teh desk" in original[p0]           # reject-all is the source


# --- guards learned on the Redding trial ---------------------------------------

def test_instruction_shaped_suggestions_never_become_text(tmp_path):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    _walk(run, [{"para_id": ids[3], "quote": "The garden gate",
                 "problem": "leading space", "suggestion":
                 "Delete the trailing whitespace.", "severity": "low"}])
    assert _settle(tmp_path, run, src) == 0
    recs, _st = _records(run)
    r = recs[residual_id(ids[3], "The garden gate")]
    assert r.action == "query" and r.reason == "instruction"
    assert "Delete the trailing" not in _accepted(run)[ids[3]]


def test_a_whole_sentence_suggestion_replaces_the_sentence_not_the_fragment(
        tmp_path):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    p2 = ids[2]
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    # quote is a fragment; the suggestion rewrites the whole sentence
    _walk(run, [{"para_id": p2, "quote": "sure the total would not change",
                 "problem": "wordy", "suggestion":
                 "David counted the coins twice, certain the total would not "
                 "change.", "severity": "low"}])
    assert _settle(tmp_path, run, src) == 0
    acc = _accepted(run)[p2]
    assert acc.startswith("David counted the coins twice, certain the total")
    assert acc.count("David counted") == 1


def test_settle_queries_are_never_collapsed_into_one_comment(tmp_path):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    # three undecidable residuals in three paragraphs -> three questions
    _walk(run, [{"para_id": ids[i], "quote": q, "problem": f"q{i}",
                 "suggestion": "", "severity": "medium"}
                for i, q in ((1, "Its light"), (2, "the coins"),
                             (3, "old dog"))])
    assert _settle(tmp_path, run, src) == 0
    env = json.loads((run / "findings.json").read_text("utf-8"))
    assert sum(1 for r in env["findings"] if r.get("queried")
               and r["error_type"] == "galley_settle") == 3


def test_case_only_change_inside_quoted_speech_is_not_a_fact_change():
    from galley.settle import _fact
    assert _fact('“just be careful,” she said', '“Just be careful,” she said') \
        is None
    assert _fact("my friend's luck", "Ava's luck") is not None
    assert _fact("one 12 minute talk", "one 21 minute talk") is not None


def test_a_suggestion_that_ships_a_house_artifact_is_queried(tmp_path):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    # British punctuation: the period outside the closing quote
    _walk(run, [{"para_id": ids[1], "quote": "the letter by tonight.",
                 "problem": "quote it", "suggestion": "“the letter by tonight”.",
                 "severity": "low"}])
    assert _settle(tmp_path, run, src) == 0
    recs, _st = _records(run)
    r = recs[residual_id(ids[1], "the letter by tonight.")]
    assert r.action == "query" and r.reason.startswith("artifact:")
    assert "”." not in _accepted(run)[ids[1]]


def test_typographic_parentheticals_are_notes_not_text():
    from galley.settle import strip_note
    assert strip_note("the Rocky theme (with Rocky in italics)", "the Rocky theme") \
        == ("the Rocky theme", True)
    # the author's own parenthetical, present in the quote, survives
    q = "my husband (a comma fan) laughed"
    assert strip_note(q, q) == (q, False)


def test_deleting_a_leaked_editorial_aside_is_not_a_fact_change():
    from galley.settle import _fact, deletes_an_aside
    before = ('actresses who we were going to be when we grew up (parallel '
              'with "were going to be"; breaks tense agreement as written)')
    after = 'actresses who we were going to be when we grew up'
    assert deletes_an_aside(before, after)
    assert _fact(before, after) is None
    # the author's own parenthetical is not an aside
    assert not deletes_an_aside("we left (all three of us) at noon",
                                "we left at noon")


def test_restore_rows_never_overwrites_a_current_row():
    from galley.settle import restore_rows
    # after a rebuild "f-0003" names an unrelated row; the old owner that
    # was absorbed also used to be "f-0003"
    working = {"f-0003": {"para_id": "p9", "original_text": "keep me"}}
    removed = {"f-0003": {"para_id": "p1", "original_text": "old owner"}}
    assert restore_rows(working, "r-abc", removed.values()) == 1
    assert working["f-0003"]["original_text"] == "keep me"
    assert any(v["original_text"] == "old owner" for v in working.values())


# --- propagation: the same fix at identical untouched sites nearby ----------

def test_a_settled_fix_propagates_to_the_same_surface_nearby(tmp_path):
    paras = [
        "I will recieve the letter, and you will recieve the reply.",
        "Nobody could recieve it in time, though formal receipts came later.",
        "The end.",
    ]
    src = _manuscript(tmp_path, paras)
    ids, _doc = _para_ids(src)
    run = _build(tmp_path, src, [
        {"para_id": ids[2], "original_text": "end", "corrected_text": "End",
         "confidence": "high"}])
    _walk(run, [{"para_id": ids[0], "quote": "recieve", "problem": "sp",
                 "suggestion": "receive", "severity": "high"}])
    assert _settle(tmp_path, run, src) == 0
    acc = _accepted(run)
    assert acc[ids[0]].count("receive") == 2 and "recieve" not in acc[ids[0]]
    assert "could receive it" in acc[ids[1]]            # the neighbour too
    assert "formal receipts" in acc[ids[1]]             # word boundary held
    recs, _st = _records(run)
    prop = [r for r in recs.values() if r.reason.startswith("propagated:")]
    assert len(prop) == 2 and {r.action for r in prop} == {"add"}


# --- --until-clean: keep sweeping while rounds find work, stop when quiet --

def _fake_engine(monkeypatch, prov):
    monkeypatch.setattr(m, "_resolve_engine",
                        lambda args, cfg, default_model=None:
                        ("provider", prov, "fake-model"))


def test_until_clean_stops_after_a_quiet_round(tmp_path, monkeypatch):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    _walk(run, [{"para_id": ids[1], "quote": "recieve", "problem": "sp",
                 "suggestion": "receive", "severity": "high"}])
    # round 1 verify: clean changes, walk raises ONE new item (quiet) ->
    # round 2 settles it, its verify is clean -> stop. Never a third round.
    prov = _Provider(
        {"problems": []},
        {"findings": [{"para_id": ids[1], "quote": "was thin",
                       "problem": "x", "suggestion": "was faint",
                       "severity": "low"}]},
        {"problems": []}, {"findings": []})
    _fake_engine(monkeypatch, prov)
    rc = main(["galley", "settle", str(run), "--source", str(src), "--config",
               _replay_config(tmp_path), "--engine", "provider",
               "--until-clean"])
    assert rc == 0
    _recs, st = _records(run)
    assert st.rounds == 2 and st.open == []
    assert any("quiet" in n for n in st.notes)


def test_until_clean_respects_the_turn_budget(tmp_path, monkeypatch):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    _walk(run, [{"para_id": ids[1], "quote": "recieve", "problem": "sp",
                 "suggestion": "receive", "severity": "high"}])
    # every verify keeps raising a fresh item; the budget of 2 turns ends it
    # after round 1 and the leftover ships as a question.
    prov = _Provider(
        {"problems": []},
        {"findings": [{"para_id": ids[1], "quote": "Its light",
                       "problem": "x", "suggestion": "Its glow",
                       "severity": "low"},
                      {"para_id": ids[1], "quote": "was thin",
                       "problem": "y", "suggestion": "was faint",
                       "severity": "low"},
                      {"para_id": ids[1], "quote": "was enough",
                       "problem": "z", "suggestion": "was sufficient",
                       "severity": "low"},
                      {"para_id": ids[1], "quote": "by tonight",
                       "problem": "w", "suggestion": "by nightfall",
                       "severity": "low"}]})
    _fake_engine(monkeypatch, prov)
    rc = main(["galley", "settle", str(run), "--source", str(src), "--config",
               _replay_config(tmp_path), "--engine", "provider",
               "--until-clean", "--max-turns", "2"])
    assert rc == 0
    recs, st = _records(run)
    assert st.rounds == 2 and st.open == []
    assert any("turn budget" in n for n in st.notes)
    assert sum(1 for r in recs.values()
               if r.reason.startswith("unresolved_after_")) == 4


def test_a_sweep_that_will_not_converge_flags_needs_human(tmp_path, monkeypatch):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    _walk(run, [{"para_id": ids[1], "quote": "recieve", "problem": "sp",
                 "suggestion": "receive", "severity": "high"}])
    # the walk raises four new items every round; with a 2-turn budget the
    # sweep stops noisy -> the outcome says a human proofreader is needed
    def noisy(pairs):
        return {"findings": [{"para_id": ids[1], "quote": q, "problem": "x",
                              "suggestion": r, "severity": "low"}
                             for q, r in pairs]}
    prov = _Provider(
        {"problems": []},
        noisy((("Its light", "Its glow"), ("was thin", "was faint"),
               ("was enough", "was sufficient"), ("by tonight", "by nightfall"))),
        {"problems": []},
        noisy((("Its glow", "Its shine"), ("was faint", "was dim"),
               ("was sufficient", "was ample"), ("by nightfall", "by dusk"))))
    _fake_engine(monkeypatch, prov)
    # two noisy rounds fit in a 4-turn budget; the sweep stops still noisy
    assert main(["galley", "settle", str(run), "--source", str(src),
                 "--config", _replay_config(tmp_path), "--engine", "provider",
                 "--until-clean", "--max-turns", "4"]) == 0
    oc = json.loads((run / "outcome.json").read_text("utf-8"))
    assert oc["outcome"] == "needs_human"
    assert "still finding" in oc["reason"]
    _recs, st = _records(run)
    assert st.convergence["stopped"] == "turn_budget"


def test_until_clean_with_nothing_open_starts_with_a_fresh_sweep(tmp_path,
                                                                  monkeypatch):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    run = _build(tmp_path, src, [
        {"para_id": ids[0], "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    _walk(run, [])                                   # nothing open
    # the fresh sweep: 1 change batch (clean) + 1 walk read raising one item;
    # round 1 settles it; its delta verify is clean -> quiet, done.
    prov = _Provider(
        {"problems": []},
        {"findings": [{"para_id": ids[1], "quote": "recieve", "problem": "sp",
                       "suggestion": "receive", "severity": "high"}]},
        {"problems": []}, {"findings": []})
    _fake_engine(monkeypatch, prov)
    assert main(["galley", "settle", str(run), "--source", str(src),
                 "--config", _replay_config(tmp_path), "--engine", "provider",
                 "--until-clean"]) == 0
    assert "receive the letter" in _accepted(run)[ids[1]]
    _recs, st = _records(run)
    assert any(n.startswith("fresh sweep") for n in st.notes)
    assert len(prov.calls) == 4
    fw = json.loads((run / "finished_walk.json").read_text("utf-8"))
    assert fw["ran"] is True and fw["engine"] == "provider"


def test_outcome_density_counts_wording_edits_not_mechanics(tmp_path, capsys):
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    # lots of punctuation/case edits, few wording edits: a sound book with
    # a heavy house style, not one that needs rewriting
    rows = []
    for pid, text in zip(ids, PARAGRAPHS):
        for w in ("lamp", "light", "coins", "gate", "note"):
            if w in text:
                rows.append({"para_id": pid, "original_text": w,
                             "corrected_text": w.capitalize(),
                             "confidence": "high"})
    run = _build(tmp_path, src, rows)
    assert main(["galley", "outcome", str(run), "--json"]) == 0
    out = capsys.readouterr().out
    oc = json.loads([l for l in out.splitlines() if l.startswith("{")][-1])
    assert oc["outcome"] == "done"
    assert oc["evidence"]["mechanical_edits"] >= 4
    assert oc["evidence"]["wording_edits"] == 0
