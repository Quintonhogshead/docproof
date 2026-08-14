"""The smoothing trap set: it parses, its ids are unique, it gates in both
directions (silence traps AND controls), its difficulty bands are usable, its
normalization contract holds, its two neighbouring loaders cannot read each
other's files, and the standard scorecard cannot see it — verified through the
glob that actually keeps it out, not by assertion."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))   # eval/ is a namespace package, not installed

from docproof.config import load_config  # noqa: E402
from docproof.eval.corpus import CorpusError, load_case_file, load_corpus  # noqa: E402
from docproof.normalize import normalize_text  # noqa: E402
from docproof.sweeps import resolve  # noqa: E402
from eval.tools.style_traps import (APPROVED, CONTROL, CONTROL_CATEGORIES,  # noqa: E402
                                    DEFAULT_TRAPS, DIFFICULTIES, MARGINAL,
                                    MAX_WORDS, MIN_WORDS, OVERT, SILENCE,
                                    SILENCE_CATEGORIES, TrapError, controls,
                                    count_by_category, count_by_difficulty,
                                    dialogue_share, load_traps, silence_traps,
                                    trap_file_normalization,
                                    trap_file_reconciliation, trap_file_status)

CASES = ROOT / "eval" / "cases"
ERROR_DIR = ROOT / "config" / "error_types"
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"


def _silence_item(**over):
    item = {"id": "x-01", "category": "deliberate-fragment",
            "difficulty": OVERT, "text": " ".join(["word"] * 50),
            "tempting_edit": "x", "why_silence": "y"}
    item.update(over)
    return item


def _recon(**over):
    block = {"model": "gpt-5.6-luna", "effort": "medium", "runs": 12,
             "is_configured_detector": False,
             "runner": "eval/tools/reconcile_traps.py"}
    block.update(over)
    return block


def _write(tmp_path, *items, **top):
    data = {"schema_version": 5, "kind": "smoothing_traps",
            "status": "draft-pending-user-signoff", "normalization": "on",
            "reconciliation": _recon(), "traps": list(items)}
    data.update(top)
    f = tmp_path / "traps.yaml"
    f.write_text(yaml.safe_dump(data))
    return f


def test_trap_file_loads_and_every_item_validates():
    for t in load_traps():
        assert MIN_WORDS <= t.word_count <= MAX_WORDS, (t.id, t.word_count)
        assert "\n" not in t.text.strip(), t.id
        assert t.source_file.endswith("traps.yaml")
        if t.role == SILENCE:
            assert t.category in SILENCE_CATEGORIES, t.id
            assert t.difficulty in DIFFICULTIES, t.id
            # The two fields that make a silence trap reviewable: what a pass
            # would want to do, and why doing it is wrong.
            assert t.tempting_edit.strip() and t.why_silence.strip(), t.id
            assert not t.expected_edit and not t.why_edit, t.id
        else:
            assert t.category in CONTROL_CATEGORIES, t.id
            assert not t.difficulty, t.id
            # The mirror pair: what a pass ought to propose, and why no voice
            # defence is available for the prose it would be proposing it on.
            assert t.expected_edit.strip() and t.why_edit.strip(), t.id
            assert not t.tempting_edit and not t.why_silence, t.id


def test_trap_ids_are_unique():
    ids = [t.id for t in load_traps()]
    assert len(ids) == len(set(ids))


def test_the_silence_set_is_the_size_the_phase_plan_asks_for():
    """The plan's band is 50-80 passages for the silence set. A trap set this
    small is a tripwire, not a corpus — it only works if every passage is
    deliberate. Controls are additional and counted separately."""
    assert 50 <= len(silence_traps(load_traps())) <= 80


def test_controls_exist_and_are_a_smaller_separate_group():
    """Without controls, a smoothing pass that emits NOTHING AT ALL scores a
    perfect 0% firing rate on the silence set — a hole exactly as large as the
    one the silence set is there to close. Silence on a control is a failure,
    so there have to be enough of them for that number to mean something, and
    few enough that they stay a clearly separate group rather than a second
    corpus with its own gravity."""
    traps = load_traps()
    ctl, silence = controls(traps), silence_traps(traps)
    assert 8 <= len(ctl) <= 20, len(ctl)
    assert len(ctl) < len(silence) / 3
    assert len(ctl) + len(silence) == len(traps)


def test_both_difficulty_bands_are_usable():
    """`overt` is the floor — loud stylization, where a firing is
    disqualifying. `marginal` is where a gate actually discriminates. A band of
    three passages reports a firing rate in steps of 33%, which is not a
    measurement, so both bands have to carry real weight."""
    bands = count_by_difficulty(load_traps())
    assert set(bands) == set(DIFFICULTIES), bands
    assert bands[OVERT] >= 25, bands
    assert bands[MARGINAL] >= 15, bands


def test_every_voice_risk_category_is_actually_covered_in_both_bands():
    """A category with one or two passages measures nothing. The spread is the
    point: a smoothing pass fails differently against dialect than against a
    refrain, and the scorer reports the firing rate per category — per band
    within the category, which is only meaningful if both bands are there."""
    traps = load_traps()
    counts = count_by_category(traps)
    for category in SILENCE_CATEGORIES:
        assert counts.get(category, 0) >= 8, (category, counts.get(category, 0))
        for band in DIFFICULTIES:
            n = sum(1 for t in traps
                    if t.category == category and t.difficulty == band)
            assert n >= 3, (category, band, n)
    for category in CONTROL_CATEGORIES:
        assert counts.get(category, 0) >= 4, (category, counts.get(category, 0))


def test_the_set_is_dominated_by_narration():
    """Dialogue is already protected for free: `general_error.yaml` exempts
    "dialect, deliberate fragments, coined names, and unusual-but-intentional
    phrasing" unconditionally. A trap set built out of quoted speech would
    score that guard and say nothing about narration, which is where Phase 3
    does its work and where the risk actually is."""
    traps = load_traps()
    assert dialogue_share(traps) < 0.20, dialogue_share(traps)
    silence = silence_traps(traps)
    majority = [t for t in silence if t.is_majority_dialogue]
    assert len(majority) < len(silence) / 5, [t.id for t in majority]
    # ...and the ones that are majority dialogue all sit in the one category
    # that earns it, since correcting a speaker's grammar erases the character.
    assert {t.category for t in majority} <= {"dialect-idiolect"}
    assert not any(t.is_majority_dialogue for t in controls(traps))


def test_every_mechanical_overlap_names_a_real_error_type():
    """`mechanical_overlap` says "a first-class type legitimately owns a span
    here, so a finding from it is not a smoothing failure". A typo in that key
    would silently un-exempt the trap."""
    for t in load_traps():
        assert isinstance(t.mechanical_overlap, tuple), t.id
        for key in t.mechanical_overlap:
            assert (ERROR_DIR / f"{key}.yaml").exists(), (t.id, key)


def test_no_control_carries_a_mechanical_overlap():
    """A control has to be stylistically poor and mechanically CORRECT, or it
    stops separating the smoothing pass from the proofreader: a proofreading
    finding on it would score as a success the smoothing pass never earned.

    This invariant survived a real detector run that found ctl-01 and ctl-07
    each missing a genuinely required introductory comma: the passages were
    repunctuated rather than the rule relaxed, because a control that has to
    declare an overlap is a control that has stopped being one."""
    for t in controls(load_traps()):
        assert not t.mechanical_overlap, t.id


def test_the_dialect_category_carries_three_passages_in_narration():
    """The composition claim the header makes, checked rather than asserted:
    `general_error.yaml` grants the dialect-in-dialogue guard unconditionally,
    so a dialect passage whose whole voice signal sits inside quotation marks
    scores a guard that ships for free. Three of the nine carry their idiolect
    in NARRATION instead — regional idiom in close third (dial-07), a
    second-language formality the narrator inherited (dial-08), an
    institutional register in a private notebook (dial-09) — where no quotation
    mark protects them. Counted here so the claim cannot drift out of true the
    way it did when dial-08 held its signal in two quoted sentences."""
    dialect = [t for t in load_traps() if t.category == "dialect-idiolect"]
    narration = [t.id for t in dialect if '"' not in t.text]
    assert narration == ["dial-07", "dial-08", "dial-09"], narration


def test_no_passage_violates_a_house_style_sweep():
    """The one class of mechanical defect a trap passage may never carry.

    A sweep hit is not a judgement call and not a model call: `sweep_dash` and
    its seven siblings are deterministic pattern rules that run before any API
    request, so a passage that violates one draws a high-confidence tracked
    change on every run, forever, whatever the detector is feeling that day.
    lyr-06 carried a spaced em dash for exactly that reason — Atmosphere house
    style sets the em dash unspaced — and LanguageTool structurally cannot see
    it, having no house dash rule, which is why the sweep set that ships is run
    here directly instead.

    Both forms are checked, because `normalization: on` means the document the
    pass sees is the curled one: a rule that is quiet on straight quotes and
    fires on curly ones would otherwise slip through.
    """
    cfg = load_config(str(DEFAULT_CONFIG))
    sweeps = resolve(cfg.sweeps)
    assert sweeps, "config/default.yaml enables no sweeps"
    for t in load_traps():
        for text in (t.text, normalize_text(t.text)):
            for sweep in sweeps:
                hits = (sweep.scan(text, None, style=cfg.style.ellipsis)
                        if sweep.key == "sweep_ellipsis" else
                        sweep.scan(text, None))
                assert not hits, (
                    t.id, sweep.key,
                    [text[h.start:h.end] for h in hits])


# --- which model the reconciliation was measured on ---------------------------

def test_the_reconciliation_names_the_model_it_was_measured_on():
    """The header's DETECTOR RECONCILIATION section is the file's central
    honesty claim, and a claim about a stochastic detector is a claim about ONE
    model over a known number of runs. Both have to be on the record."""
    recon = trap_file_reconciliation()
    assert recon.model.strip()
    assert recon.runs >= 1


def test_the_reconciliation_model_flag_agrees_with_the_shipped_config():
    """The check that would have caught this file's worst defect.

    Every number in DETECTOR RECONCILIATION was produced against gpt-5.6-luna
    while `config/default.yaml` sets `api.model: claude-haiku-4-5` — and with
    `ensemble.detectors` empty, that single model IS the detector a manuscript
    is reviewed by. The header called the section "the real pipeline" and named
    the substitute model only in passing, so nothing could tell the two apart.
    Findings are not portable between models: `eval/tools/anchor_dump.py` exists
    because the accuracy baseline put claude-haiku-4-5 at a 29% anchor-failure
    rate against gpt-5.6-luna's 0%.

    So the file asserts whether its reconciliation model is the configured one,
    and this test decides whether the assertion is true, from the config. It
    fails in BOTH directions: reconcile against the shipped detector without
    flipping the flag and it fails; change `api.model` under a file that claims
    agreement and it fails.
    """
    recon = trap_file_reconciliation()
    configured = load_config(str(DEFAULT_CONFIG)).api.model
    assert recon.is_configured_detector == (recon.model == configured), (
        f"the trap file says is_configured_detector="
        f"{recon.is_configured_detector} for model {recon.model!r}, but "
        f"config/default.yaml configures {configured!r}")


def test_the_reconciliation_names_a_runner_that_exists():
    """The check that makes the header's regeneration claim true rather than
    merely written down.

    v4's header said the DETECTOR RECONCILIATION section "is regenerated
    whenever a passage changes rather than being inherited". No runner had ever
    been committed — `eval/tools/` held anchor_dump, compare_preflight,
    generate_injected and style_traps and nothing else — and the script that
    produced the table lived in a scratchpad that did not survive the session.
    The claim was therefore unfalsifiable, and worse, unrepeatable: the next
    person to change a passage had no way to redo the measurement.

    So the runner is a field, and this test resolves it against the filesystem.
    Delete or rename the tool and the trap file fails its own tests until the
    claim and the repo agree again.
    """
    recon = trap_file_reconciliation()
    runner = ROOT / recon.runner
    assert runner.is_file(), (
        f"reconciliation.runner names {recon.runner!r}, which does not exist. "
        f"A reconciliation section with no runner behind it is inherited, not "
        f"regenerated")


def test_the_reconciliation_runner_cannot_reach_the_scorecard_loader():
    """The runner is dev tooling that builds a document out of trap prose and
    calls the real pipeline on it. It must not become a second way for the trap
    set to enter the standard accuracy corpus, which `load_corpus` globs out of
    `eval/cases/*.yaml`: it lives under `eval/tools/`, and it is a `.py`."""
    recon = trap_file_reconciliation()
    runner = ROOT / recon.runner
    assert runner.suffix == ".py", recon.runner
    assert CASES not in runner.parents, recon.runner


def test_a_reconciliation_without_a_runner_is_rejected(tmp_path):
    """Refused rather than defaulted, for the same reason the model is: a
    reconciliation whose regenerating script is unnamed is one nobody can
    repeat, and "the header says it is regenerated" is not evidence that it
    is."""
    block = _recon()
    del block["runner"]
    with pytest.raises(TrapError, match="runner"):
        load_traps(_write(tmp_path, _silence_item(), reconciliation=block))


def test_a_missing_reconciliation_block_is_rejected(tmp_path):
    """A reconciliation table with no model on it is a page of numbers with no
    claim attached, which is what schema 3 allowed and what schema 4 exists to
    stop."""
    f = _write(tmp_path, _silence_item(), reconciliation=None)
    with pytest.raises(TrapError, match="reconciliation"):
        load_traps(f)


def test_a_reconciliation_without_the_configured_detector_flag_is_rejected(tmp_path):
    """Omitting the flag is precisely how a reconciliation against a stand-in
    model gets read as one against the shipped detector, so it is refused rather
    than defaulted — a default either way would be a guess about the one fact
    the block exists to record."""
    block = _recon()
    del block["is_configured_detector"]
    with pytest.raises(TrapError, match="is_configured_detector"):
        load_traps(_write(tmp_path, _silence_item(), reconciliation=block))


@pytest.mark.parametrize("runs", [0, -1, "seven", True, None])
def test_a_reconciliation_without_a_usable_run_count_is_rejected(tmp_path, runs):
    """The detector is stochastic — no two runs over this file have returned
    the same set — so every count in the section is meaningless without the
    number of runs behind it. `True` is in the list because bool is an int
    subclass and `runs: true` is a typo, not one run."""
    f = _write(tmp_path, _silence_item(), reconciliation=_recon(runs=runs))
    with pytest.raises(TrapError, match="runs"):
        load_traps(f)


def test_trap_status_is_declared():
    """The set is agent-drafted; docs/accuracy-eval-plan.md requires trap
    material to be human-authored. Until the user signs off, `status` says so,
    and no firing rate measured against it may be quoted."""
    status = trap_file_status()
    assert status in ("draft-pending-user-signoff", APPROVED)


# --- the normalization contract ----------------------------------------------

def test_the_normalization_contract_is_declared_and_holds():
    """The file declares whether the trap document goes through
    `docproof.normalize` before the pass sees it, so the scorer and Phase 3
    cannot quietly disagree. It says "on", and that is only safe because
    normalizing these passages moves no offset: the quote rule substitutes one
    character for one, and the space rule cannot fire because the loader
    rejects a double space. Checked against the real normalizer rather than
    asserted in a header comment."""
    assert trap_file_normalization() == "on"
    for t in load_traps():
        assert len(normalize_text(t.text)) == len(t.text), t.id


def test_normalization_actually_has_something_to_do():
    """The contract would be vacuous if no passage contained a straight mark.
    The dialect passages are full of them — dropped-g apostrophes, nested
    speech — which is the whole reason the trap document is run curled."""
    changed = [t.id for t in load_traps() if normalize_text(t.text) != t.text]
    assert len(changed) > 20, changed


def test_a_curly_quote_in_a_passage_is_rejected(tmp_path):
    f = _write(tmp_path, _silence_item(text="word " * 49 + "“word”"))
    with pytest.raises(TrapError, match="curly quote"):
        load_traps(f)


def test_a_double_space_in_a_passage_is_rejected(tmp_path):
    f = _write(tmp_path, _silence_item(text="word  " + "word " * 49))
    with pytest.raises(TrapError, match="double space"):
        load_traps(f)


def test_a_missing_normalization_contract_is_rejected(tmp_path):
    f = _write(tmp_path, _silence_item(), normalization=None)
    with pytest.raises(TrapError, match="normalization"):
        load_traps(f)


# --- role and difficulty are enforced against each other ---------------------

def test_a_control_category_with_a_silence_role_is_rejected(tmp_path):
    """The cross-check that stops a typo moving a passage between the two
    halves of the metric, which would change a firing rate without changing
    any number a person reads."""
    f = _write(tmp_path, _silence_item(category="control-deadwood"))
    with pytest.raises(TrapError, match="implies role"):
        load_traps(f)


def test_a_silence_trap_without_a_difficulty_is_rejected(tmp_path):
    item = _silence_item()
    del item["difficulty"]
    with pytest.raises(TrapError, match="difficulty"):
        load_traps(_write(tmp_path, item))


def test_a_control_with_a_difficulty_is_rejected(tmp_path):
    f = _write(tmp_path, {"id": "c-01", "category": "control-deadwood",
                          "role": CONTROL, "difficulty": MARGINAL,
                          "text": " ".join(["word"] * 50),
                          "expected_edit": "x", "why_edit": "y"})
    with pytest.raises(TrapError, match="carries no 'difficulty'"):
        load_traps(f)


def test_a_control_missing_its_own_fields_is_rejected(tmp_path):
    f = _write(tmp_path, {"id": "c-01", "category": "control-clumsy-syntax",
                          "role": CONTROL, "text": " ".join(["word"] * 50),
                          "expected_edit": "x"})
    with pytest.raises(TrapError, match="why_edit"):
        load_traps(f)


def test_a_silence_trap_carrying_control_prose_is_rejected(tmp_path):
    """`why_silence` and `why_edit` say opposite things about the same
    passage. An item with both is an item nobody has decided about."""
    f = _write(tmp_path, _silence_item(why_edit="smooth it"))
    with pytest.raises(TrapError, match="belongs to a control"):
        load_traps(f)


# --- the two loaders cannot read each other's files --------------------------

def test_a_case_file_is_not_a_trap_file():
    with pytest.raises(TrapError, match="kind"):
        load_traps(CASES / "comma_splice.yaml")


def test_a_trap_file_is_not_a_case_file():
    with pytest.raises(CorpusError, match="error_type"):
        load_case_file(DEFAULT_TRAPS)


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_a_superseded_schema_version_is_refused(tmp_path, version):
    """v1 was silence-only and had no `role`, so every control in a file read
    as v1 would score as a silence trap and the firing rate would come out
    wrong in the flattering direction. v2 wrote `mechanical_overlap` as one
    key rather than a list, which a v3 reader would iterate character by
    character. v3 had no `reconciliation` block, so a whole detector table
    measured on a stand-in model read as one measured on the shipped detector.
    v4 had no `reconciliation.runner`, so the header could claim the table was
    regenerated on every passage change with no script in the repo to do it.
    Refuse all four rather than guess."""
    f = _write(tmp_path, _silence_item(), schema_version=version)
    with pytest.raises(TrapError, match="schema_version"):
        load_traps(f)


def test_a_scalar_mechanical_overlap_is_rejected(tmp_path):
    """The v2 spelling, refused by name instead of being wrapped: a string is
    iterable, so accepting it quietly would turn "word_echo" into nine
    one-character error-type keys, none of which names a real type."""
    f = _write(tmp_path, _silence_item(mechanical_overlap="comma_splice"))
    with pytest.raises(TrapError, match="LIST of error-type keys"):
        load_traps(f)


def test_a_repeated_mechanical_overlap_key_is_rejected(tmp_path):
    """Declaring a type twice would double-count it in the reconciliation
    table, which is the one table this file exists to keep honest."""
    f = _write(tmp_path, _silence_item(
        mechanical_overlap=["comma_splice", "comma_splice"]))
    with pytest.raises(TrapError, match="repeats a key"):
        load_traps(f)


def test_a_passage_can_declare_two_overlapping_types(tmp_path):
    """The reason for schema 3. Running the real pipeline over the file found
    passages where two first-class types have a claim on different spans of one
    paragraph — rhy-08's opening is both a short-parallel-clause splice and a
    withheld introductory comma — and a single-key field could only ever record
    one of them."""
    f = _write(tmp_path, _silence_item(
        mechanical_overlap=["comma_splice", "introductory_comma"]))
    assert load_traps(f)[0].mechanical_overlap == ("comma_splice",
                                                   "introductory_comma")


def test_a_duplicate_trap_id_is_rejected(tmp_path):
    f = _write(tmp_path, _silence_item(), _silence_item())
    with pytest.raises(TrapError, match="duplicate trap id"):
        load_traps(f)


def test_a_missing_field_is_rejected(tmp_path):
    item = _silence_item()
    del item["why_silence"]
    with pytest.raises(TrapError, match="why_silence"):
        load_traps(_write(tmp_path, item))


def test_an_unknown_category_is_rejected(tmp_path):
    with pytest.raises(TrapError, match="unknown category"):
        load_traps(_write(tmp_path, _silence_item(category="vibes")))


# --- isolation from the standard scorecard -----------------------------------

def test_load_corpus_glob_is_non_recursive(tmp_path):
    """The actual mechanism that keeps the trap set out of the scorecard:
    `load_corpus` globs `*.yaml`, not `**/*.yaml`. Proven here on a synthetic
    tree, so a future change from glob to rglob fails this test instead of
    silently folding smoothing prose into the accuracy numbers."""
    cases = tmp_path / "cases"
    (cases / "smoothing").mkdir(parents=True)
    top = {"error_type": "spelling",
           "cases": [{"id": "top-1", "text": "The harbor lamps were lit at dusk.",
                      "expect": {"span": "harbor"}}]}
    nested = {"error_type": "spelling",
              "cases": [{"id": "nested-1", "text": "The lamps burned all night.",
                         "expect": {"span": "lamps"}}]}
    (cases / "spelling.yaml").write_text(yaml.safe_dump(top))
    (cases / "smoothing" / "traps.yaml").write_text(yaml.safe_dump(nested))

    loaded = load_corpus(cases)
    assert [c.id for c in loaded] == ["top-1"]
    # And the nested file is a perfectly valid case file — it was skipped for
    # its location, not for its content.
    assert [c.id for c in load_case_file(cases / "smoothing" / "traps.yaml")] == ["nested-1"]


def test_the_scorecard_never_sees_the_trap_file():
    # The trap file is not under the globbed directory at all...
    assert DEFAULT_TRAPS not in set(CASES.glob("*.yaml"))
    assert CASES not in DEFAULT_TRAPS.parents
    # ...and nothing it contains reaches the loaded corpus, by path or by id.
    cases = load_corpus(CASES)
    trap_ids = {t.id for t in load_traps()}
    for c in cases:
        assert "smoothing" not in Path(c.source_file).parts, c.source_file
        assert c.id not in trap_ids, c.id


def _top_level_case_files() -> list[Path]:
    """The files the scorecard is made of, enumerated by plain directory
    listing instead of by reusing `load_corpus`'s own glob — so a loader that
    started reading subdirectories disagrees with this list rather than
    agreeing with itself."""
    return sorted(p for p in CASES.iterdir()
                  if p.is_file() and p.suffix == ".yaml"
                  and not p.name.startswith("_"))


def test_the_standard_corpus_is_exactly_its_top_level_case_files():
    """The claim this phase owes: the public scorecard is the sum of
    `eval/cases/*.yaml` and nothing else.

    A pinned case count does not make that claim. It asserts "no author
    anywhere ever added a case", which is a different and much weaker thing:
    it breaks on the next unrelated PR that adds one, and it stays green if
    smoothing prose were folded in while a mechanical case was dropped. These
    equalities are true by mechanism instead, so they survive every legitimate
    case addition and still fail the moment `load_corpus` turns recursive or
    the trap set moves under `eval/cases/`.

    `eval/cases/injected/` is the live witness: real, valid, loadable case
    files one level down, deliberately outside the scorecard (they are the
    injected tier, scored separately). Nothing synthetic is needed to prove a
    subdirectory stays invisible — the repo already ships one.
    """
    loaded = load_corpus(CASES)
    files = _top_level_case_files()
    assert files, "no case files in eval/cases"

    # Provenance: every loaded case came from one of those files, and every one
    # of those files is represented (an empty `cases:` list is a CorpusError,
    # so each file must contribute).
    assert ({Path(c.source_file).resolve() for c in loaded}
            == {p.resolve() for p in files})
    # Count: self-consistent with the files on disk, not with a magic number.
    assert len(loaded) == sum(len(load_case_file(p)) for p in files)

    # And the subdirectory really is loadable-but-unloaded — skipped for where
    # it sits, not for being malformed.
    injected = sorted((CASES / "injected").glob("*.yaml"))
    assert injected, "expected eval/cases/injected/ to still be a subdirectory"
    nested_ids = {c.id for f in injected for c in load_case_file(f)}
    assert nested_ids and nested_ids.isdisjoint({c.id for c in loaded})


def test_a_smoothing_tree_never_changes_what_the_scorecard_loads(tmp_path):
    """Standing rule 4 as a differential rather than as a constant: adding the
    trap set to the tree leaves the loaded corpus identical, case for case.
    Run over copies of the real case files and the real trap file, so it is the
    shipped layout under test and not a toy of it."""
    tree = tmp_path / "eval"
    cases = tree / "cases"
    shutil.copytree(CASES, cases)
    before = [c.id for c in load_corpus(cases)]

    # Where the trap set actually lives: a sibling of the globbed directory.
    (tree / "smoothing").mkdir()
    shutil.copy(DEFAULT_TRAPS, tree / "smoothing" / "traps.yaml")
    assert [c.id for c in load_corpus(cases)] == before

    # And where an absent-minded refactor would put it: inside the globbed
    # directory, one level down. Still invisible, because the glob is not
    # recursive — so a loader that grew an `rglob` fails here, on the trap
    # file's deliberately incompatible schema, instead of folding smoothing
    # prose into the accuracy numbers.
    (cases / "smoothing").mkdir()
    shutil.copy(DEFAULT_TRAPS, cases / "smoothing" / "traps.yaml")
    assert [c.id for c in load_corpus(cases)] == before
