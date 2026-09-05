"""docproof/cover/pipeline.py: the job store and the orchestration around it.

assign_concepts (the director), build_concept (the atelier), revise_spec,
imaging.generate/has_real_alpha and compose/save_renders are all
monkeypatched directly on the pipeline module (the "pipeline seam") for every
async test here -- this suite is about pipeline.py's OWN job (state
transitions, concurrency, ledger, on-disk persistence, the revision
asset-diffing contract, assignment persistence), not about whether
director.py/atelier.py/imaging.py/compose.py are themselves correct, which is
what their own dedicated test files cover. No network, no real image bytes,
no real render pixels, and no spawned agent anywhere in this file.

No pytest-asyncio in this repo (see tests/ for the convention): every async
pipeline entry point is driven with a plain asyncio.run() inside an ordinary
sync test function.

PROVIDERS bundles two DISTINCT sentinel Providers (one per model role --
director and revision), so a test can assert not just THAT a provider was
passed somewhere, but WHICH role's provider reached which call
(assign_concepts gets .direction; revise_spec gets .revision).
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from docproof.cover import atelier, pipeline
from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.direction import (DirectionError, DirectionResult,
                                      RevisionError, RevisionResult)
from docproof.cover.model import (Brief, ConceptState, CoverSpec, Direction,
                                  Palette, RenderReport, build_spec)
from docproof.cover.director import ConceptAssignment, DirectorError, DirectorResult
from docproof.cover.planner import CompositionPlan, PlannerError, StageReview
from docproof.ingest import IngestError

# Sentinels: never really called, since assign_concepts/build_concept/
# revise_spec/generate are monkeypatched in every test that reaches them --
# only their PLUMBING (did pipeline.py pass the right thing to the right
# place) is under test. Two DISTINCT provider sentinels, one per role, so a
# test can assert WHICH role's provider reached a given call.
DIRECTION_PROVIDER = object()
REVISION_PROVIDER = object()
PROVIDERS = pipeline.Providers(direction=DIRECTION_PROVIDER,
                               revision=REVISION_PROVIDER)
IMAGE_CLIENT = object()
CRITIQUE_CLIENT = object()     # still accepted by run_job/run_revision
FAKE_IMAGE = object()          # stands in for a PIL.Image.Image



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
    "probe_typographic": {},
    "probe_scene": {"background": "A lonely lighthouse at dusk, oil painting."},
    "probe_sandwich": {"background": "A misty pine forest, gouache.",
                        "focal": "A cloaked figure, cutout subject only."},
}


def _direction(archetype: str = "probe_scene", **overrides) -> Direction:
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


def _spec_for_diffing(archetype: str = "probe_scene") -> CoverSpec:
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


def _ready_job_with_concept(tmp_path, *, archetype: str = "probe_scene",
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


# -- run_job: the director reads, then the agents build (§8) -------------------

def _assignment(archetype: str = "probe_scene", **overrides
                ) -> ConceptAssignment:
    data = dict(direction=_direction(archetype),
                execution_notes="Do not let the plate fight the type.",
                done_when="It reads at thumbnail size.")
    data.update(overrides)
    return ConceptAssignment(**data)


def _director_result(*assignments, **overrides) -> DirectorResult:
    data = dict(assignments=list(assignments) or [_assignment()],
                reading="A book about absence.", model="claude-fable-5",
                cost=0.05, words_read=1040, sliced=False)
    data.update(overrides)
    return DirectorResult(**data)


def _fake_build(**outcome_overrides):
    """A stand-in atelier: composes nothing, reports what it was given."""
    seen = []

    async def build(*, job_dir, index, brief, assignment, spec, image_client,
                    assemble_prompt, save_renders, sem=None, budget=None,
                    model=None):
        seen.append({"index": index, "assignment": assignment, "spec": spec,
                     "budget": budget, "image_client": image_client,
                     "job_dir": job_dir})
        data = dict(spec=spec, report=_report(),
                    renders=[f"renders/v1_c{index}.png"], ledger=[
                        {"kind": "image", "concept": index,
                         "detail": f"concept {index} background (1K, atelier)",
                         "usd": 0.03}],
                    summary=f"concept {index} done", finished=True, error=None)
        data.update(outcome_overrides)
        return pipeline.ConceptOutcome(**data)

    return build, seen


def test_run_job_reads_the_book_assigns_concepts_and_reaches_ready(
        tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=2))
    seen = {}

    def fake_assign(brief, provider, *, n, manuscript="", **kw):
        seen.update(brief=brief, provider=provider, n=n, manuscript=manuscript)
        return _director_result(_assignment("probe_typographic"),
                                _assignment("probe_scene"))
    monkeypatch.setattr(pipeline, "assign_concepts", fake_assign)
    build, built = _fake_build()
    monkeypatch.setattr(pipeline, "build_concept", build)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 CRITIQUE_CLIENT, manuscript="THE WHOLE BOOK"))

    assert seen["provider"] is DIRECTION_PROVIDER
    assert seen["n"] == 2
    assert seen["manuscript"] == "THE WHOLE BOOK"

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "ready"
    assert [c.status for c in result.concepts] == ["ready", "ready"]
    assert [b["index"] for b in built] == [0, 1]
    # one agent per concept, each handed ITS OWN spec and assignment
    assert built[0]["spec"].archetype == "probe_typographic"
    assert built[1]["spec"].archetype == "probe_scene"
    assert built[0]["image_client"] is IMAGE_CLIENT

    kinds = [row["kind"] for row in result.ledger]
    assert kinds.count("director") == 1
    assert kinds.count("image") == 2
    assert result.concepts[0].renders and result.concepts[1].report is not None


def test_run_job_persists_the_assignments_and_nothing_of_the_manuscript(
        tmp_path, monkeypatch):
    """The director's read is kept; the book is not (§8.1's storage posture).

    The assignments are what every agent is briefed from and what a replay
    would need, so they land on disk. The manuscript reached run_job as an
    argument and must leave no trace beyond the sample create_job already
    wrote."""
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "assign_concepts",
                        lambda *a, **k: _director_result())
    build, _ = _fake_build()
    monkeypatch.setattr(pipeline, "build_concept", build)

    secret = "THE MANUSCRIPT SAYS SOMETHING PRIVATE"
    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT,
                                 manuscript=secret))

    written = json.loads(
        pipeline.assignments_path(tmp_path, job.job_id).read_text())
    assert written["reading"] == "A book about absence."
    assert len(written["concepts"]) == 1
    assert written["concepts"][0]["execution_notes"]

    d = pipeline.job_dir(tmp_path, job.job_id)
    for path in d.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(errors="ignore"), path


def test_run_job_falls_back_to_the_stored_sample_when_no_manuscript_is_passed(
        tmp_path, monkeypatch):
    ms = tmp_path / "book.txt"
    ms.write_text("A ship sailed into the fog. " * 400, encoding="utf-8")
    job = pipeline.create_job(tmp_path, _brief(concepts=1), manuscript_path=ms,
                              manuscript_name="book.txt")
    seen = {}

    def fake_assign(brief, provider, *, n, manuscript="", **kw):
        seen["manuscript"] = manuscript
        return _director_result()
    monkeypatch.setattr(pipeline, "assign_concepts", fake_assign)
    build, _ = _fake_build()
    monkeypatch.setattr(pipeline, "build_concept", build)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT))
    assert "A ship sailed into the fog." in seen["manuscript"]


def test_run_job_with_no_manuscript_at_all_passes_nothing(tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    seen = {}

    def fake_assign(brief, provider, *, n, manuscript="", **kw):
        seen["manuscript"] = manuscript
        return _director_result()
    monkeypatch.setattr(pipeline, "assign_concepts", fake_assign)
    build, _ = _fake_build()
    monkeypatch.setattr(pipeline, "build_concept", build)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT))
    assert seen["manuscript"] == ""
    row = next(r for r in pipeline.load_job(tmp_path, job.job_id).ledger
               if r["kind"] == "director")
    assert "no manuscript" in row["detail"]


def test_run_job_director_error_ends_the_job_in_error(tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief())

    def boom(*a, **k):
        raise DirectorError("The director call failed: no budget.")
    monkeypatch.setattr(pipeline, "assign_concepts", boom)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "error"
    assert "no budget" in result.error
    assert result.concepts == []


def test_run_job_builds_every_concept_concurrently(tmp_path, monkeypatch):
    """N agents at once (owner, 2026-08-31, reversing the serial rule).

    Serialisation existed to stop concept N's staged reviews waiting behind
    concept N-1's generations through one shared judge loop. An atelier
    session is its own process holding its own budget, and the per-job image
    semaphore still bounds what is actually in flight -- so the fan-out is
    the point, and this asserts it really is one."""
    job = pipeline.create_job(tmp_path, _brief(concepts=3))
    monkeypatch.setattr(pipeline, "assign_concepts", lambda *a, **k:
                        _director_result(*[_assignment() for _ in range(3)]))

    in_flight = 0
    high_water = 0

    async def watched(root, job_id, index, *args, **kwargs):
        nonlocal in_flight, high_water
        in_flight += 1
        high_water = max(high_water, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1

    monkeypatch.setattr(pipeline, "_build_concept", watched)
    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT))

    assert high_water == 3, "concepts were not built concurrently"


def test_run_job_per_concept_isolation_and_incremental_persistence(
        tmp_path, monkeypatch):
    """One agent's failure must not kill its sibling (§8), and the sibling's
    success must survive on disk -- per-concept writes merge into job.json
    rather than one clobbering the other."""
    job = pipeline.create_job(tmp_path, _brief(concepts=2))
    monkeypatch.setattr(pipeline, "assign_concepts", lambda *a, **k:
                        _director_result(_assignment(), _assignment()))

    async def build(*, index, spec, **kw):
        if index == 1:
            raise RuntimeError("the model refused")
        return pipeline.ConceptOutcome(
            spec=spec, report=_report(), renders=["renders/v1_c0.png"],
            ledger=[{"kind": "image", "concept": 0, "detail": "c0", "usd": 0.03}],
            summary="done", finished=True)
    monkeypatch.setattr(pipeline, "build_concept", build)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT))

    result = pipeline.load_job(tmp_path, job.job_id)
    assert result.status == "ready"            # the job itself IS terminal
    assert result.concepts[0].status == "ready"
    assert result.concepts[0].renders
    assert result.concepts[1].status == "error"
    assert "refused" in result.concepts[1].error
    assert any(row["kind"] == "image" for row in result.ledger)


def test_a_cover_whose_agent_never_called_finish_is_still_shipped(
        tmp_path, monkeypatch):
    """An agent that runs out of turns leaves a real cover behind. Throwing
    it away would discard art the person paid for; the ledger says what
    happened instead."""
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "assign_concepts",
                        lambda *a, **k: _director_result())
    build, _ = _fake_build(finished=False,
                           error="ran out of turns before calling finish")
    monkeypatch.setattr(pipeline, "build_concept", build)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT))

    concept = pipeline.load_job(tmp_path, job.job_id).concepts[0]
    assert concept.status == "ready"
    assert concept.error is None
    assert any("ran out of turns" in r["detail"]
               for r in pipeline.load_job(tmp_path, job.job_id).ledger)


def test_an_agent_that_composed_nothing_lands_its_concept_in_error(
        tmp_path, monkeypatch):
    job = pipeline.create_job(tmp_path, _brief(concepts=1))
    monkeypatch.setattr(pipeline, "assign_concepts",
                        lambda *a, **k: _director_result())
    build, _ = _fake_build(renders=[], finished=False,
                           error="never composed a cover")
    monkeypatch.setattr(pipeline, "build_concept", build)

    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT))

    concept = pipeline.load_job(tmp_path, job.job_id).concepts[0]
    assert concept.status == "error"
    assert "never composed" in concept.error


# -- the per-job image tier (draft vs full) ------------------------------------
#
# The tier no longer reaches imaging.generate from this module -- the agent's
# `paint` tool rolls and prices it (docproof.cover.atelier). What pipeline.py
# still owns is the BUDGET it hands each agent, and the failure mode these
# tests exist for is unchanged in shape: a draft job that buys fewer rolls
# than a full one, or a full job priced as a draft.

def _budget_for(tmp_path, monkeypatch, *, image_quality: str):
    job = pipeline.create_job(tmp_path, _brief(concepts=1),
                              image_quality=image_quality)
    monkeypatch.setattr(pipeline, "assign_concepts",
                        lambda *a, **k: _director_result())
    build, seen = _fake_build()
    monkeypatch.setattr(pipeline, "build_concept", build)
    asyncio.run(pipeline.run_job(tmp_path, job.job_id, PROVIDERS, IMAGE_CLIENT))
    return seen[0]["budget"], pipeline.load_job(tmp_path, job.job_id)


def _image_rows(job) -> list[dict]:
    return [r for r in job.ledger if r["kind"] == "image" and r["usd"] > 0]


def test_a_draft_job_buys_the_same_rolls_at_the_draft_price(tmp_path,
                                                            monkeypatch):
    budget, result = _budget_for(tmp_path, monkeypatch, image_quality="draft")

    assert result.status == "ready"
    assert pipeline.DRAFT_RESOLUTION == "1K"
    assert budget.max_generations == atelier.MAX_GENERATIONS
    assert budget.max_usd == pytest.approx(
        atelier.MAX_GENERATIONS * pipeline.IMAGE_COST["1K"])


def test_a_full_job_is_identical_to_the_default_path(tmp_path, monkeypatch):
    # "full" is a spelling of today's behaviour, not a new one: same tier,
    # same ceiling as a job that says nothing at all.
    full, _ = _budget_for(tmp_path / "a", monkeypatch, image_quality="full")
    unset, _ = _budget_for(tmp_path / "b", monkeypatch, image_quality="")

    assert full.max_usd == unset.max_usd == pytest.approx(
        atelier.MAX_GENERATIONS * pipeline.IMAGE_COST["2K"])
    assert pipeline.IMAGE_RESOLUTION == "2K"


def test_the_agents_image_rows_reach_the_job_ledger(tmp_path, monkeypatch):
    """Whatever the agent spent is billed on the JOB, not lost in its
    session: a ledger nobody can trust is worse than either price alone."""
    _, result = _budget_for(tmp_path, monkeypatch, image_quality="draft")
    rows = _image_rows(result)
    assert len(rows) == 1
    assert rows[0]["usd"] == pytest.approx(pipeline.IMAGE_COST["1K"])
    assert "(1K, atelier)" in rows[0]["detail"]


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
    job = _ready_job_with_concept(tmp_path, archetype="probe_typographic")
    monkeypatch.setattr(pipeline, "compose", _fake_compose)
    monkeypatch.setattr(pipeline, "save_renders", _fake_save_renders)

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
    assert concept.spec.archetype == "probe_typographic"
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
    new = old.model_copy(update={"archetype": "probe_typographic"})
    assert ("archetype probe_scene→probe_typographic"
            in pipeline.diff_spec_fields(old, new))


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

# -- critique pass: the 100px thumbnail (BRAIN v2.1) ---------------------------

# -- critique pass: the art-repaint escape hatch (BRAIN v2.1) ------------------

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
