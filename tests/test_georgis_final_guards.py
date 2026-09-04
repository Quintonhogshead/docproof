"""Items 13–20 from the final Georgis review (2026-09-04): the sweep guard in
the core validator, sub-dollar currency, the dash/time sweeps, the
cross-boundary duplicate, unresolved fields, the ledger promotion, and the
letter's workspace spend. Strings are the ones from runs/ladder/findings.json.
"""
from __future__ import annotations

import json

import pytest

from docproof.config import Config, load_config
from docproof.models import Anchor, DocumentModel, Finding, ParagraphRef
from docproof.sweepguard import SweepGuard
from docproof.validator import duplicates_neighbour, validate_findings


def _doc(*texts: str) -> DocumentModel:
    return DocumentModel(
        source_path="x.docx",
        paragraphs=tuple(ParagraphRef(f"body-{i:04d}", "word/document.xml",
                                      "body", t, "Normal")
                         for i, t in enumerate(texts)))


def _f(fid, original, corrected, *, etype="imported_edit", para="body-0000"):
    return Finding(fid, "chunk-000", para, etype, original, 1, corrected, "x",
                   "high")


# --- (13) the sweep guard refuses a lane's row the house sweeps would undo ---

GEORGIS_230 = ("It was 2:30 AM the next morning when Aunt Paraskevi woke me "
               "and uncle Anastasi up.")


def test_a_capitalization_row_that_lowercases_a_house_time_is_refused():
    cfg = load_config("config/default.yaml")
    guard = SweepGuard.from_config(cfg, None)
    doc = _doc(GEORGIS_230)
    out = validate_findings(
        [_f("c-1", GEORGIS_230, GEORGIS_230.replace("2:30 AM", "2:30 am"),
            etype="capitalization")], doc, "medium", sweep_guard=guard)
    assert out[0].status == "rejected_undoes_house_style:sweep_time_of_day"
    # the same row's OTHER fix in the sentence still lands on its own
    out = validate_findings(
        [_f("c-2", GEORGIS_230, GEORGIS_230.replace("uncle", "Uncle"),
            etype="capitalization")], doc, "medium", sweep_guard=guard)
    assert out[0].status == "validated"
    # without a guard the validator behaves as before
    out = validate_findings(
        [_f("c-3", GEORGIS_230, GEORGIS_230.replace("2:30 AM", "2:30 am"),
            etype="capitalization")], doc, "medium")
    assert out[0].status == "validated"


def test_the_guard_does_not_refuse_a_row_where_the_sweep_already_fired():
    """A model row whose span already carried a sweep target ("3 p.m." it
    does not touch) is not blamed for it: the sweep fires before and after."""
    cfg = load_config("config/default.yaml")
    guard = SweepGuard.from_config(cfg, None)
    text = "We met at 3 p.m. and teh rain came."
    doc = _doc(text)
    out = validate_findings(
        [_f("s-1", "at 3 p.m. and teh rain", "at 3 p.m. and the rain",
            etype="spelling")], doc, "medium", sweep_guard=guard)
    assert out[0].status == "validated"


# --- (14) sub-dollar amounts stay in words ----------------------------------------

@pytest.mark.parametrize("original,corrected", [
    ("I needed $1.35 or 90 cents to get a seat.",
     "I needed $1.35 or $0.90 to get a seat."),
    ("In a flurry of excitement, I paid a dime for my coffee.",
     "In a flurry of excitement, I paid $0.10 for my coffee."),
    ("The 5 cents in my pocket were not going to get me very far.",
     "The $0.05 in my pocket were not going to get me very far."),
])
def test_currency_style_never_produces_a_sub_dollar_numeral(original, corrected):
    doc = _doc(original)
    out = validate_findings([_f("m-1", original, corrected,
                                etype="currency_style")], doc, "medium")
    assert out[0].status == "rejected_policy"
    # a whole-dollar restyle is still an edit
    out = validate_findings(
        [_f("m-2", "The ticket cost twelve dollars and she paid.",
            "The ticket cost $12 and she paid.", etype="currency_style")],
        _doc("The ticket cost twelve dollars and she paid."), "medium")
    assert out[0].status == "validated"


def test_the_currency_prompt_says_so():
    from docproof.error_registry import load_error_types
    et = load_error_types("config/error_types", ["currency_style"])["currency_style"]
    text = et.detection_prompt if hasattr(et, "detection_prompt") else str(et)
    assert "a dime" in text and "$0.05" in text


# --- (15) sweeps: one-sided hyphens and every typed time form ------------------

def _swept(key: str, text: str) -> str:
    from docproof.sweeps import SWEEPS_BY_KEY, apply_hits
    return apply_hits(text, SWEEPS_BY_KEY[key].scan(text, None))


@pytest.mark.parametrize("before,after", [
    ("the fast- flowing river", "the fast-flowing river"),
    ("a co- worker of mine", "a co-worker of mine"),
    ("a well- known author", "a well-known author"),
    ("a T- shirt cannon", "a T-shirt cannon"),
    ("twenty- five dollars", "twenty-five dollars"),
])
def test_a_one_sided_hyphen_is_a_broken_compound_never_a_dash(before, after):
    assert _swept("sweep_dash", before) == after
    assert "—" not in _swept("sweep_dash", before)


@pytest.mark.parametrize("text", [
    "that is now on us- well, me- to chase down.",   # function words: a reader's call
    "the explosions- none of this is their fault.",
    "it was late -too late.",                        # space BEFORE: left alone
    "the 5- and 10-mile options",                    # suspended pair
    "a well-known author",
])
def test_ambiguous_one_sided_hyphens_are_left_alone(text):
    assert _swept("sweep_dash", text) == text


@pytest.mark.parametrize("before,after", [
    ("It was 2:30 am the next morning", "It was 2:30 AM the next morning"),
    ("by 11PM tonight", "by 11:00 PM tonight"),
    ("up at 7AM.", "up at 7:00 AM."),
    ("at 9:00am sharp", "at 9:00 AM sharp"),
    ("home at 8:00pm", "home at 8:00 PM"),
])
def test_every_typed_time_form_becomes_the_house_form(before, after):
    assert _swept("sweep_time_of_day", before) == after


def test_a_meridiem_glued_to_a_longer_word_is_not_a_time():
    assert _swept("sweep_time_of_day", "the crowd at 3 amid the rain") == \
        "the crowd at 3 amid the rain"
    assert _swept("sweep_time_of_day", "at 7 ambulances arrived") == \
        "at 7 ambulances arrived"


# --- (16) the cross-boundary duplicate ---------------------------------------------

KALAMATA = ("It was 12 noon when we arrived in. Kalamata, the capital of "
            "Messenia, was quiet.")


def test_a_missing_word_that_repeats_the_next_sentences_first_word_is_refused():
    doc = _doc(KALAMATA)
    out = validate_findings(
        [_f("mw-1", "It was 12 noon when we arrived in.",
            "It was 12 noon when we arrived in Kalamata.", etype="missing_word")],
        doc, "medium")
    assert out[0].status == "rejected_duplicates_neighbour"
    assert duplicates_neighbour(KALAMATA, 33, 33, " Kalamata") == "Kalamata"
    # the word before the span counts too; a re-typed word does not
    assert duplicates_neighbour("the old dog barked", 8, 8, "dog ") == "dog"
    assert duplicates_neighbour("the old dog barked", 8, 11, "dog.") is None
    assert duplicates_neighbour("the old dog barked", 8, 11, "hound") is None


# --- (17) unresolved field results ---------------------------------------------------

def _docx_with(tmp_path, paragraphs, styles=None):
    import docx
    d = docx.Document()
    for i, p in enumerate(paragraphs):
        para = d.add_paragraph(p)
        if styles and styles[i]:
            try:
                para.style = d.styles[styles[i]]
            except KeyError:
                pass
    path = tmp_path / "book.docx"
    d.save(path)
    return path


def test_profile_and_certify_report_unresolved_fields(tmp_path):
    from docproof.genre_profile import build_profile
    from galley.manifest import certify_run, unresolved_field_results
    src = _docx_with(tmp_path, [
        "Contents",
        "CLASSESES\tError! Bookmark not defined.",
        "Chapter One\tError! Reference source not found.",
        "The story began on a wet Tuesday in Kalamata.",
    ], styles=[None, "TOC 1", "TOC 1", None])
    hits = unresolved_field_results(src)
    assert len(hits) == 2 and all("Error!" in h[1] for h in hits)
    profile = build_profile(src, Config())
    assert len(profile.field_errors) == 2
    assert "Bookmark not defined" in profile.field_errors[0]
    # certify: a warning, never a failure — the designer regenerates the TOC
    run = tmp_path / "run"
    run.mkdir()
    (run / "book - Atmosphere Press Proofreader.docx").write_bytes(src.read_bytes())
    (run / "findings.json").write_text(json.dumps(
        {"findings": [], "cost": {"total_usd": 0.0}}), "utf-8")
    cert = certify_run(run)
    check = next(c for c in cert.checks if c.name == "unresolved fields")
    assert check.status == "warn" and "2" in check.detail
    assert check not in cert.failed


def test_settle_drops_a_residual_in_a_toc_paragraph_as_unreachable():
    from galley.settle import Decision, Residual, decide
    from docproof import editmap as emap
    em = emap.EditMap(paragraphs={}, owner_of={}, unmapped={"body-0001": "skipped"})
    res = Residual(id="r1", kind="residual", para_id="body-0001",
                   quote="CLASSESES", problem="typo", suggestion="CLASSES")
    dec = decide(res, em, {}, {}, {}, None,
                 skipped={"body-0001": "style:TOC1"})
    assert isinstance(dec, Decision)
    assert dec.action == "drop" and dec.reason == "toc_unreachable"


# --- (19) an uncertain site may be promoted to an edit ----------------------------

def test_a_gate_may_promote_an_uncertain_site_to_an_edit():
    from docproof.site_ledger import ExaminationLedger, LedgerState
    from docproof.site_ledger import _ALLOWED
    assert LedgerState.EDIT in _ALLOWED[LedgerState.UNCERTAIN]
    assert LedgerState.MODEL_CONFIRMED in _ALLOWED[LedgerState.UNCERTAIN]


def test_shadow_reporter_survives_a_promoted_low_confidence_site(tmp_path):
    """The Georgis finish(): a low-confidence finding moved a precise site to
    uncertain; the confirm valve then validated the same span, and the
    shadow reporter raised on uncertain -> edit."""
    from docproof.examination import prepare_shadow
    from docproof.site_ledger import LedgerState
    from docproof.spellscan import SpellScan
    from docproof.examination import site_from_finding
    text = "Teh first word."
    para = ParagraphRef("body-0000", "word/document.xml", "body", text, "Normal")
    doc = DocumentModel("book.docx", (para,))
    cfg = Config(error_types=[["spelling"]], sweeps=[],
                 examination_graph={"enabled": True})
    run = prepare_shadow(cfg, doc, paragraphs=[para], sweep_findings=[],
                         consistency_findings=[], spell=SpellScan(available=False),
                         adjudicate_candidates=[])
    soft = Finding("f-soft", "chunk-000", para.para_id, "spelling", text, 1,
                   text.replace("Teh", "The"), "soft", "low",
                   status="skipped_low_confidence",
                   anchor=Anchor(0, 3, "Teh", "The"))
    promoted = Finding("f-promoted", "confirm", para.para_id, "spelling", text, 1,
                       text.replace("Teh", "The"), "confirmed", "high",
                       status="validated", anchor=Anchor(0, 3, "Teh", "The"))
    run.observe_findings([soft], doc, applied_ids=())
    assert run.ledger.state(site_from_finding(soft, doc).site_id) == \
        LedgerState.UNCERTAIN
    run.observe_findings([promoted], doc, applied_ids=(promoted.finding_id,))
    assert run.ledger.state(site_from_finding(promoted, doc).site_id) == \
        LedgerState.APPLIED


# --- (20a) the letter states the workspace's real spend ------------------------------

def test_workspace_waves_sum_every_runs_costs(tmp_path):
    from galley.casefile_synth import workspace_waves
    runs = tmp_path / "runs"
    for name, cost, extra in (("ladder", 3.25, {}),
                              ("final_build5", 0.0, {"settlement.json": 0.40,
                                                     "change_verify.json": 0.55,
                                                     "finished_walk.json": 0.30})):
        d = runs / name
        d.mkdir(parents=True)
        (d / "findings.json").write_text(json.dumps({
            "findings": [{"error_type": "spelling"}, {"error_type": "sweep_dash"}],
            "cost": {"total_usd": cost}}), "utf-8")
        for fname, c in extra.items():
            (d / fname).write_text(json.dumps({"cost": {"total_usd": c}}), "utf-8")
    waves = workspace_waves(tmp_path)
    assert [w.index for w in waves] == [1, 2]
    assert waves[0].spend_usd == pytest.approx(3.25)
    assert waves[1].spend_usd == pytest.approx(1.25)
    assert sum(w.spend_usd for w in waves) == pytest.approx(4.50)
    assert "spelling" in waves[0].actions[0]["lanes"]


def test_galley_letter_workspace_flag_reports_the_real_spend(tmp_path, capsys):
    from docproof.__main__ import main
    runs = tmp_path / "runs"
    d = runs / "final"
    d.mkdir(parents=True)
    (d / "findings.json").write_text(json.dumps({
        "findings": [{"finding_id": "f-1", "para_id": "body-0000",
                      "original_text": "teh", "corrected_text": "the",
                      "error_type": "spelling", "status": "validated",
                      "applied": True, "anchor": {"start": 0, "end": 3}}],
        "cost": {"total_usd": 0.0}}), "utf-8")
    (runs / "ladder").mkdir()
    (runs / "ladder" / "findings.json").write_text(json.dumps({
        "findings": [], "cost": {"total_usd": 2.5}}), "utf-8")
    rc = main(["galley", "letter", str(d), "--workspace", str(tmp_path),
               "--out", str(tmp_path / "out"), "--json"])
    assert rc == 0
    env = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert env["waves"] == 2 and env["spent_usd"] == pytest.approx(2.5)
    letter = (tmp_path / "out" / "letter.md").read_text("utf-8")
    assert "$2.50" in letter


# --- (20b) inventory never spends ----------------------------------------------------

def test_inventory_never_builds_the_storysheet_provider(tmp_path, monkeypatch):
    import docproof.__main__ as m
    from docproof import pipeline
    from .conftest import FIXTURES
    calls = []
    real = pipeline.prepare

    def spy(cfg, path, error_dir, **kw):
        calls.append(kw)
        return real(cfg, path, error_dir, **kw)
    monkeypatch.setattr(m, "prepare", spy)
    monkeypatch.setattr("docproof.storysheet.build_storysheet",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("storysheet provider was built")))
    rc = m.main(["inventory", str(FIXTURES / "simple.docx")])
    assert rc == 0
    assert calls and calls[0].get("dry_run") is True
