"""The galley job runner's post-processing, end to end on fakes.

Driving `run_one` by hand (the pattern test_promo_jobs.py and test_jobs.py
use), so the whole wave loop -> adjudicate -> deliverable -> letter -> memory
path runs against a `FakeDetector` — never a real provider, never a real
dollar. `_galley_adapters` is monkeypatched the way the module's own docstring
says a test should ("tests patch this to inject FakeDetectors and never touch
a model").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.jobs import Job, JobRunner, JobStore
from app.settings import Paths, Settings

from galley.casefile import CaseFile
from galley.memory.store import MemoryStore

from tests.galley.fakes import FakeDetector, gfinding

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "default.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "tiny_novel.docx"

# The fixture's real, established content (see tests/galley/test_ladder_adapter
# .py and test_single_pass.py): body-0028 holds "...opened teh door..." with
# "teh" at offset 10; body-0001 opens "Kathryn had lived...".
TEH_PARA = "body-0028"
TEH_START = 10


pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="fixture book missing")


@pytest.fixture
def runner(tmp_path):
    paths = Paths(tmp_path).ensure()
    store = JobStore(paths)
    r = JobRunner(store, Settings(output_dir=str(tmp_path / "out")),
                  config_path=CONFIG)
    return store, r


def _job(store, **over) -> Job:
    fields = {"id": "j1", "filename": FIXTURE.name,
              "source_path": str(FIXTURE), "model": "", "mode": "now",
              "kind": "galley", "tier": "T1", "budget_usd": 5.0,
              "state": "queued"}
    fields.update(over)
    return store.save(Job(**fields))


def _fake_adapters():
    """Wave one's scripted ladder findings: one real edit, one that loses
    arbitration to it (same span — exercises adjudicate() producing a real
    Verdict without a provider), one self-declared query, one unanchorable
    finding (a coverage-warning, not a crash)."""
    ladder = FakeDetector(
        name="docproof_ladder",
        scripted=[
            gfinding("g-1", TEH_PARA, "teh", "the",
                     start=TEH_START, end=TEH_START + 3, wave=1),
            gfinding("g-1b", TEH_PARA, "teh", "TEH",
                     start=TEH_START, end=TEH_START + 3, wave=1),  # overlap loser
            gfinding("g-2", "body-0001", "Kathryn", "", wave=1,
                     confidence="query", note="possible continuity flag"),
            gfinding("g-3", TEH_PARA, "zzzznotreal", "zzz", wave=1),  # unanchorable
        ],
        cost_usd=0.0,
    )
    single = FakeDetector(name="single_pass")
    return {"docproof_ladder": ladder, "single_pass": single}


def test_run_galley_produces_every_deliverable(runner, monkeypatch):
    store, r = runner
    monkeypatch.setattr(r, "_galley_adapters",
                        lambda job, cfg: _fake_adapters())
    job = _job(store)

    r.run_one("j1")

    done = store.get("j1")
    assert done.state == "done", done.error
    out = Path(done.results_dir)

    # 1 — adjudication: verdicts written back into the case file.
    cf = CaseFile.load(out / "casefile.json")
    assert cf.verdicts, "the overlap loser must have earned a verdict"
    losers = [v for v in cf.verdicts if v.finding_id == "g-1b"]
    assert losers and losers[0].ruling == "query"

    # 2 — the reviewed manuscript: a real tracked-changes docx, not just JSON.
    reviewed = list(out.glob("*.docx"))
    assert reviewed, "no reviewed document was written"
    findings_json = out / "findings.json"
    assert findings_json.is_file()

    # 3 — letter + style sheet.
    assert (out / "letter.md").is_file()
    assert (out / "style-sheet.md").is_file()
    letter_text = (out / "letter.md").read_text(encoding="utf-8")
    assert "g-1b" in letter_text  # the query is named, not hidden

    # The unanchorable finding is a warning, not a failure.
    assert any("g-3" in w for w in done.warnings)

    # 4 — memory ingest: the overlap-loser verdict became a precedent.
    with MemoryStore.open(store.paths.galley_memory_db) as mem:
        precedents = mem.precedents()
    assert precedents, "the run's verdicts should have been ingested"

    # The job card exposes every deliverable that actually landed.
    api = done.to_api()
    assert api["has_galley_document"] is True
    assert api["has_galley_letter"] is True
    assert api["has_galley_style_sheet"] is True


def test_run_galley_memory_ingest_is_idempotent_across_runs(runner, monkeypatch):
    """Two galley runs over the same book, same verdicts, add no duplicate
    precedent rows — the dedup in galley.memory.ingest carries across jobs
    because both write to the one durable store."""
    store, r = runner
    monkeypatch.setattr(r, "_galley_adapters",
                        lambda job, cfg: _fake_adapters())

    _job(store, id="j1")
    r.run_one("j1")
    with MemoryStore.open(store.paths.galley_memory_db) as mem:
        first = len(mem.precedents())

    _job(store, id="j2", filename=FIXTURE.name)
    r.run_one("j2")
    with MemoryStore.open(store.paths.galley_memory_db) as mem:
        second = len(mem.precedents())

    assert second == first, "the identical verdict must dedupe, not double up"


def test_a_deliverable_build_failure_is_a_warning_not_a_failed_job(
        runner, monkeypatch):
    """The wave loop is the money already spent; a broken deliverable build
    must not throw that away by failing the job."""
    store, r = runner
    monkeypatch.setattr(r, "_galley_adapters",
                        lambda job, cfg: _fake_adapters())

    def boom(*a, **kw):
        raise RuntimeError("simulated deliverable failure")

    # `_run_galley` imports build_manuscript_deliverable locally (inside the
    # method) each call, so patching the source module's attribute is what a
    # fresh `from galley.deliverable import ...` at call time will pick up.
    import galley.deliverable as deliverable_mod
    monkeypatch.setattr(deliverable_mod, "build_manuscript_deliverable", boom)

    job = _job(store)
    r.run_one("j1")

    done = store.get("j1")
    assert done.state == "done"
    assert any("reviewed manuscript" in w.lower() for w in done.warnings)
    # The case file and letter are still there.
    assert (Path(done.results_dir) / "casefile.json").is_file()
    assert (Path(done.results_dir) / "letter.md").is_file()
