"""docproof/cover/pipeline.py: the job store and the orchestration around it.

run_directions, revise_spec, imaging.generate/has_real_alpha, compose/
save_renders, and critique.run_critique are all monkeypatched directly on
the pipeline module (the "pipeline seam") for every async test here -- this
suite is about pipeline.py's OWN job (state transitions, concurrency,
ledger, on-disk persistence, the revision asset-diffing contract, the §6.3
critique wiring), not about whether direction.py/imaging.py/compose.py/
critique.py themselves are correct, which is what their own dedicated test
files already cover. No network, no real image bytes, no real render pixels
anywhere in this file.

No pytest-asyncio in this repo (see tests/ for the convention): every async
pipeline entry point is driven with a plain asyncio.run() inside an ordinary
sync test function.

_default_critique_passes (autouse) defaults every test's critique step to a
free, always-passing verdict, so the pre-existing tests below -- written
before §6.3 existed -- keep testing exactly what they tested before: the
paint/compose/ledger/persistence machinery, undisturbed by the critique
pass riding along on every run_job now. The "-- critique pass (§6.3) --"
section further down overrides that default per test to exercise the
critique wiring itself.
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
from docproof.cover.model import (Brief, ConceptState, Direction, Palette,
                                  RenderReport, build_spec)
from docproof.ingest import IngestError

# Sentinels: never really called, since run_directions/revise_spec/generate
# are monkeypatched in every test that reaches them -- only their PLUMBING
# (did pipeline.py pass the right thing to the right place) is under test.
PROVIDER = object()
IMAGE_CLIENT = object()
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
        directions=directions, model="gpt-5.6-luna", cost=0.02))

    calls = []
    def fake_generate(client, prompt, *, transparent=False, resolution="2K"):
        calls.append((client, prompt, transparent, resolution))
        return b"fake-png-bytes"
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    monkeypatch.setattr(pipeline, "has_real_alpha", lambda png: True)
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDER, IMAGE_CLIENT))

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


def test_run_job_passes_the_persisted_manuscript_sample_to_run_directions(
        tmp_path, monkeypatch):
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

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDER, IMAGE_CLIENT))

    assert received["provider"] is PROVIDER
    assert received["n"] == 1
    assert "OPENING SAMPLE:" in received["sample"]
    assert "A ship sailed into the fog." in received["sample"]


def test_run_job_with_no_manuscript_passes_an_empty_sample(tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    received = {}
    def fake_run_directions(brief, provider, *, n, manuscript_sample="", **kw):
        received["sample"] = manuscript_sample
        return DirectionResult(directions=[_direction("big_type")], model="m", cost=0.0)
    monkeypatch.setattr(pipeline, "run_directions", fake_run_directions)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDER, IMAGE_CLIENT))
    assert received["sample"] == ""


def test_run_job_direction_error_ends_the_job_in_error(tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief())
    def boom(*a, **k):
        raise DirectionError("The art-direction call failed: no budget.")
    monkeypatch.setattr(pipeline, "run_directions", boom)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDER, IMAGE_CLIENT))

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

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDER, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "ready"            # the job itself IS terminal
    assert result.concepts[0].status == "ready"
    assert result.concepts[0].renders           # concept 0's success was not clobbered
    assert result.concepts[1].status == "error"
    assert "refused" in result.concepts[1].error
    assert any(row["kind"] == "image" for row in result.ledger)   # concept 0's image billed


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
                                      PROVIDER, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert concept.spec.version == 2
    assert concept.spec.notes_log == ["make the title bigger"]
    assert len(concept.renders) == 2            # the original render plus the new one
    assert any(row["kind"] == "revision" for row in result.ledger)
    assert not any(row["kind"] == "image" for row in result.ledger)


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
                                      True, PROVIDER, IMAGE_CLIENT))

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
                                      False, PROVIDER, IMAGE_CLIENT))

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
                                      PROVIDER, IMAGE_CLIENT))

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
                                      False, PROVIDER, IMAGE_CLIENT))


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


# -- critique pass (§6.3) -------------------------------------------------------
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
    def fake_run_critique(png_bytes, spec, brief, client, **kw):
        critique_calls.append((spec.version, client, brief.title))
        return CritiqueResult(passes=True, tells=[], notes="", cost=0.0007)
    monkeypatch.setattr(pipeline, "run_critique", fake_run_critique)

    def fail_revise(*a, **k):
        raise AssertionError("revise_spec should not run when critique passes")
    monkeypatch.setattr(pipeline, "revise_spec", fail_revise)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDER, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert concept.spec.version == 1                  # no auto-revision -> no version bump
    assert len(critique_calls) == 1                    # exactly one round when it passes
    version, client, title = critique_calls[0]
    assert client is IMAGE_CLIENT                       # "same key" -- reuses the image client
    assert title == job.brief.title

    kinds = [row["kind"] for row in result.ledger]
    assert kinds.count("critique") == 1
    critique_row = next(r for r in result.ledger if r["kind"] == "critique")
    assert critique_row["usd"] == pytest.approx(0.0007)


def test_run_job_critique_fails_triggers_exactly_one_auto_revision_round(
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
    def fake_run_critique(png_bytes, spec, brief, client, **kw):
        critique_calls.append(spec.version)
        return verdicts.pop(0)
    monkeypatch.setattr(pipeline, "run_critique", fake_run_critique)

    def fake_revise_spec(spec, notes, provider, **kw):
        assert notes == "Enlarge the title."
        assert provider is PROVIDER
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes]})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    def fail_generate(*a, **k):
        raise AssertionError(
            "generate should never run for a design-only auto-critique revision")
    monkeypatch.setattr(pipeline, "generate", fail_generate)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDER, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert concept.spec.version == 2
    assert concept.spec.notes_log == ["[auto-critique] Enlarge the title."]
    assert critique_calls == [1, 2]                     # critiqued v1, then the revised v2
    assert verdicts == []                                # both canned verdicts consumed

    kinds = [row["kind"] for row in result.ledger]
    assert kinds.count("critique") == 2
    revision_rows = [r for r in result.ledger if r["kind"] == "revision"]
    assert len(revision_rows) == 1
    assert "auto-critique" in revision_rows[0]["detail"]
    # two renders on file: the original composition and the post-revision one
    assert len(concept.renders) == 2


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

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDER, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"                    # §6.3: never blocks a cover
    assert concept.spec.version == 1
    critique_row = next(r for r in result.ledger if r["kind"] == "critique")
    assert critique_row["usd"] == 0.0
    assert "refused" in critique_row["detail"]


def test_run_job_critique_leftover_tells_from_the_second_verdict_land_in_warnings(
        tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=[_direction("big_type")], model="m", cost=0.01))
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

    verdicts = [
        CritiqueResult(passes=False, tells=["weak hierarchy"],
                       notes="Enlarge the title.", cost=0.0007),
        CritiqueResult(passes=False, tells=["still a bit crowded"],
                       notes="not used -- v1 runs exactly one round", cost=0.0007),
    ]
    monkeypatch.setattr(pipeline, "run_critique", lambda *a, **k: verdicts.pop(0))

    def fake_revise_spec(spec, notes, provider, **kw):
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes]})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(pipeline, "revise_spec", fake_revise_spec)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDER, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"                    # ships regardless of the 2nd verdict
    assert concept.spec.version == 2                     # the one auto-revision round DID run
    assert "still a bit crowded" in concept.report.warnings
    # the FIRST verdict's tell was (presumably) addressed by the revision --
    # only the leftover, still-true one rides along in the report.
    assert "weak hierarchy" not in concept.report.warnings


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

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDER, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    concept = result.concepts[0]
    assert concept.status == "ready"
    assert concept.spec.version == 1                     # the revision never actually applied
    assert "weak hierarchy" in concept.report.warnings
    revision_rows = [r for r in result.ledger if r["kind"] == "revision"]
    assert len(revision_rows) == 1
    assert revision_rows[0]["usd"] == 0.0


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

    def fake_run_critique(png_bytes, spec, brief, client, **kw):
        if spec.concept_name == "Bad":
            raise pipeline.CritiqueError("boom")
        return CritiqueResult(passes=True, tells=[], notes="", cost=0.0007)
    monkeypatch.setattr(pipeline, "run_critique", fake_run_critique)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDER, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "ready"
    assert [c.status for c in result.concepts] == ["ready", "ready"]
    good_row = next(r for r in result.ledger
                    if r["kind"] == "critique" and "passed" in r["detail"])
    bad_row = next(r for r in result.ledger
                   if r["kind"] == "critique" and "failed" in r["detail"])
    assert good_row["usd"] == pytest.approx(0.0007)
    assert bad_row["usd"] == 0.0


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
                                      False, PROVIDER, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.concepts[0].status == "ready"
    assert result.concepts[0].spec.version == 2
    assert not any(row["kind"] == "critique" for row in result.ledger)
