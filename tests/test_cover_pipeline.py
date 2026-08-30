"""docproof/cover/pipeline.py: the job store and the orchestration around it.

run_directions, revise_spec, distill_reality, imaging.generate/has_real_alpha,
compose/save_renders, and critique.run_critique are all monkeypatched
directly on the pipeline module (the "pipeline seam") for every async test
here -- this suite is about pipeline.py's OWN job (state transitions,
concurrency, ledger, on-disk persistence, the revision asset-diffing
contract, the §6.3 iterating critique loop, reality-sheet wiring), not about
whether direction.py/imaging.py/compose.py/critique.py/reality.py themselves
are correct, which is what their own dedicated test files already cover. No
network, no real image bytes, no real render pixels anywhere in this file.

No pytest-asyncio in this repo (see tests/ for the convention): every async
pipeline entry point is driven with a plain asyncio.run() inside an ordinary
sync test function.

PROVIDERS bundles three DISTINCT sentinel Providers (one per model role --
direction, revision, reality), so a test can assert not just THAT a provider
was passed somewhere, but WHICH role's provider reached which call
(run_directions gets .direction; revise_spec -- human-triggered or from the
auto-critique loop -- gets .revision; distill_reality gets .reality).
CRITIQUE_CLIENT is a separate sentinel from IMAGE_CLIENT: critique.py talks
to the anthropic SDK directly, not through a Provider, and pipeline.py now
threads it as its own parameter (BRAIN wave, 2026-08-29) rather than reusing
the OpenAI image client the way v1 did.

_default_critique_passes (autouse) defaults every test's critique step to a
free, always-passing verdict, so the pre-existing tests below -- written
before §6.3 existed, and before it was made to iterate -- keep testing
exactly what they tested before: the paint/compose/ledger/persistence
machinery, undisturbed by the critique pass riding along on every run_job
now. The "-- critique pass (§6.3, iterated) --" section further down
overrides that default per test to exercise the critique wiring itself.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from docproof.cover import pipeline
from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.critique import CritiqueResult
from docproof.cover.direction import (DirectionError, DirectionResult,
                                      RevisionError, RevisionResult)
from docproof.cover.model import (Brief, ConceptState, CoverSpec, Direction,
                                  Palette, RenderReport, build_spec)
from docproof.cover.planner import CompositionPlan, PlannerError, StageReview
from docproof.cover.reality import RealityResult, RealitySheet, RealitySheetError, \
    render_reality_sheet
from docproof.ingest import IngestError

# Sentinels: never really called, since run_directions/revise_spec/
# distill_reality/generate are monkeypatched in every test that reaches
# them -- only their PLUMBING (did pipeline.py pass the right thing to the
# right place) is under test. Three DISTINCT provider sentinels, one per
# role, so a test can assert WHICH role's provider reached a given call.
DIRECTION_PROVIDER = object()
REVISION_PROVIDER = object()
REALITY_PROVIDER = object()
PROVIDERS = pipeline.Providers(direction=DIRECTION_PROVIDER,
                               revision=REVISION_PROVIDER,
                               reality=REALITY_PROVIDER)
IMAGE_CLIENT = object()
CRITIQUE_CLIENT = object()
FAKE_IMAGE = object()          # stands in for a PIL.Image.Image


@pytest.fixture(autouse=True)
def _default_critique_passes(monkeypatch):
    monkeypatch.setattr(pipeline, "run_critique", lambda *a, **k: CritiqueResult(
        passes=True, tells=[], notes="", cost=0.001))


# -- fixtures -----------------------------------------------------------------

def _brief(**overrides) -> Brief:
    data = dict(title="The Lighthouse at Gull Point", author="J. R. Vance",
               genre="literary", concepts=2)
    data.update(overrides)
    return Brief(**data)


def _palette(**overrides) -> Palette:
    data = dict(background="#101010", primary="#f5f1e8", accent="#c9a227",
               text="#f5f1e8", scrim="#000000")
    data.update(overrides)
    return Palette(**data)


_PROMPTS = {
    "big_type": {},
    "full_bleed_art": {"background": "A lonely lighthouse at dusk, oil painting."},
    "cutout_sandwich": {"background": "A misty pine forest, gouache.",
                        "focal": "A cloaked figure, cutout subject only."},
}


def _direction(archetype: str = "full_bleed_art", **overrides) -> Direction:
    data = dict(concept_name=f"Concept ({archetype})", rationale="A test concept.",
               archetype=archetype, palette=_palette(),
               title_font="Playfair Display", author_font="Spectral",
               art_prompts=_PROMPTS[archetype], texture=False)
    data.update(overrides)
    return Direction(**data)


def _report(**overrides) -> RenderReport:
    data = dict(contrast={}, scrim_final={}, fitted_sizes={}, warnings=[])
    data.update(overrides)
    return RenderReport(**data)


def _spec_for_diffing(archetype: str = "full_bleed_art") -> CoverSpec:
    return build_spec(_direction(archetype), _brief(), ARCHETYPES[archetype])


def _fake_compose(spec, job_dir):
    return FAKE_IMAGE, _report()


def _fake_save_renders(image, job_dir, version, concept):
    """Writes a real (tiny, fake) file at the path it claims to -- the
    critique step (§6.3) reads this file back off disk, so a fake that only
    returns a string without writing anything would make _run_critique_safely
    see a FileNotFoundError (an OSError, indistinguishable from a real
    critique-call failure) before run_critique is ever actually called."""
    out = job_dir / "renders"
    out.mkdir(parents=True, exist_ok=True)
    rel = f"renders/v{version}_c{concept}.png"
    (job_dir / rel).write_bytes(b"fake-png-bytes")
    return [rel]


def _fake_save_renders_with_thumb(image, job_dir, version, concept):
    """Like _fake_save_renders, but also writes the 100px shelf/search
    thumbnail companion file (BRAIN v2.1 -- compose.save_renders's own
    `_thumb100.png` naming) -- for the handful of tests below that verify
    _run_critique_safely's thumb-path plumbing specifically. Every other
    test in this file keeps using the thumb-less _fake_save_renders, which
    doubles as this suite's implicit coverage of the "missing thumbnail"
    degrade path (see pipeline._thumb_path/_run_critique_safely)."""
    out = job_dir / "renders"
    out.mkdir(parents=True, exist_ok=True)
    rel = f"renders/v{version}_c{concept}.png"
    thumb_rel = f"renders/v{version}_c{concept}_thumb100.png"
    (job_dir / rel).write_bytes(b"fake-png-bytes")
    (job_dir / thumb_rel).write_bytes(b"fake-thumb-bytes")
    return [rel]


def _ready_job_with_concept(tmp_path, *, archetype: str = "full_bleed_art",
                            asset: str = "assets/c0_background.png") -> pipeline.JobState:
    """A job with exactly one 'ready' concept, its background art already
    painted -- the state every revision test starts from."""
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    direction = _direction(archetype)
    spec = build_spec(direction, job.brief, ARCHETYPES[archetype])
    if asset:
        for art in spec.art:
            if art.id == "background":
                art.asset = asset
    job.concepts = [ConceptState(spec=spec, status="ready",
                                 renders=["renders/v1_c0.png"])]
    job.status = "ready"
    pipeline._write_state(tmp_path, job)
    return job


# -- new_job_id -----------------------------------------------------------------

def test_new_job_id_matches_the_documented_shape():
    job_id = pipeline.new_job_id()
    assert re.fullmatch(r"\d{8}-[0-9a-f]{6}", job_id)


# -- create_job / manuscript handling (§8.1) -----------------------------------

def test_create_job_without_a_manuscript(tmp_path):
    job = pipeline.create_job(tmp_path, _brief())
    assert job.status == "directing"
    assert job.manuscript_name == "" and job.word_count == 0
    assert job.concepts == [] and job.ledger == []
    assert (tmp_path / job.job_id / pipeline.JOB_MANIFEST).is_file()
    assert not (tmp_path / job.job_id / pipeline.MANUSCRIPT_SAMPLE_NAME).exists()


def test_create_job_with_a_manuscript_persists_only_the_sample(tmp_path):
    ms_path = tmp_path / "upload" / "book.txt"
    ms_path.parent.mkdir()
    full_text = "A ship sailed into the fog. " * 400
    ms_path.write_text(full_text, encoding="utf-8")

    job = pipeline.create_job(tmp_path, _brief(), manuscript_path=ms_path,
                              manuscript_name="book.txt")

    assert job.manuscript_name == "book.txt"
    assert job.word_count > 0
    sample_path = tmp_path / job.job_id / pipeline.MANUSCRIPT_SAMPLE_NAME
    assert sample_path.is_file()
    sample = sample_path.read_text(encoding="utf-8")
    assert "OPENING SAMPLE:" in sample
    assert "A ship sailed into the fog." in sample
    assert sample != full_text          # the sample, never the whole manuscript


def test_create_job_unreadable_manuscript_raises_before_touching_disk(tmp_path):
    ms_path = tmp_path / "upload" / "empty.txt"
    ms_path.parent.mkdir()
    ms_path.write_text("   \n   \n", encoding="utf-8")     # whitespace only
    before = set(tmp_path.iterdir())

    with pytest.raises(IngestError, match="no readable text"):
        pipeline.create_job(tmp_path, _brief(), manuscript_path=ms_path,
                            manuscript_name="empty.txt")
    assert set(tmp_path.iterdir()) == before                # no job dir left behind


# -- job.json persistence ------------------------------------------------------

def test_load_job_round_trips_what_create_job_wrote(tmp_path):
    created = pipeline.create_job(tmp_path, _brief(title="Roundtrip"))
    loaded = pipeline.load_job(tmp_path, created.job_id)
    assert loaded is not None
    assert loaded.job_id == created.job_id
    assert loaded.brief.title == "Roundtrip"


def test_load_job_returns_none_for_an_id_that_does_not_exist(tmp_path):
    assert pipeline.load_job(tmp_path, "20260101-abcdef") is None


def test_list_jobs_sorts_newest_first_and_respects_limit(tmp_path):
    for i in range(3):
        job = pipeline.create_job(tmp_path, _brief(title=f"Book {i}"))
        job.created = f"2026-08-2{i}T00:00:00+00:00"
        pipeline._write_state(tmp_path, job)

    jobs = pipeline.list_jobs(tmp_path, limit=2)
    assert [j.brief.title for j in jobs] == ["Book 2", "Book 1"]


def test_list_jobs_on_a_root_that_does_not_exist_yet(tmp_path):
    assert pipeline.list_jobs(tmp_path / "nope") == []


def test_total_usd_sums_the_ledger(tmp_path):
    job = pipeline.create_job(tmp_path, _brief())
    job.ledger = [{"kind": "direction", "detail": "x", "usd": 0.02},
                 {"kind": "image", "detail": "y", "usd": 0.05},
                 {"kind": "image", "detail": "z", "usd": 0.0}]
    assert pipeline.total_usd(job) == pytest.approx(0.07)


def test_default_root_reads_the_env_var_or_falls_back(monkeypatch, tmp_path):
    monkeypatch.delenv("COVER_DATA_PATH", raising=False)
    assert pipeline.default_root() == Path("cover_jobs")
    monkeypatch.setenv("COVER_DATA_PATH", str(tmp_path / "cover"))
    assert pipeline.default_root() == tmp_path / "cover"


# -- run_job: happy path (§8) ---------------------------------------------------

def test_run_job_reaches_ready_mixing_a_procedural_and_a_painted_concept(
        tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=2))
    directions = [_direction("big_type"), _direction("full_bleed_art")]
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=directions, model="claude-fable-5", cost=0.02))

    calls = []
    def fake_generate(client, prompt, *, transparent=False, resolution="2K"):
        calls.append((client, prompt, transparent, resolution))
        return b"fake-png-bytes"
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    monkeypatch.setattr(pipeline, "has_real_alpha", lambda png: True)
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "ready"
    assert [c.status for c in result.concepts] == ["ready", "ready"]
    # big_type needed zero generated images; full_bleed_art needed exactly one.
    assert len(calls) == 1
    client, prompt, transparent, resolution = calls[0]
    assert client is IMAGE_CLIENT
    assert "A lonely lighthouse at dusk, oil painting." in prompt   # slot.prompt
    assert ARCHETYPES["full_bleed_art"].composition_note in prompt
    assert pipeline.NEGATIVE_SUFFIX in prompt
    assert resolution == pipeline.IMAGE_RESOLUTION

    kinds = [row["kind"] for row in result.ledger]
    assert kinds.count("direction") == 1
    assert kinds.count("image") == 1
    assert pipeline.total_usd(result) > 0
    assert result.concepts[0].renders and result.concepts[1].renders
    assert result.concepts[0].report is not None


def test_run_job_direction_call_uses_the_direction_role_provider(tmp_path, monkeypatch):
    ms_path = tmp_path / "book.txt"
    ms_path.write_text("A ship sailed into the fog. " * 400, encoding="utf-8")
    job = pipeline.create_job(tmp_path, _brief(concepts=1), manuscript_path=ms_path,
                              manuscript_name="book.txt")

    received = {}
    def fake_run_directions(brief, provider, *, n, manuscript_sample="", **kw):
        received["brief"] = brief
        received["provider"] = provider
        received["n"] = n
        received["sample"] = manuscript_sample
        return DirectionResult(directions=[_direction("big_type")],
                               model="m", cost=0.0)
    monkeypatch.setattr(pipeline, "run_directions", fake_run_directions)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    assert received["provider"] is DIRECTION_PROVIDER
    assert received["n"] == 1
    # Without a working reality distillation (not monkeypatched in this
    # test -> it runs for real against DIRECTION_PROVIDER-shaped sentinels
    # and fails, which is fine: the raw sample is what falls back in here.
    assert "A ship sailed into the fog." in received["sample"]


def test_run_job_with_no_manuscript_passes_an_empty_sample(tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    received = {}
    def fake_run_directions(brief, provider, *, n, manuscript_sample="", **kw):
        received["sample"] = manuscript_sample
        return DirectionResult(directions=[_direction("big_type")], model="m", cost=0.0)
    monkeypatch.setattr(pipeline, "run_directions", fake_run_directions)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))
    assert received["sample"] == ""


def test_run_job_direction_error_ends_the_job_in_error(tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief())
    def boom(*a, **k):
        raise DirectionError("The art-direction call failed: no budget.")
    monkeypatch.setattr(pipeline, "run_directions", boom)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "error"
    assert "no budget" in result.error
    assert result.concepts == []


def test_run_job_per_concept_isolation_and_incremental_persistence(
        tmp_path, monkeypatch):
    """One concept's ImagingError must not kill its sibling (§8), and the
    sibling's success must survive on disk even though the failing concept's
    own commit happens later in the same gather -- i.e. per-concept writes
    merge into job.json rather than one clobbering the other. This is the
    "kill it mid-flight, reload from disk" persistence guarantee: whichever
    concept finishes first is durably recorded before the other is even
    known to have failed."""
    job = pipeline.create_job(tmp_path, _brief(concepts=2))
    directions = [
        _direction("full_bleed_art", concept_name="Good",
                  art_prompts={"background": "GOOD PROMPT, oil painting."}),
        _direction("full_bleed_art", concept_name="Bad",
                  art_prompts={"background": "BOOM PROMPT, oil painting."}),
    ]
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=directions, model="m", cost=0.01))

    def fake_generate(client, prompt, *, transparent=False, resolution="2K"):
        if "BOOM" in prompt:
            raise pipeline.ImagingError("Image generation failed: the model refused.")
        return b"fake-png-bytes"
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    monkeypatch.setattr(pipeline, "has_real_alpha", lambda png: True)
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "ready"            # the job itself IS terminal
    assert result.concepts[0].status == "ready"
    assert result.concepts[0].renders           # concept 0's success was not clobbered
    assert result.concepts[1].status == "error"
    assert "refused" in result.concepts[1].error
    assert any(row["kind"] == "image" for row in result.ledger)   # concept 0's image billed


# -- reality-sheet distillation wiring (BRAIN wave) ----------------------------

def _fake_reality_result(**overrides) -> RealityResult:
    data = dict(setting="a foggy harbor town", era="Victorian",
               palette_cues=["slate gray", "lamp-oil amber"],
               concrete_objects=["a lighthouse", "a rowboat"],
               motifs=["recurring fog"], atmosphere="Elegiac and salt-scoured.",
               never_show=[])
    data.update(overrides)
    sheet = RealitySheet(**data)
    return RealityResult(sheet=sheet, rendered=render_reality_sheet(sheet),
                         model="claude-sonnet-5", cost=0.0021)


def test_run_job_distills_a_reality_sheet_and_hands_directions_the_rendered_form(
        tmp_path, monkeypatch):
    ms_path = tmp_path / "book.txt"
    ms_path.write_text("A ship sailed into the fog. " * 400, encoding="utf-8")
    job = pipeline.create_job(tmp_path, _brief(concepts=1), manuscript_path=ms_path,
                              manuscript_name="book.txt")

    distill_received = {}
    reality_result = _fake_reality_result()
    def fake_distill_reality(sample, provider, **kw):
        distill_received["sample"] = sample
        distill_received["provider"] = provider
        return reality_result
    monkeypatch.setattr(pipeline, "distill_reality", fake_distill_reality)

    direction_received = {}
    def fake_run_directions(brief, provider, *, n, manuscript_sample="", **kw):
        direction_received["sample"] = manuscript_sample
        return DirectionResult(directions=[_direction("big_type")], model="m", cost=0.0)
    monkeypatch.setattr(pipeline, "run_directions", fake_run_directions)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    # distill_reality got the RAW sample and the reality-role provider.
    assert distill_received["provider"] is REALITY_PROVIDER
    assert "A ship sailed into the fog." in distill_received["sample"]

    # run_directions got the RENDERED SHEET, not the raw sample.
    assert direction_received["sample"] == reality_result.rendered
    assert "Setting: a foggy harbor town" in direction_received["sample"]
    assert "A ship sailed into the fog." not in direction_received["sample"]

    # reality.json persisted; a ledger row records the distillation.
    reality_path = tmp_path / job.job_id / pipeline.REALITY_SHEET_NAME
    assert reality_path.is_file()
    assert RealitySheet.model_validate_json(reality_path.read_text()) == \
        reality_result.sheet

    result = pipeline.load_job(tmp_path, job.job_id)
    reality_row = next(r for r in result.ledger if r["kind"] == "reality")
    assert reality_row["usd"] == pytest.approx(0.0021)


def test_run_job_reality_distillation_failure_falls_back_to_the_raw_sample(
        tmp_path, monkeypatch):
    ms_path = tmp_path / "book.txt"
    ms_path.write_text("A ship sailed into the fog. " * 400, encoding="utf-8")
    job = pipeline.create_job(tmp_path, _brief(concepts=1), manuscript_path=ms_path,
                              manuscript_name="book.txt")

    def boom(*a, **k):
        raise RealitySheetError("The reality-sheet call failed: no budget.")
    monkeypatch.setattr(pipeline, "distill_reality", boom)

    direction_received = {}
    def fake_run_directions(brief, provider, *, n, manuscript_sample="", **kw):
        direction_received["sample"] = manuscript_sample
        return DirectionResult(directions=[_direction("big_type")], model="m", cost=0.0)
    monkeypatch.setattr(pipeline, "run_directions", fake_run_directions)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    # The raw sample, unchanged -- never blocked the job.
    assert "A ship sailed into the fog." in direction_received["sample"]
    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "ready"
    assert not (tmp_path / job.job_id / pipeline.REALITY_SHEET_NAME).exists()
    reality_row = next(r for r in result.ledger if r["kind"] == "reality")
    assert reality_row["usd"] == 0.0
    assert "no budget" in reality_row["detail"]


def test_run_job_with_no_manuscript_never_calls_distill_reality(tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    def fail_distill(*a, **k):
        raise AssertionError("distill_reality should not run without a manuscript")
    monkeypatch.setattr(pipeline, "distill_reality", fail_distill)
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.0))

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert not any(r["kind"] == "reality" for r in result.ledger)
    assert not (tmp_path / job.job_id / pipeline.REALITY_SHEET_NAME).exists()


# -- run_revision (§6.2, §8) -----------------------------------------------------

def test_run_revision_without_new_art_recomposes_and_spends_no_image_money(
        tmp_path, monkeypatch):
    job = _ready_job_with_concept(tmp_path)

    def fake_revise_spec(spec, notes, provider, **kw):
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes]})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    def fail_generate(*a, **k):
        raise AssertionError("generate should not run for an art-unchanged revision")
    monkeypatch.setattr(pipeline, "generate", fail_generate)
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    asyncio.run(pipeline.run_revision(tmp_path, job.job_id, 0,
                                      "make the title bigger", False,
                                      PROVIDERS, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert concept.spec.version == 2
    assert concept.spec.notes_log == ["make the title bigger"]
    assert len(concept.renders) == 2            # the original render plus the new one
    assert any(row["kind"] == "revision" for row in result.ledger)
    assert not any(row["kind"] == "image" for row in result.ledger)


def test_run_revision_uses_the_revision_role_provider(tmp_path, monkeypatch):
    job = _ready_job_with_concept(tmp_path)
    received = {}
    def fake_revise_spec(spec, notes, provider, **kw):
        received["provider"] = provider
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes]})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    asyncio.run(pipeline.run_revision(tmp_path, job.job_id, 0, "notes", False,
                                      PROVIDERS, IMAGE_CLIENT))

    assert received["provider"] is REVISION_PROVIDER


def test_run_revision_gains_a_code_computed_diff_note(tmp_path, monkeypatch):
    job = _ready_job_with_concept(tmp_path)

    def fake_revise_spec(spec, notes, provider, **kw):
        new_palette = spec.palette.model_copy(update={"primary": "#a83250"})
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes],
                                         "palette": new_palette})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    asyncio.run(pipeline.run_revision(tmp_path, job.job_id, 0,
                                      "make the palette warmer", False,
                                      PROVIDERS, IMAGE_CLIENT))

    concept = pipeline.load_job(tmp_path, job.job_id).concepts[0]
    assert concept.spec.notes_log[0] == "make the palette warmer"    # the human's own words
    assert concept.spec.notes_log[1].startswith("[changed] ")
    assert "palette.primary" in concept.spec.notes_log[1]
    assert "→#a83250" in concept.spec.notes_log[1]


def test_run_revision_diff_note_absent_when_nothing_changed(tmp_path, monkeypatch):
    job = _ready_job_with_concept(tmp_path)

    def fake_revise_spec(spec, notes, provider, **kw):
        # Only the mechanical version bump + notes_log append -- no design
        # field actually differs.
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes]})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    asyncio.run(pipeline.run_revision(tmp_path, job.job_id, 0, "a retry with no real ask",
                                      False, PROVIDERS, IMAGE_CLIENT))

    concept = pipeline.load_job(tmp_path, job.job_id).concepts[0]
    assert concept.spec.notes_log == ["a retry with no real ask"]      # no [changed] entry


def test_run_revision_allow_new_art_regenerates_only_the_cleared_slot(
        tmp_path, monkeypatch):
    job = _ready_job_with_concept(tmp_path, asset="assets/c0_background.png")

    def fake_revise_spec(spec, notes, provider, **kw):
        new_art = [a.model_copy(update={"asset": "", "prompt": "A new scene, gouache."})
                  if a.id == "background" else a for a in spec.art]
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes],
                                         "art": new_art})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    calls = []
    def fake_generate(client, prompt, *, transparent=False, resolution="2K"):
        calls.append(prompt)
        return b"new-png-bytes"
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    monkeypatch.setattr(pipeline, "has_real_alpha", lambda png: True)
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    asyncio.run(pipeline.run_revision(tmp_path, job.job_id, 0, "different imagery",
                                      True, PROVIDERS, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert len(calls) == 1
    assert "A new scene, gouache." in calls[0]
    background = next(a for a in concept.spec.art if a.id == "background")
    assert background.asset == "assets/c0_background.png"
    assert any(row["kind"] == "image" for row in result.ledger)


def test_run_revision_without_allow_new_art_restores_the_prior_asset(
        tmp_path, monkeypatch):
    job = _ready_job_with_concept(tmp_path, asset="assets/c0_background.png")

    def fake_revise_spec(spec, notes, provider, **kw):
        new_art = [a.model_copy(update={"asset": "", "prompt": "A new scene, gouache."})
                  if a.id == "background" else a for a in spec.art]
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes],
                                         "art": new_art})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    def fail_generate(*a, **k):
        raise AssertionError("generate should not run when allow_new_art is False")
    monkeypatch.setattr(pipeline, "generate", fail_generate)
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    asyncio.run(pipeline.run_revision(tmp_path, job.job_id, 0, "different imagery",
                                      False, PROVIDERS, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    background = next(a for a in concept.spec.art if a.id == "background")
    assert background.asset == "assets/c0_background.png"       # restored, not regenerated
    assert not any(row["kind"] == "image" for row in result.ledger)
    assert any("art regen is off" in row["detail"] for row in result.ledger
              if row["kind"] == "revision")


def test_run_revision_error_lands_the_concept_in_error_and_keeps_the_old_spec(
        tmp_path, monkeypatch):
    job = _ready_job_with_concept(tmp_path)
    prior_version = job.concepts[0].spec.version

    def fake_revise_spec(spec, notes, provider, **kw):
        raise RevisionError("The revised spec did not match the schema.")
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    asyncio.run(pipeline.run_revision(tmp_path, job.job_id, 0, "notes", False,
                                      PROVIDERS, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "error"
    assert "did not match the schema" in concept.error
    assert concept.spec.version == prior_version


def test_run_revision_on_a_vanished_job_is_a_silent_no_op(tmp_path):
    # No job at this id at all -- the route already 404s before spawning this
    # as a background task, but the function itself must not raise if a job
    # somehow disappeared between the check and the task running.
    asyncio.run(pipeline.run_revision(tmp_path, "20260101-abcdef", 0, "notes",
                                      False, PROVIDERS, IMAGE_CLIENT))


def test_run_revision_rejects_an_unknown_archetype_and_keeps_the_prior_version(
        tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)
    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    monkeypatch.setattr(pipeline, "revise_spec",
                        lambda spec, notes, provider, **kw: RevisionResult(
                            spec=spec.model_copy(update={
                                "archetype": "horror_emblem",
                                "version": spec.version + 1,
                                "notes_log": [*spec.notes_log, notes]}),
                            cost=0.01))
    asyncio.run(pipeline.run_revision(tmp_path, job.job_id, 0,
                                      "switch to horror_emblem", False,
                                      PROVIDERS, IMAGE_CLIENT))

    concept = pipeline.load_job(tmp_path, job.job_id).concepts[0]
    assert concept.status == "error"
    assert "not one of the shipped archetypes" in concept.error
    # The prior version survives untouched — a retry is not doomed.
    assert concept.spec.archetype == "big_type"
    assert concept.spec.version == 1


# -- stale-job / interrupted detection (§8) --------------------------------------

def test_check_interrupted_marks_a_job_with_no_live_task_as_error(tmp_path):
    job = pipeline.create_job(tmp_path, _brief())
    job.status = "working"
    pipeline._write_state(tmp_path, job)
    assert not pipeline.is_job_alive(job.job_id)

    result = pipeline.check_interrupted(tmp_path, job)
    assert result.status == "error"
    assert result.error == "interrupted — run it again"
    assert pipeline.load_job(tmp_path, job.job_id).status == "error"


def test_check_interrupted_also_flags_a_concept_stuck_mid_revision(tmp_path):
    job = _ready_job_with_concept(tmp_path)
    job.concepts[0].status = "composing"        # a revision that never finished
    pipeline._write_state(tmp_path, job)

    result = pipeline.check_interrupted(tmp_path, job)
    assert result.concepts[0].status == "error"
    assert result.concepts[0].error == "interrupted — run it again"


def test_check_interrupted_is_a_no_op_for_a_terminal_job(tmp_path):
    job = pipeline.create_job(tmp_path, _brief())
    job.status = "ready"
    pipeline._write_state(tmp_path, job)
    result = pipeline.check_interrupted(tmp_path, job)
    assert result.status == "ready" and result.error is None


def test_check_interrupted_leaves_a_job_with_a_live_task_untouched(tmp_path):
    job = pipeline.create_job(tmp_path, _brief())
    job.status = "working"
    pipeline._write_state(tmp_path, job)

    async def _run():
        task = asyncio.create_task(asyncio.sleep(10))
        pipeline.register_task(job.job_id, task)
        assert pipeline.is_job_alive(job.job_id)

        result = pipeline.check_interrupted(tmp_path, job)
        assert result.status == "working"        # untouched -- a task is still live

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)          # let the done-callback deregister it

    asyncio.run(_run())
    assert not pipeline.is_job_alive(job.job_id)


def test_register_task_deregisters_itself_once_the_task_completes():
    async def _run():
        task = asyncio.create_task(asyncio.sleep(0))
        pipeline.register_task("job-deregister-test", task)
        assert pipeline.is_job_alive("job-deregister-test")
        await task
        await asyncio.sleep(0)          # let the done-callback run
        assert not pipeline.is_job_alive("job-deregister-test")

    asyncio.run(_run())


# -- diff_spec_fields / _dump_equal_ignoring_bookkeeping: pure functions ------
# (§ iterating judge loop -- "revising did nothing" made visibly impossible)

def test_diff_spec_fields_reports_palette_zone_and_size_changes():
    old = _spec_for_diffing()
    new_palette = old.palette.model_copy(update={"primary": "#a83250"})
    new_text = [t.model_copy(update={
                    "zone": t.zone.model_copy(update={"y": t.zone.y - 0.08}),
                    "size_max": 0.13}) if t.id == "title" else t
               for t in old.text]
    new = old.model_copy(update={"version": old.version + 1, "notes_log": ["x"],
                                 "palette": new_palette, "text": new_text})

    diff = pipeline.diff_spec_fields(old, new)

    assert "palette.primary" in diff and "→#a83250" in diff
    assert "title zone up 8%" in diff
    assert "title size_max" in diff and "→0.13" in diff
    # semicolon-joined clauses, per the spec's own example format
    assert diff.count(";") == diff.count("; ") and "; " in diff


def test_diff_spec_fields_is_empty_when_only_bookkeeping_changed():
    old = _spec_for_diffing()
    new = old.model_copy(update={"version": old.version + 1, "notes_log": ["a note"]})
    assert pipeline.diff_spec_fields(old, new) == ""


def test_diff_spec_fields_reports_an_archetype_switch():
    old = _spec_for_diffing()
    new = old.model_copy(update={"archetype": "big_type"})
    assert "archetype full_bleed_art→big_type" in pipeline.diff_spec_fields(old, new)


def test_diff_spec_fields_reports_a_prompt_rewrite_without_dumping_the_full_text():
    old = _spec_for_diffing()
    new_art = [a.model_copy(update={"prompt": "Something else entirely, gouache."})
              if a.id == "background" else a for a in old.art]
    new = old.model_copy(update={"art": new_art})
    diff = pipeline.diff_spec_fields(old, new)
    assert "background.prompt rewritten" in diff
    assert "Something else entirely" not in diff


def test_diff_spec_fields_reports_a_scrim_strength_change():
    old = _spec_for_diffing()
    new_scrims = [s.model_copy(update={"strength": s.strength + 0.15}) if i == 0
                 else s for i, s in enumerate(old.scrims)]
    new = old.model_copy(update={"scrims": new_scrims})
    diff = pipeline.diff_spec_fields(old, new)
    assert "scrim[0].strength" in diff


def test_dump_equal_ignoring_bookkeeping_true_only_for_bookkeeping_fields():
    old = _spec_for_diffing()
    same_design = old.model_copy(update={"version": old.version + 1,
                                         "notes_log": ["a note"]})
    assert pipeline._dump_equal_ignoring_bookkeeping(old, same_design) is True

    different = old.model_copy(update={
        "palette": old.palette.model_copy(update={"primary": "#ffffff"})})
    assert pipeline._dump_equal_ignoring_bookkeeping(old, different) is False


# -- critique pass (§6.3, ITERATED — BRAIN wave) -------------------------------
# Each test below overrides the file's autouse _default_critique_passes fixture
# with its own monkeypatch.setattr(pipeline, "run_critique", ...) to exercise a
# specific verdict/failure path.

def test_run_job_critique_passes_ships_without_a_revision_round(tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    critique_calls = []
    def fake_run_critique(png_bytes, thumb_bytes, spec, brief, client, **kw):
        critique_calls.append((spec.version, client, brief.title))
        return CritiqueResult(passes=True, tells=[], notes="", cost=0.0007)
    monkeypatch.setattr(pipeline, "run_critique", fake_run_critique)

    def fail_revise(*a, **k):
        raise AssertionError("revise_spec should not run when critique passes")
    monkeypatch.setattr(pipeline, "revise_spec", fail_revise)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert concept.spec.version == 1                  # no auto-revision -> no version bump
    assert len(critique_calls) == 1                    # exactly one round when it passes
    version, client, title = critique_calls[0]
    assert client is CRITIQUE_CLIENT                    # the separate anthropic client
    assert title == job.brief.title

    kinds = [row["kind"] for row in result.ledger]
    assert kinds.count("critique") == 1
    critique_row = next(r for r in result.ledger if r["kind"] == "critique")
    assert critique_row["usd"] == pytest.approx(0.0007)
    assert "round 1" in critique_row["detail"]


def test_run_job_critique_fails_then_passes_iterates_exactly_two_rounds(
        tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    verdicts = [
        CritiqueResult(passes=False, tells=["weak hierarchy"],
                       notes="Enlarge the title.", cost=0.0007),
        CritiqueResult(passes=True, tells=[], notes="", cost=0.0007),
    ]
    critique_calls = []
    def fake_run_critique(png_bytes, thumb_bytes, spec, brief, client, **kw):
        critique_calls.append(spec.version)
        assert client is CRITIQUE_CLIENT
        return verdicts.pop(0)
    monkeypatch.setattr(pipeline, "run_critique", fake_run_critique)

    def fake_revise_spec(spec, notes, provider, **kw):
        assert notes == "Enlarge the title."
        assert provider is REVISION_PROVIDER
        # A real revision driven by "Enlarge the title" would actually
        # enlarge it -- bumping size_max keeps this fake from tripping the
        # iterating loop's own identical-spec early stop (§ iterating judge
        # loop), which is exactly what a no-op-except-bookkeeping revision
        # is designed to catch.
        new_text = [t.model_copy(update={"size_max": t.size_max + 0.01})
                   if t.id == "title" else t for t in spec.text]
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes],
                                         "text": new_text})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    def fail_generate(*a, **k):
        raise AssertionError(
            "generate should never run for a design-only auto-critique revision")
    monkeypatch.setattr(pipeline, "generate", fail_generate)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert concept.spec.version == 2
    assert concept.spec.notes_log[0] == "[auto-critique r1] Enlarge the title."
    assert concept.spec.notes_log[1].startswith("[auto-critique r1 changed] ")
    assert critique_calls == [1, 2]                     # critiqued v1, then the revised v2
    assert verdicts == []                                # both canned verdicts consumed

    kinds = [row["kind"] for row in result.ledger]
    assert kinds.count("critique") == 2
    revision_rows = [r for r in result.ledger if r["kind"] == "revision"]
    assert len(revision_rows) == 1
    assert "auto-critique revision" in revision_rows[0]["detail"]
    assert "round 1" in revision_rows[0]["detail"]
    # round-numbered ledger rows read as a sequence
    critique_rows = [r for r in result.ledger if r["kind"] == "critique"]
    assert any("round 1" in r["detail"] for r in critique_rows)
    assert any("round 2" in r["detail"] for r in critique_rows)
    # two renders on file: the original composition and the post-revision one
    assert len(concept.renders) == 2


def test_run_job_critique_stops_at_the_round_cap_with_no_final_confirming_critique(
        tmp_path, monkeypatch):
    """A critique that STILL fails on the very last permitted round ships
    with its own tells as leftover warnings rather than buying a revision
    nothing will ever re-check -- MAX_CRITIQUE_ROUNDS=4 therefore means at
    most 4 critique calls and at most 3 revisions, not 4 of each."""
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    verdicts = [CritiqueResult(passes=False, tells=[f"tell {n}"], notes=f"note {n}",
                               cost=0.0007) for n in range(1, 5)]
    monkeypatch.setattr(pipeline, "run_critique", lambda *a, **k: verdicts.pop(0))

    revise_calls = []
    def fake_revise_spec(spec, notes, provider, **kw):
        revise_calls.append(notes)
        # A genuinely different accent hex each round, so dump-equality
        # never fires before the cap does.
        new_palette = spec.palette.model_copy(
            update={"accent": f"#{len(revise_calls):06d}"})
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes],
                                         "palette": new_palette})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"                    # still ships -- never blocks
    assert concept.spec.version == 4                     # 3 revisions applied (rounds 1-3)
    assert len(revise_calls) == 3                         # round 4 never revises
    assert verdicts == []                                 # all 4 canned verdicts consumed

    kinds = [row["kind"] for row in result.ledger]
    assert kinds.count("critique") == 4                   # the round cap, exactly
    assert kinds.count("revision") == 3

    # round 4's still-failing verdict is the FINAL one -> its tell rides
    # along as a leftover warning; round 1's tell (long since "fixed", or at
    # least attempted) does not.
    assert "tell 4" in concept.report.warnings
    assert "tell 1" not in concept.report.warnings


def test_run_job_critique_identical_revision_stops_the_loop_early(
        tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    monkeypatch.setattr(pipeline, "run_critique", lambda *a, **k: CritiqueResult(
        passes=False, tells=["weak hierarchy"], notes="Enlarge the title.", cost=0.0007))

    def fake_revise_spec(spec, notes, provider, **kw):
        # The model echoes the design back UNCHANGED -- only the mechanical
        # version bump + notes_log append revise_spec always does. "Nothing
        # more to give."
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes]})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    # compose/save_renders would be called again if the loop mistakenly
    # recomposed the identical revision -- assert it does NOT by counting.
    compose_calls = []
    def counting_compose(spec, job_dir):
        compose_calls.append(spec.version)
        return _fake_compose(spec, job_dir)
    monkeypatch.setattr(pipeline, "compose", counting_compose)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert concept.spec.version == 1                       # the identical revision was DISCARDED
    assert compose_calls == [1]                              # composed once, never recomposed
    kinds = [row["kind"] for row in result.ledger]
    assert kinds.count("critique") == 1                      # stopped right after round 1
    assert any("identical spec" in r["detail"] for r in result.ledger
              if r["kind"] == "revision")
    assert "weak hierarchy" in concept.report.warnings        # the triggering verdict's tell


def test_run_job_critique_call_failure_still_reaches_ready(tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    def fake_run_critique(*a, **k):
        raise pipeline.CritiqueError("The critique model refused to answer.")
    monkeypatch.setattr(pipeline, "run_critique", fake_run_critique)

    def fail_revise(*a, **k):
        raise AssertionError("revise_spec should not run -- no verdict was ever reached")
    monkeypatch.setattr(pipeline, "revise_spec", fail_revise)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"                    # §6.3: never blocks a cover
    assert concept.spec.version == 1
    critique_row = next(r for r in result.ledger if r["kind"] == "critique")
    assert critique_row["usd"] == 0.0
    assert "refused" in critique_row["detail"]
    assert "round 1" in critique_row["detail"]


def test_run_job_critique_iterates_through_multiple_failing_rounds_until_clean(
        tmp_path, monkeypatch):
    # Unlike v1's single fixed round, a second (or third) failing verdict no
    # longer just rides along as a leftover warning -- it triggers ANOTHER
    # revision round, up to the cap. Only tells from the FINAL verdict ever
    # land in RenderReport.warnings; a clean final pass means none do.
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    verdicts = [
        CritiqueResult(passes=False, tells=["weak hierarchy"],
                       notes="Enlarge the title.", cost=0.0007),
        CritiqueResult(passes=False, tells=["still a bit crowded"],
                       notes="Tighten the tracking.", cost=0.0007),
        CritiqueResult(passes=True, tells=[], notes="", cost=0.0007),
    ]
    monkeypatch.setattr(pipeline, "run_critique", lambda *a, **k: verdicts.pop(0))

    def fake_revise_spec(spec, notes, provider, **kw):
        new_palette = spec.palette.model_copy(
            update={"accent": "#111111" if spec.version == 1 else "#222222"})
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes],
                                         "palette": new_palette})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"                    # ships once it passes
    assert concept.spec.version == 3                     # two auto-revision rounds ran
    assert "still a bit crowded" not in concept.report.warnings   # fixed, so no longer final
    assert "weak hierarchy" not in concept.report.warnings         # fixed even earlier
    assert concept.report.warnings == []                            # the FINAL verdict passed clean


def test_run_job_critique_auto_revision_failure_still_reaches_ready(tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)
    monkeypatch.setattr(pipeline, "run_critique", lambda *a, **k: CritiqueResult(
        passes=False, tells=["weak hierarchy"], notes="Enlarge the title.", cost=0.0007))

    def fail_revise(spec, notes, provider, **kw):
        raise pipeline.RevisionError("The revised spec did not match the schema.")
    monkeypatch.setattr(pipeline, "revise_spec", fail_revise)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert concept.spec.version == 1                     # the revision never actually applied
    assert "weak hierarchy" in concept.report.warnings
    revision_rows = [r for r in result.ledger if r["kind"] == "revision"]
    assert len(revision_rows) == 1
    assert revision_rows[0]["usd"] == 0.0
    assert "round 1" in revision_rows[0]["detail"]


def test_run_job_critique_per_concept_isolation(tmp_path, monkeypatch):
    # A critique failure on one concept must not affect a sibling's own
    # critique verdict -- each concept's run through _critique_and_revise is
    # independent, same as painting/composing already is (§8).
    job = pipeline.create_job(tmp_path, _brief(concepts=2))
    directions = [_direction("big_type", concept_name="Good"),
                 _direction("big_type", concept_name="Bad")]
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=directions, model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    def fake_run_critique(png_bytes, thumb_bytes, spec, brief, client, **kw):
        if spec.concept_name == "Bad":
            raise pipeline.CritiqueError("boom")
        return CritiqueResult(passes=True, tells=[], notes="", cost=0.0007)
    monkeypatch.setattr(pipeline, "run_critique", fake_run_critique)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "ready"
    assert [c.status for c in result.concepts] == ["ready", "ready"]
    good_row = next(r for r in result.ledger
                    if r["kind"] == "critique" and "passed" in r["detail"])
    bad_row = next(r for r in result.ledger
                   if r["kind"] == "critique" and "failed" in r["detail"])
    assert good_row["usd"] == pytest.approx(0.0007)
    assert bad_row["usd"] == 0.0


def test_run_job_critique_receives_adjustments_alongside_warnings(
        tmp_path, monkeypatch):
    # §15.10: RenderReport.adjustments (what the balance snap pass moved)
    # ride into the judge's composer_warnings channel right behind the
    # warnings themselves — the "near-miss alignment survived" tell is
    # only checkable against what actually moved.
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    snap_line = ("text 'title': ink center 49.00% → 50.00% of width — "
                 "snapped onto the center axis (+4px).")
    monkeypatch.setattr(pipeline, "compose", lambda spec, job_dir: (
        FAKE_IMAGE, _report(warnings=["weak hierarchy"],
                            adjustments=[snap_line])))
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    seen: dict[str, list[str]] = {}
    def fake_run_critique(png_bytes, thumb_bytes, spec, brief, client, **kw):
        seen["composer_warnings"] = list(kw.get("composer_warnings", ()))
        return CritiqueResult(passes=True, tells=[], notes="", cost=0.0007)
    monkeypatch.setattr(pipeline, "run_critique", fake_run_critique)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    assert seen["composer_warnings"] == ["weak hierarchy", snap_line]


def test_run_revision_human_triggered_does_not_call_run_critique(tmp_path, monkeypatch):
    job = _ready_job_with_concept(tmp_path)

    def fail_critique(*a, **k):
        raise AssertionError(
            "run_critique must not run for a human-triggered revision (§6.3: "
            "the human is the critic there)")
    monkeypatch.setattr(pipeline, "run_critique", fail_critique)

    def fake_revise_spec(spec, notes, provider, **kw):
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes]})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    asyncio.run(pipeline.run_revision(tmp_path, job.job_id, 0, "make it bigger",
                                      False, PROVIDERS, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.concepts[0].status == "ready"
    assert result.concepts[0].spec.version == 2
    assert not any(row["kind"] == "critique" for row in result.ledger)


def test_run_job_auto_critique_revision_that_clears_art_keeps_the_existing_art(
        tmp_path, monkeypatch):
    # The code-level allow_new_art=False backstop: even when the auto-
    # critique revision changes an art prompt (which clears the asset — the
    # regenerate signal), the auto round restores the prior art instead of
    # recomposing a blank layer, and never calls generate a second time.
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("full_bleed_art")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)
    monkeypatch.setattr(pipeline, "has_real_alpha", lambda png: True)

    generate_calls = []
    def fake_generate(client, prompt, *, transparent=False, resolution="2K"):
        generate_calls.append(prompt)
        return b"png-bytes"
    monkeypatch.setattr(pipeline, "generate", fake_generate)

    verdicts = [
        CritiqueResult(passes=False, tells=["muddy art"],
                       notes="Different imagery entirely.", cost=0.0007),
        CritiqueResult(passes=True, tells=[], notes="", cost=0.0007),
    ]
    monkeypatch.setattr(pipeline, "run_critique",
                        lambda *a, **k: verdicts.pop(0))

    def fake_revise_spec(spec, notes, provider, **kw):
        # Mimic the real revise_spec: the model rewrote the background's
        # prompt, so the diffing cleared its asset.
        art = [s.model_copy(update={"prompt": "something else", "asset": ""})
               if s.id == "background" else s for s in spec.art]
        revised = spec.model_copy(update={
            "version": spec.version + 1, "art": art,
            "notes_log": [*spec.notes_log, notes]})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert concept.spec.version == 2
    background = next(s for s in concept.spec.art if s.id == "background")
    assert background.asset                  # restored, not left blank
    assert len(generate_calls) == 1          # painted once, never repainted
    kept = [r for r in result.ledger
            if r["kind"] == "revision" and "kept the existing art" in r["detail"]]
    assert len(kept) == 1


def test_run_job_second_critique_passing_with_tells_still_records_them(
        tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    verdicts = [
        CritiqueResult(passes=False, tells=["weak hierarchy"],
                       notes="Enlarge the title.", cost=0.0007),
        CritiqueResult(passes=True, tells=["accent contrast a touch low"],
                       notes="", cost=0.0007),
    ]
    monkeypatch.setattr(pipeline, "run_critique",
                        lambda *a, **k: verdicts.pop(0))

    def fake_revise_spec(spec, notes, provider, **kw):
        # A genuine field change (see the sibling test's own comment on why
        # this matters for the identical-spec early stop).
        new_palette = spec.palette.model_copy(update={"accent": "#654321"})
        return RevisionResult(spec=spec.model_copy(update={
            "version": spec.version + 1,
            "notes_log": [*spec.notes_log, notes],
            "palette": new_palette}), cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    concept = pipeline.load_job(tmp_path, job.job_id).concepts[0]
    assert concept.status == "ready"
    # A pass-with-notes second verdict records its tells exactly like the
    # first critique would — the card's warning line is for it.
    assert "accent contrast a touch low" in concept.report.warnings


# -- critique pass: the 100px thumbnail (BRAIN v2.1) ---------------------------

def test_run_job_critique_reads_the_thumbnail_off_disk_when_present(
        tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders_with_thumb)

    received = {}
    def fake_run_critique(png_bytes, thumb_bytes, spec, brief, client, **kw):
        received["png"] = png_bytes
        received["thumb"] = thumb_bytes
        return CritiqueResult(passes=True, tells=[], notes="", cost=0.0007)
    monkeypatch.setattr(pipeline, "run_critique", fake_run_critique)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    assert received["png"] == b"fake-png-bytes"
    assert received["thumb"] == b"fake-thumb-bytes"


def test_run_job_critique_missing_thumbnail_degrades_to_none_not_a_failure(
        tmp_path, monkeypatch):
    # _fake_save_renders (unlike the _with_thumb variant above) never writes
    # a _thumb100.png -- exactly the "old job" / "caller never rendered one"
    # case run_critique's own docstring calls out. Must still reach a real
    # verdict, never get folded into the "critique call failed" path.
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    received = {}
    def fake_run_critique(png_bytes, thumb_bytes, spec, brief, client, **kw):
        received["thumb"] = thumb_bytes
        return CritiqueResult(passes=True, tells=[], notes="", cost=0.0007)
    monkeypatch.setattr(pipeline, "run_critique", fake_run_critique)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.concepts[0].status == "ready"          # never fails over a missing thumb
    assert received["thumb"] is None
    critique_row = next(r for r in result.ledger if r["kind"] == "critique")
    assert critique_row["usd"] > 0                        # a REAL verdict, not a failure note
    assert "passed" in critique_row["detail"]


# -- critique pass: the art-repaint escape hatch (BRAIN v2.1) ------------------

def test_run_job_critique_art_defect_triggers_a_repaint(tmp_path, monkeypatch):
    # A design-only revision can never fix a defective GENERATED image -- a
    # failing verdict naming art_defects clears those (real, generatable)
    # slots' assets and repaints them for real via generate(), on top of
    # whatever design-only edit the same round's notes also asked for. A
    # hallucinated slot id is dropped rather than blowing up the round.
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("cutout_sandwich")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)
    monkeypatch.setattr(pipeline, "has_real_alpha", lambda png: True)

    generate_calls = []
    def fake_generate(client, prompt, *, transparent=False, resolution="2K"):
        generate_calls.append(prompt)
        return b"png-bytes"
    monkeypatch.setattr(pipeline, "generate", fake_generate)

    verdicts = [
        CritiqueResult(passes=False, tells=["a surreal blob in the forest"],
                       notes="Tighten the tracking.", cost=0.0007,
                       art_defects=["nonexistent_slot", "background", "focal"]),
        CritiqueResult(passes=True, tells=[], notes="", cost=0.0007),
    ]
    monkeypatch.setattr(pipeline, "run_critique", lambda *a, **k: verdicts.pop(0))

    def fake_revise_spec(spec, notes, provider, **kw):
        new_text = [t.model_copy(update={"size_max": t.size_max + 0.01})
                   if t.id == "title" else t for t in spec.text]
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes],
                                         "text": new_text})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    background = next(s for s in concept.spec.art if s.id == "background")
    focal = next(s for s in concept.spec.art if s.id == "focal")
    assert background.asset and focal.asset            # repainted, not left blank
    # 2 initial paints (background + focal, both generatable) + 2 repaints
    # (the 2 REAL flagged slots -- nonexistent_slot is silently dropped).
    assert len(generate_calls) == 4

    repaint_rows = [r for r in result.ledger if r["kind"] == "image"
                    and "repainted on judge's flag" in r["detail"]]
    assert len(repaint_rows) == 2
    assert all(r["usd"] == 0.0 for r in repaint_rows)
    assert any("slot background repainted on judge's flag" in r["detail"]
              for r in repaint_rows)
    assert any("slot focal repainted on judge's flag" in r["detail"]
              for r in repaint_rows)
    # the real costed image-generation rows ride along too -- nothing lost
    # (2 initial paints + the 2 repaints just asserted above).
    costed_image_rows = [r for r in result.ledger
                         if r["kind"] == "image" and r["usd"] > 0]
    assert len(costed_image_rows) == 4


def test_run_job_critique_repaint_happens_at_most_once_per_concept(
        tmp_path, monkeypatch):
    # Hard bound (§6.3, BRAIN v2.1): at most ONE repaint round per concept
    # per job, even when a LATER round's verdict also names art_defects.
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("full_bleed_art")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    generate_calls = []
    def fake_generate(client, prompt, *, transparent=False, resolution="2K"):
        generate_calls.append(prompt)
        return b"png-bytes"
    monkeypatch.setattr(pipeline, "generate", fake_generate)

    verdicts = [
        CritiqueResult(passes=False, tells=["a surreal blob"],
                       notes="Tighten the tracking.", cost=0.0007,
                       art_defects=["background"]),
        CritiqueResult(passes=False, tells=["still a surreal blob"],
                       notes="Loosen the tracking.", cost=0.0007,
                       art_defects=["background"]),
        CritiqueResult(passes=True, tells=[], notes="", cost=0.0007),
    ]
    monkeypatch.setattr(pipeline, "run_critique", lambda *a, **k: verdicts.pop(0))

    def fake_revise_spec(spec, notes, provider, **kw):
        new_text = [t.model_copy(update={"size_max": t.size_max + 0.01})
                   if t.id == "title" else t for t in spec.text]
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes],
                                         "text": new_text})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert concept.spec.version == 3                     # both revision rounds still applied
    # 1 initial paint (background is full_bleed_art's only generatable slot)
    # + 1 repaint in round 1; round 2's art_defects flag is a no-op -- the
    # concept already spent its one repaint.
    assert len(generate_calls) == 2
    repaint_rows = [r for r in result.ledger if r["kind"] == "image"
                    and "repainted on judge's flag" in r["detail"]]
    assert len(repaint_rows) == 1
    assert "round 1" in next(r for r in result.ledger
                             if r["kind"] == "revision"
                             and "auto-critique revision" in r["detail"])["detail"]


def test_run_job_critique_repaint_bypasses_the_identical_spec_early_stop(
        tmp_path, monkeypatch):
    # A repaint changes real pixels on disk without changing the spec's
    # `asset` STRING at all (_generate_art_slot writes to a fixed,
    # deterministic per-slot path) -- the identical-spec early stop must
    # NOT fire just because the revision itself was otherwise a no-op, when
    # a repaint happened this same round.
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("full_bleed_art")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)
    monkeypatch.setattr(pipeline, "has_real_alpha", lambda png: True)
    monkeypatch.setattr(pipeline, "generate",
                        lambda client, prompt, **k: b"png-bytes")

    verdicts = [
        CritiqueResult(passes=False, tells=["a surreal blob"], notes="",
                       cost=0.0007, art_defects=["background"]),
        CritiqueResult(passes=True, tells=[], notes="", cost=0.0007),
    ]
    monkeypatch.setattr(pipeline, "run_critique", lambda *a, **k: verdicts.pop(0))

    def fake_revise_spec(spec, notes, provider, **kw):
        # The model echoes the design back UNCHANGED -- only the mechanical
        # version bump + notes_log append revise_spec always does.
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes]})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    compose_calls = []
    def counting_compose(spec, job_dir):
        compose_calls.append(spec.version)
        return _fake_compose(spec, job_dir)
    monkeypatch.setattr(pipeline, "compose", counting_compose)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert compose_calls == [1, 2]              # recomposed despite the "identical" spec
    assert verdicts == []                        # both rounds' verdicts consumed
    assert not any("identical spec" in r["detail"] for r in result.ledger
                  if r["kind"] == "revision")
    assert any("repainted on judge's flag" in r["detail"] for r in result.ledger
              if r["kind"] == "image")


# -- composition planner (§15.16, COVER_PLANNER-gated) -------------------------
#
# plan_composition and review_stage are monkeypatched on the pipeline module
# exactly like every other model call in this file -- these tests are about
# pipeline.py's OWN §15.16 job (the gate, plan persistence, staged painting
# order, conditioning bytes, degrade-to-spontaneous, the replan rule), not
# about whether planner.py itself is correct, which is what
# tests/test_cover_planner.py covers.

_PLAN_SUFFIX = ("Amber dusk key light from the west, #101010 and #c9a227, "
                "painterly gouache, timeless coastal era.")


def _cover_plan(**overrides) -> CompositionPlan:
    data = dict(
        light="Key light low from the west, warm amber, dusk.",
        palette_anchors=["background #101010", "accent #c9a227"],
        depth=[{"slot": "background", "plane": "far",
                "negative_space": "leave the upper third as empty sky"},
               {"slot": "focal", "plane": "near",
                "negative_space": "keep the left edge clear"}],
        horizon_y=0.62,
        generation_order=["background", "focal"],
        conditioning=[{"slot": "focal", "review": "background"}],
        unify_recipe="", unify_stops=[],
        consistency_suffix=_PLAN_SUFFIX,
        prompts=[
            {"slot": "background",
             "prompt": f"Planned forest plate, vast empty sky. {_PLAN_SUFFIX}"},
            {"slot": "focal",
             "prompt": f"Planned cloaked figure. {_PLAN_SUFFIX}"}],
        cost=0.12, model="claude-fable-5")
    data.update(overrides)
    return CompositionPlan(**data)


def _stage_review(**overrides) -> StageReview:
    data = dict(prompt=f"Reviewed cloaked figure. {_PLAN_SUFFIX}",
               anchor=[0.4, 0.7], scale=1.2, offset=[0.02, -0.01],
               mask_angle=None, cost=0.03, model="claude-fable-5")
    data.update(overrides)
    return StageReview(**data)


def _rig_planned_paint(monkeypatch):
    """The paint/compose fakes every planner test shares (generate is left
    to each test -- the prompts and bytes are usually what it asserts on)."""
    monkeypatch.setattr(pipeline, "has_real_alpha", lambda png: True)
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)


def test_run_job_gate_off_makes_zero_planner_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("COVER_PLANNER", raising=False)
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("cutout_sandwich")], model="m", cost=0.0))
    planner_calls: list = []
    monkeypatch.setattr(pipeline, "plan_composition",
                        lambda *a, **k: planner_calls.append(a) or _cover_plan())
    monkeypatch.setattr(pipeline, "review_stage",
                        lambda *a, **k: planner_calls.append(a) or _stage_review())
    monkeypatch.setattr(pipeline, "generate",
                        lambda client, prompt, **k: b"png-bytes")
    _rig_planned_paint(monkeypatch)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "ready"
    assert [c.status for c in result.concepts] == ["ready"]
    assert planner_calls == []                      # the spies never fired
    assert not pipeline.plan_path(tmp_path, job.job_id, 0).exists()
    assert not any(row["kind"] == "plan" for row in result.ledger)


def test_run_job_planned_stages_are_sequential_and_review_sees_real_bytes(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COVER_PLANNER", "1")
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("cutout_sandwich")], model="m", cost=0.0))

    events: list = []
    plan = _cover_plan()
    plan_args: dict = {}

    def fake_plan(brief, spec, archetype, sample, client, **kw):
        plan_args.update(brief=brief, spec=spec, archetype=archetype,
                        sample=sample, client=client)
        events.append(("plan", spec.concept_name))
        return plan
    monkeypatch.setattr(pipeline, "plan_composition", fake_plan)

    review_args: dict = {}

    def fake_review(plan_arg, slot_id, prior_renders, draft_prompt, client, **kw):
        review_args.update(plan=plan_arg, slot=slot_id, renders=prior_renders,
                          draft=draft_prompt, client=client)
        events.append(("review", slot_id))
        return _stage_review()
    monkeypatch.setattr(pipeline, "review_stage", fake_review)

    def fake_generate(client, prompt, *, transparent=False, resolution="2K"):
        slot = "background" if "forest" in prompt else "focal"
        events.append(("generate", slot))
        return f"PNG-{slot}".encode()
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    _rig_planned_paint(monkeypatch)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert result.status == "ready" and concept.status == "ready"

    # Staged and SEQUENTIAL: the plate generates first, the review runs
    # against it, and only then does the conditioned focal generate.
    assert events == [("plan", "Concept (cutout_sandwich)"),
                      ("generate", "background"), ("review", "focal"),
                      ("generate", "focal")]

    # The planner rode the same anthropic client critique does, and was
    # handed the real archetype object.
    assert plan_args["client"] is CRITIQUE_CLIENT
    assert plan_args["archetype"] is ARCHETYPES["cutout_sandwich"]

    # The conditioning review received the prior stage's ACTUAL bytes --
    # non-empty, and exactly what the fake engine painted for background.
    assert review_args["slot"] == "focal"
    assert review_args["renders"] == [b"PNG-background"]
    assert review_args["draft"] == plan.prompt_for("focal")
    assert review_args["client"] is CRITIQUE_CLIENT

    # The review's placement fields landed on the slot before it generated.
    focal = next(s for s in concept.spec.art if s.id == "focal")
    assert focal.anchor == [0.4, 0.7]
    assert focal.scale == pytest.approx(1.2)
    assert focal.offset == [0.02, -0.01]
    assert focal.prompt.startswith("Reviewed cloaked figure.")

    # Ledger: a priced plan row and a priced review row.
    plan_rows = [r for r in result.ledger if r["kind"] == "plan"]
    assert any("composition planned via claude-fable-5" in r["detail"]
              and r["usd"] == pytest.approx(0.12) for r in plan_rows)
    assert any("stage review finalized focal against background" in r["detail"]
              and r["usd"] == pytest.approx(0.03) for r in plan_rows)


def test_run_job_planned_prompts_carry_the_consistency_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv("COVER_PLANNER", "1")
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("cutout_sandwich")], model="m", cost=0.0))
    monkeypatch.setattr(pipeline, "plan_composition",
                        lambda *a, **k: _cover_plan(conditioning=[]))

    prompts: list[str] = []

    def fake_generate(client, prompt, *, transparent=False, resolution="2K"):
        prompts.append(prompt)
        return b"png-bytes"
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    _rig_planned_paint(monkeypatch)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    assert pipeline.load_job(tmp_path, job.job_id).status == "ready"
    # Both staged prompts (background stage, then focal stage, in order)
    # carry the plan's consistency suffix, with imaging's negative suffix
    # still layered on top by _assemble_prompt.
    assert len(prompts) == 2
    assert "forest" in prompts[0] and "cloaked" in prompts[1]
    for prompt in prompts:
        assert _PLAN_SUFFIX in prompt
        assert pipeline.NEGATIVE_SUFFIX in prompt
        assert prompt.index(_PLAN_SUFFIX) < prompt.index(pipeline.NEGATIVE_SUFFIX)


def test_run_job_planner_failure_degrades_to_spontaneous(tmp_path, monkeypatch):
    monkeypatch.setenv("COVER_PLANNER", "1")
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("cutout_sandwich")], model="m", cost=0.0))

    def boom(*a, **k):
        raise PlannerError("The planner model declined to plan this composition.")
    monkeypatch.setattr(pipeline, "plan_composition", boom)
    review_calls: list = []
    monkeypatch.setattr(pipeline, "review_stage",
                        lambda *a, **k: review_calls.append(a) or _stage_review())

    prompts: list[str] = []

    def fake_generate(client, prompt, *, transparent=False, resolution="2K"):
        prompts.append(prompt)
        return b"png-bytes"
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    _rig_planned_paint(monkeypatch)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    # The job is NOT lost: every slot painted spontaneously, concept ready.
    assert result.status == "ready"
    assert [c.status for c in result.concepts] == ["ready"]
    assert len(prompts) == 2
    assert any("A misty pine forest, gouache." in p for p in prompts)
    assert review_calls == []                       # no plan, no staged reviews
    assert not pipeline.plan_path(tmp_path, job.job_id, 0).exists()
    # The failure left a $0 ledger note naming the degrade.
    assert any(r["kind"] == "plan" and "planning failed" in r["detail"]
              and "painted spontaneously" in r["detail"] and r["usd"] == 0.0
              for r in result.ledger)


def test_run_job_stage_review_failure_degrades_that_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("COVER_PLANNER", "1")
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("cutout_sandwich")], model="m", cost=0.0))
    plan = _cover_plan()
    monkeypatch.setattr(pipeline, "plan_composition", lambda *a, **k: plan)

    def review_boom(*a, **k):
        raise PlannerError("The stage-review call failed: connection dropped.")
    monkeypatch.setattr(pipeline, "review_stage", review_boom)

    prompts: list[str] = []

    def fake_generate(client, prompt, *, transparent=False, resolution="2K"):
        prompts.append(prompt)
        return b"png-bytes"
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    _rig_planned_paint(monkeypatch)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "ready"
    assert [c.status for c in result.concepts] == ["ready"]
    # Both slots still generated -- the focal with the PLAN's own prompt,
    # unreviewed (the spontaneous path for that slot).
    assert len(prompts) == 2
    assert any("Planned cloaked figure." in p for p in prompts)
    assert any(r["kind"] == "plan" and "stage review for focal failed" in r["detail"]
              and r["usd"] == 0.0 for r in result.ledger)


def test_run_job_persists_a_replayable_plan_json(tmp_path, monkeypatch):
    monkeypatch.setenv("COVER_PLANNER", "1")
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("cutout_sandwich")], model="m", cost=0.0))
    monkeypatch.setattr(pipeline, "plan_composition", lambda *a, **k: _cover_plan())
    monkeypatch.setattr(pipeline, "review_stage", lambda *a, **k: _stage_review())
    monkeypatch.setattr(pipeline, "generate",
                        lambda client, prompt, **k: b"png-bytes")
    _rig_planned_paint(monkeypatch)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    path = pipeline.plan_path(tmp_path, job.job_id, 0)
    assert path.is_file()
    reloaded = CompositionPlan.model_validate_json(path.read_text(encoding="utf-8"))
    assert reloaded == _cover_plan()                # replayable: reload -> same plan


def test_run_job_plan_light_and_unify_reach_the_judge(tmp_path, monkeypatch):
    monkeypatch.setenv("COVER_PLANNER", "1")
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("cutout_sandwich")], model="m", cost=0.0))
    monkeypatch.setattr(pipeline, "plan_composition",
                        lambda *a, **k: _cover_plan(unify_recipe="quiet_literary"))
    monkeypatch.setattr(pipeline, "review_stage", lambda *a, **k: _stage_review())
    monkeypatch.setattr(pipeline, "generate",
                        lambda client, prompt, **k: b"png-bytes")
    _rig_planned_paint(monkeypatch)

    captured: dict = {}

    def fake_critique(png, thumb, spec, brief, client, *, composer_warnings=(),
                      **kw):
        captured["warnings"] = list(composer_warnings)
        return CritiqueResult(passes=True, tells=[], notes="", cost=0.001)
    monkeypatch.setattr(pipeline, "run_critique", fake_critique)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    assert pipeline.load_job(tmp_path, job.job_id).status == "ready"
    warnings = captured["warnings"]
    assert any("lighting contract" in w and "warm amber" in w for w in warnings)
    assert any("unify bind" in w and "quiet_literary" in w for w in warnings)


@pytest.mark.parametrize("archetype, applied", [
    ("cutout_sandwich", True),      # no fx_ layers yet -> the bind applies
    ("full_bleed_art", False),      # archetype default recipe already expanded fx_
])
def test_run_job_plan_unify_applies_only_when_no_fx_layers(
        tmp_path, monkeypatch, archetype, applied):
    monkeypatch.setenv("COVER_PLANNER", "1")
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction(archetype)], model="m", cost=0.0))
    monkeypatch.setattr(pipeline, "plan_composition", lambda *a, **k: _cover_plan(
        generation_order=[], conditioning=[], prompts=[],
        unify_recipe="quiet_literary", unify_stops=["background", "primary"]))
    monkeypatch.setattr(pipeline, "generate",
                        lambda client, prompt, **k: b"png-bytes")
    _rig_planned_paint(monkeypatch)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "ready"
    spec = result.concepts[0].spec
    adjust_ids = {a.id for a in spec.adjust}
    if applied:
        # quiet_literary's finish plus the plan's own gradient_map grade.
        assert {"fx_hush", "fx_warm", "fx_vign", "fx_plan_grade"} <= adjust_ids
        assert any(a.id == "fx_grain" for a in spec.art)
        grade = next(a for a in spec.adjust if a.id == "fx_plan_grade")
        assert grade.op == "gradient_map"
        assert grade.stops == ["background", "primary"]
        # Every added layer is really in the z-order.
        layer_refs = {(r.kind, r.ref) for r in spec.layers}
        assert ("adjust", "fx_plan_grade") in layer_refs
    else:
        # full_bleed_art already wears cinematic_duotone's fx_ stack -- the
        # plan's bind is logged and skipped, nothing doubled.
        assert "fx_plan_grade" not in adjust_ids
        assert "fx_hush" not in adjust_ids
        assert adjust_ids == {"fx_map", "fx_contrast", "fx_bloom"}


def test_run_revision_replan_trigger_rules(tmp_path, monkeypatch):
    """§15.16: a revision re-buys planning ONLY when allow_new_art is set
    AND the notes explicitly say "replan" (case-insensitive) -- and even
    then only with the gate on and a client to plan with."""
    calls: list = []
    monkeypatch.setattr(pipeline, "plan_composition", lambda *a, **k: calls.append(a)
                        or _cover_plan(generation_order=[], conditioning=[],
                                       prompts=[]))

    def fake_revise_spec(spec, notes, provider, **kw):
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes]})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)
    monkeypatch.setattr(pipeline, "generate",
                        lambda client, prompt, **k: b"png-bytes")
    _rig_planned_paint(monkeypatch)

    def replan_count(notes, allow_new_art, *, client=CRITIQUE_CLIENT,
                     gate="1"):
        if gate is None:
            monkeypatch.delenv("COVER_PLANNER", raising=False)
        else:
            monkeypatch.setenv("COVER_PLANNER", gate)
        job = _ready_job_with_concept(tmp_path)
        before = len(calls)
        asyncio.run(pipeline.run_revision(tmp_path, job.job_id, 0, notes,
                                          allow_new_art, PROVIDERS,
                                          IMAGE_CLIENT, client))
        concept = pipeline.load_job(tmp_path, job.job_id).concepts[0]
        assert concept.status == "ready"            # never blocks the revision
        return len(calls) - before

    assert replan_count("moodier, and REPLAN the composition", True) == 1
    assert replan_count("moodier, and replan the composition", False) == 0
    assert replan_count("moodier please", True) == 0
    assert replan_count("replan the composition", True, client=None) == 0
    assert replan_count("replan the composition", True, gate=None) == 0
