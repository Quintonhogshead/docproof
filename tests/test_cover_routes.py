"""app/routes/cover.py + the cover wiring in app/quest_site.py: the key gate,
upload validation, job creation and polling, revision, file serving and its
traversal defense, and the rate limits quest_site.py wires around all of it.

Every model call and render step is monkeypatched at the pipeline seam
(docproof.cover.pipeline's own module attributes -- the same module object
app/routes/cover.py imports), exactly the way tests/test_cover_pipeline.py
does, so these are tests of HTTP behaviour, not of direction.py/imaging.py/
compose.py. _providers/_image_client/_critique_client are monkeypatched directly on
app.routes.cover so a route test never needs a real config file, a real API
key, or the network -- mirrors tests/test_quest.py's own
`monkeypatch.setattr("app.routes.quest.build_provider", ...)` pattern.

Job creation and revision are fire-and-forget (asyncio.create_task, 202 back
immediately) -- _poll_until_terminal below is what a real client's poll loop
would do, and is how these tests wait for a background job to actually
finish before asserting on it.
"""
from __future__ import annotations

import json
import time
import types

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import quest_site
from app.routes import cover as cover_routes
from docproof.cover import pipeline as cover_pipeline
from docproof.cover import subscription
from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.direction import DirectionResult
from docproof.cover.imaging import IMAGE_COST
from docproof.cover.model import Brief, ConceptState, Direction, Palette, \
    RenderReport, build_spec
from docproof.cover.subscription import (SubscriptionAnthropicClient,
                                         SubscriptionProvider,
                                         SubscriptionUnavailable)

COVER_KEY = "test-cover-key"


# -- fixtures -------------------------------------------------------------------

def _palette(**overrides) -> Palette:
    data = dict(background="#101010", primary="#f5f1e8", accent="#c9a227",
               text="#f5f1e8", scrim="#000000")
    data.update(overrides)
    return Palette(**data)


_PROMPTS = {"big_type": {},
           "full_bleed_art": {"background": "A lonely lighthouse at dusk, oil painting."}}


def _direction(archetype: str = "big_type", **overrides) -> Direction:
    data = dict(concept_name=f"Concept ({archetype})", rationale="A test concept.",
               archetype=archetype, palette=_palette(),
               title_font="Playfair Display", author_font="Spectral",
               art_prompts=_PROMPTS[archetype], texture=False)
    data.update(overrides)
    return Direction(**data)


def _brief_json(**overrides) -> str:
    data = dict(title="The Lighthouse at Gull Point", author="J. R. Vance",
               genre="literary", concepts=1)
    data.update(overrides)
    return json.dumps(data)


def _app(monkeypatch, tmp_path, *, cover_key: str | None = COVER_KEY,
         lane: str | None = "api"):
    """A test app with the environment pinned.

    `lane` pins COVER_ANTHROPIC_LANE to "api" by default so every test that
    is not ABOUT the lane behaves the same on the owner's logged-in Mac and
    on a CI box with no Claude CLI — "auto" would resolve differently on the
    two. The lane tests pass their own value (or None, to leave the
    environment silent)."""
    if cover_key is None:
        monkeypatch.delenv("COVER_KEY", raising=False)
    else:
        monkeypatch.setenv("COVER_KEY", cover_key)
    monkeypatch.setenv("COVER_DATA_PATH", str(tmp_path))
    if lane is None:
        monkeypatch.delenv("COVER_ANTHROPIC_LANE", raising=False)
    else:
        monkeypatch.setenv("COVER_ANTHROPIC_LANE", lane)
    return quest_site.create_app()


def _bypass_provider_and_image_client(monkeypatch) -> None:
    """Stand in for _providers()/_image_client()/_critique_client() so a
    route test never needs a real config file, a real API key, or the
    network -- whatever they return only ever gets threaded through to the
    pipeline-level fakes below, which ignore it. One Providers instance
    (BRAIN wave, 2026-08-29) rather than a bare object(), since
    cover_pipeline.run_job/run_revision now expect the real dataclass shape
    (.direction/.revision/.reality), not any old sentinel.

    _providers/_critique_client take the resolved Anthropic lane, so the
    stand-ins swallow whatever they are handed — a route test asserting on
    the lane monkeypatches them itself (see the lane tests below)."""
    monkeypatch.setattr(
        "app.routes.cover._providers",
        lambda *a, **k: cover_pipeline.Providers(
            direction=object(), revision=object(), reality=object()))
    monkeypatch.setattr("app.routes.cover._image_client", lambda: object())
    monkeypatch.setattr("app.routes.cover._critique_client",
                        lambda *a, **k: object())


def _fake_direction_call(monkeypatch, directions: list[Direction]) -> None:
    """The director's read, faked. Every route test that lets a job run needs
    one: the real call reads a whole manuscript."""
    from docproof.cover.director import ConceptAssignment, DirectorResult

    monkeypatch.setattr(cover_pipeline, "assign_concepts", lambda *a, **k:
                        DirectorResult(
                            assignments=[
                                ConceptAssignment(direction=d,
                                                  execution_notes="notes",
                                                  done_when="it reads")
                                for d in directions],
                            reading="a read", model="gpt-5.6-luna", cost=0.02,
                            words_read=1000, sliced=False))


def _save_renders(image, job_dir, version, concept) -> list[str]:
    """Writes a real (tiny, fake) file at the path it claims to -- unlike the
    pipeline suite, route tests actually GET these paths back over HTTP, so a
    fake that only returns a string without writing anything would make the
    file endpoint 404 on its own render."""
    out = job_dir / "renders"
    out.mkdir(parents=True, exist_ok=True)
    rel = f"renders/v{version}_c{concept}.png"
    (job_dir / rel).write_bytes(b"fake-png-bytes")
    return [rel]


def _fake_render_chain(monkeypatch, tiers: list[str] | None = None) -> None:
    """The atelier, faked -- every route test that lets a job run through to
    'ready' needs this. A real agent spawns a Claude session; these tests are
    about the HTTP layer and the job store, so the agent is stood in for by
    something that writes one render and bills one image at the tier its
    BUDGET implies. `tiers` collects the tier each concept was funded for,
    which is how the tier tests still prove the request reached the roll."""
    async def fake_build(*, job_dir, index, brief, assignment, spec,
                         image_client, assemble_prompt, save_renders,
                         sem=None, budget=None, model=None):
        per = budget.max_usd / budget.max_generations if budget else 0.05
        tier = "1K" if abs(per - IMAGE_COST["1K"]) < 1e-9 else "2K"
        if tiers is not None:
            tiers.append(tier)
        renders = save_renders(object(), job_dir, spec.version, index)
        return cover_pipeline.ConceptOutcome(
            spec=spec,
            report=RenderReport(contrast={}, scrim_final={}, fitted_sizes={},
                                warnings=[]),
            renders=renders,
            ledger=[{"kind": "image", "concept": index,
                     "detail": f"concept {index} background ({tier}, atelier)",
                     "usd": IMAGE_COST[tier]}],
            summary="done", finished=True)

    monkeypatch.setattr(cover_pipeline, "build_concept", fake_build)
    monkeypatch.setattr(cover_pipeline, "save_renders", _save_renders)


def _poll_until_terminal(client: TestClient, headers: dict, job_id: str,
                         timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    data = None
    while time.monotonic() < deadline:
        resp = client.get(f"/api/cover/jobs/{job_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in ("ready", "error"):
            return data
        time.sleep(0.02)
    raise AssertionError(f"cover job {job_id} never reached a terminal state: {data}")


def _poll_concept_until_terminal(client: TestClient, headers: dict, job_id: str,
                                 concept_index: int, timeout: float = 5.0) -> dict:
    """Like _poll_until_terminal, but for a revision: run_revision never
    touches the job's own top-level `status` (only a fresh run_job does), so
    polling THAT would return the instant it is called -- before the
    background revision has necessarily finished. This polls the one concept
    the revision actually moves through painting/composing/ready-or-error."""
    deadline = time.monotonic() + timeout
    data = None
    while time.monotonic() < deadline:
        resp = client.get(f"/api/cover/jobs/{job_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        if data["concepts"][concept_index]["status"] in ("ready", "error"):
            return data
        time.sleep(0.02)
    raise AssertionError(
        f"cover job {job_id} concept {concept_index} never reached a "
        f"terminal state: {data}")


# -- the key gate (§9) ------------------------------------------------------------

def test_every_endpoint_503s_when_cover_key_is_unset(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, cover_key=None)
    with TestClient(app) as client:
        resp = client.get("/api/cover/jobs")
        assert resp.status_code == 503
        assert "not enabled" in resp.json()["detail"]

        create = client.post("/api/cover/jobs", data={"brief": _brief_json()})
        assert create.status_code == 503


def test_missing_or_wrong_key_is_401(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        wrong = client.get("/api/cover/jobs", headers={"X-Cover-Key": "wrong"})
        assert wrong.status_code == 401

        missing = client.get("/api/cover/jobs")
        assert missing.status_code == 401


def test_the_right_key_gets_past_the_gate(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        resp = client.get("/api/cover/jobs", headers={"X-Cover-Key": COVER_KEY})
        assert resp.status_code == 200
        assert resp.json() == {"jobs": []}


# -- happy path: create -> poll -> download a render + the spec (§9) -------------

def test_create_poll_and_download_happy_path(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    _fake_direction_call(monkeypatch, [_direction("big_type")])
    _fake_render_chain(monkeypatch)
    headers = {"X-Cover-Key": COVER_KEY}

    with TestClient(app) as client:
        created = client.post("/api/cover/jobs",
                              data={"brief": _brief_json(concepts=1)},
                              headers=headers)
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        data = _poll_until_terminal(client, headers, job_id)
        assert data["status"] == "ready"
        assert data["concepts"][0]["status"] == "ready"
        assert data["total_usd"] > 0
        assert data["concepts"][0]["renders"]
        render_name = data["concepts"][0]["renders"][0].removeprefix("renders/")

        rendered = client.get(f"/api/cover/jobs/{job_id}/file/{render_name}",
                              headers=headers)
        assert rendered.status_code == 200
        assert rendered.headers["cache-control"] == "no-cache"

        spec_dl = client.get(f"/api/cover/jobs/{job_id}/file/spec.json?concept=0",
                             headers=headers)
        assert spec_dl.status_code == 200
        assert spec_dl.json()["archetype"] == "big_type"

        listing = client.get("/api/cover/jobs", headers=headers)
        assert listing.status_code == 200
        [entry] = listing.json()["jobs"]
        assert entry["job_id"] == job_id
        assert entry["status"] == "ready"
        assert entry["total_usd"] > 0


def test_get_job_404s_for_an_unknown_id(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        resp = client.get("/api/cover/jobs/20260101-abcdef",
                          headers={"X-Cover-Key": COVER_KEY})
        assert resp.status_code == 404


# -- multipart manuscript upload (§8.1) -------------------------------------------

def test_multipart_create_reads_the_whole_book_and_stores_only_the_sample(
        monkeypatch, tmp_path):
    """The upload reaches the director WHOLE -- not as the opening-plus-middle
    sample create_job writes -- and the book itself still never lands in the
    job store."""
    from docproof.cover.director import ConceptAssignment, DirectorResult

    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    _fake_render_chain(monkeypatch)
    received = {}

    def fake_assign(brief, provider, *, n, manuscript="", **kw):
        received["manuscript"] = manuscript
        return DirectorResult(
            assignments=[ConceptAssignment(direction=_direction("big_type"),
                                           execution_notes="n",
                                           done_when="d")],
            reading="r", model="m", cost=0.0, words_read=100, sliced=False)
    monkeypatch.setattr(cover_pipeline, "assign_concepts", fake_assign)
    headers = {"X-Cover-Key": COVER_KEY}

    body = ("A ship sailed into the fog. " * 400).encode("utf-8")
    with TestClient(app) as client:
        created = client.post(
            "/api/cover/jobs", data={"brief": _brief_json(concepts=1)},
            files={"manuscript": ("book.txt", body, "text/plain")},
            headers=headers)
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        data = _poll_until_terminal(client, headers, job_id)
        assert data["status"] == "ready"
        assert data["manuscript_name"] == "book.txt"
        assert data["word_count"] > 0
        # the WHOLE book, not the sampled form
        assert "OPENING SAMPLE:" not in received["manuscript"]
        assert received["manuscript"].count("A ship sailed into the fog.") == 400
        # ...and only the sample was ever written down
        job_dir = cover_pipeline.job_dir(tmp_path, job_id)
        assert (job_dir / cover_pipeline.MANUSCRIPT_SAMPLE_NAME).is_file()
        assert (job_dir / cover_pipeline.ASSIGNMENTS_NAME).is_file()


def test_wrong_suffix_manuscript_is_400_and_leaves_no_job_dir(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    headers = {"X-Cover-Key": COVER_KEY}
    with TestClient(app) as client:
        resp = client.post(
            "/api/cover/jobs", data={"brief": _brief_json()},
            files={"manuscript": ("book.pdf", b"not really a pdf", "application/pdf")},
            headers=headers)
        assert resp.status_code == 400
        assert ".docx" in resp.json()["detail"]
    assert list(tmp_path.iterdir()) == []


def test_oversized_manuscript_is_413_and_leaves_no_job_dir(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    monkeypatch.setattr("app.routes.cover.MAX_UPLOAD_BYTES", 100)
    headers = {"X-Cover-Key": COVER_KEY}
    with TestClient(app) as client:
        resp = client.post(
            "/api/cover/jobs", data={"brief": _brief_json()},
            files={"manuscript": ("book.txt", b"x" * 200, "text/plain")},
            headers=headers)
        assert resp.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_bad_brief_json_is_400(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    headers = {"X-Cover-Key": COVER_KEY}
    with TestClient(app) as client:
        resp = client.post("/api/cover/jobs", data={"brief": "{not json"},
                           headers=headers)
        assert resp.status_code == 400
    assert list(tmp_path.iterdir()) == []


# -- file endpoint: path traversal defense (§9) -----------------------------------

def test_file_endpoint_rejects_path_traversal(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    _fake_direction_call(monkeypatch, [_direction("big_type")])
    _fake_render_chain(monkeypatch)
    headers = {"X-Cover-Key": COVER_KEY}

    with TestClient(app) as client:
        created = client.post("/api/cover/jobs", data={"brief": _brief_json()},
                              headers=headers)
        job_id = created.json()["job_id"]
        _poll_until_terminal(client, headers, job_id)

        slash = client.get(f"/api/cover/jobs/{job_id}/file/sub%2ffile.png",
                           headers=headers)
        assert slash.status_code in (400, 404)

        dotdot = client.get(f"/api/cover/jobs/{job_id}/file/..%2f..%2fjob.json",
                            headers=headers)
        assert dotdot.status_code in (400, 404)

        absolute = client.get(f"/api/cover/jobs/{job_id}/file/%2fetc%2fpasswd",
                              headers=headers)
        assert absolute.status_code in (400, 404)

        # A literal, un-encoded "file/.." segment is collapsed by the HTTP
        # client itself (RFC 3986 dot-segment removal) before the request is
        # even sent -- %2e%2e is what an attacker deliberately evading that
        # normalization would send, and is what actually reaches this route's
        # own name-must-not-contain-".." check as a literal ".." string.
        encoded_dotdot = client.get(f"/api/cover/jobs/{job_id}/file/%2e%2e",
                                    headers=headers)
        assert encoded_dotdot.status_code in (400, 404)

        # A real render still resolves once the attempts above are refused.
        good = client.get(f"/api/cover/jobs/{job_id}/file/v1_c0.png",
                          headers=headers)
        assert good.status_code == 200


def test_file_endpoint_404s_for_a_name_that_is_not_there(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    _fake_direction_call(monkeypatch, [_direction("big_type")])
    _fake_render_chain(monkeypatch)
    headers = {"X-Cover-Key": COVER_KEY}
    with TestClient(app) as client:
        created = client.post("/api/cover/jobs", data={"brief": _brief_json()},
                              headers=headers)
        job_id = created.json()["job_id"]
        _poll_until_terminal(client, headers, job_id)

        resp = client.get(f"/api/cover/jobs/{job_id}/file/nope.png", headers=headers)
        assert resp.status_code == 404


# -- revise: 202, 404, 409 (§9) ----------------------------------------------------

def test_revise_on_a_non_ready_concept_is_409(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    headers = {"X-Cover-Key": COVER_KEY}

    # Built directly on disk, with its one concept still mid-flight -- no
    # need to run the whole async pipeline to set this state up.
    job = cover_pipeline.create_job(
        tmp_path, Brief(title="T", author="A", genre="literary", concepts=1))
    spec = build_spec(_direction("big_type"), job.brief, ARCHETYPES["big_type"])
    job.concepts = [ConceptState(spec=spec, status="painting")]
    job.status = "working"
    cover_pipeline._write_state(tmp_path, job)

    with TestClient(app) as client:
        resp = client.post(f"/api/cover/jobs/{job.job_id}/revise",
                           json={"concept": 0, "notes": "notes",
                                 "allow_new_art": False},
                           headers=headers)
        assert resp.status_code == 409


def test_revise_on_an_unknown_job_is_404(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    headers = {"X-Cover-Key": COVER_KEY}
    with TestClient(app) as client:
        resp = client.post("/api/cover/jobs/20260101-abcdef/revise",
                           json={"concept": 0, "notes": "x", "allow_new_art": False},
                           headers=headers)
        assert resp.status_code == 404


def test_revise_on_an_out_of_range_concept_is_404(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    headers = {"X-Cover-Key": COVER_KEY}
    job = cover_pipeline.create_job(
        tmp_path, Brief(title="T", author="A", genre="literary", concepts=1))
    spec = build_spec(_direction("big_type"), job.brief, ARCHETYPES["big_type"])
    job.concepts = [ConceptState(spec=spec, status="ready", renders=["renders/v1_c0.png"])]
    job.status = "ready"
    cover_pipeline._write_state(tmp_path, job)

    with TestClient(app) as client:
        resp = client.post(f"/api/cover/jobs/{job.job_id}/revise",
                           json={"concept": 7, "notes": "x", "allow_new_art": False},
                           headers=headers)
        assert resp.status_code == 404


def test_revise_on_a_ready_concept_is_accepted_and_reaches_ready_again(
        monkeypatch, tmp_path):
    from docproof.cover.direction import RevisionResult
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    _fake_render_chain(monkeypatch)

    def fake_revise_spec(spec, notes, provider, **kw):
        revised = spec.model_copy(update={"version": spec.version + 1,
                                         "notes_log": [*spec.notes_log, notes]})
        return RevisionResult(spec=revised, cost=0.01)
    monkeypatch.setattr(cover_pipeline, "revise_spec", fake_revise_spec)
    headers = {"X-Cover-Key": COVER_KEY}

    job = cover_pipeline.create_job(
        tmp_path, Brief(title="T", author="A", genre="literary", concepts=1))
    spec = build_spec(_direction("big_type"), job.brief, ARCHETYPES["big_type"])
    job.concepts = [ConceptState(spec=spec, status="ready", renders=["renders/v1_c0.png"])]
    job.status = "ready"
    cover_pipeline._write_state(tmp_path, job)

    with TestClient(app) as client:
        resp = client.post(f"/api/cover/jobs/{job.job_id}/revise",
                           json={"concept": 0, "notes": "make it bigger",
                                 "allow_new_art": False},
                           headers=headers)
        assert resp.status_code == 202

        data = _poll_concept_until_terminal(client, headers, job.job_id, 0)
        assert data["concepts"][0]["status"] == "ready"
        assert data["concepts"][0]["spec"]["version"] == 2
        assert data["concepts"][0]["spec"]["notes_log"] == ["make it bigger"]


# -- rate limits (§9, wired in app/quest_site.py) ---------------------------------

def test_job_creation_rate_limit_429(monkeypatch, tmp_path):
    monkeypatch.setattr(quest_site, "COVER_JOB_IP_LIMIT", 1)
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    _fake_direction_call(monkeypatch, [_direction("big_type")])
    _fake_render_chain(monkeypatch)
    headers = {"X-Cover-Key": COVER_KEY}

    with TestClient(app) as client:
        first = client.post("/api/cover/jobs", data={"brief": _brief_json()},
                            headers=headers)
        assert first.status_code == 202

        second = client.post("/api/cover/jobs", data={"brief": _brief_json()},
                             headers=headers)
        assert second.status_code == 429


def test_revision_rate_limit_429(monkeypatch, tmp_path):
    monkeypatch.setattr(quest_site, "COVER_REVISE_IP_LIMIT", 1)
    app = _app(monkeypatch, tmp_path)
    headers = {"X-Cover-Key": COVER_KEY}

    job = cover_pipeline.create_job(
        tmp_path, Brief(title="T", author="A", genre="literary", concepts=1))
    spec = build_spec(_direction("big_type"), job.brief, ARCHETYPES["big_type"])
    job.concepts = [ConceptState(spec=spec, status="ready", renders=["renders/v1_c0.png"])]
    job.status = "ready"
    cover_pipeline._write_state(tmp_path, job)

    with TestClient(app) as client:
        # The limiter fires on the path before the 404/409 business logic
        # even runs, so a bogus concept index on the second call still
        # proves the point without needing a real revise to succeed twice.
        first = client.post(f"/api/cover/jobs/{job.job_id}/revise",
                            json={"concept": 99, "notes": "x", "allow_new_art": False},
                            headers=headers)
        assert first.status_code == 404

        second = client.post(f"/api/cover/jobs/{job.job_id}/revise",
                             json={"concept": 99, "notes": "x", "allow_new_art": False},
                             headers=headers)
        assert second.status_code == 429


def test_cover_rate_limits_do_not_affect_quest_endpoints(monkeypatch, tmp_path):
    monkeypatch.setattr(quest_site, "COVER_JOB_IP_LIMIT", 1)
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        resp = client.post("/api/quest/waitlist", json={"email": "not-an-email"})
        assert resp.status_code == 400          # reached the handler, not a 429


# -- existing quest-site behaviour is unaffected (spec §13.6) ---------------------

def test_cover_page_route_is_registered(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/cover" in paths


def test_job_id_with_a_traversal_shape_is_a_plain_404(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        # Dot-dot shapes are normalized away by routing before our code ever
        # sees them (also a 404); the format check is the backstop for any
        # malformed id that DOES reach the endpoint.
        for bad in ("..", "%2e%2e", "20260829-XYZZY!", "a" * 40):
            resp = client.get("/api/cover/jobs/" + bad,
                              headers={"X-Cover-Key": COVER_KEY})
            assert resp.status_code == 404, bad
        for reaches_route in ("20260829-XYZZY!", "a" * 40, "..%5Cjob"):
            resp = client.get("/api/cover/jobs/" + reaches_route,
                              headers={"X-Cover-Key": COVER_KEY})
            assert resp.status_code == 404, reaches_route
            assert "No cover job" in resp.json()["detail"]


# -- which purse the Anthropic roles spend from -------------------------------
#
# The owner's report: Cover Studio on his own machine died on "Your credit
# balance is too low" while a Max subscription sat unused. The lane decides
# which of the two a run spends, per run, and these tests are the matrix --
# built directly against the route helpers where the decision is the whole
# assertion, and over HTTP where the point is that a request, a stored job,
# and the payload agree.

_UNAVAILABLE = ("Cover Studio's Claude subscription lane needs this machine "
                "signed in to Claude and it is not -- run `claude "
                "setup-token`.")


def _lane_seams(monkeypatch, *, available: bool) -> list[str]:
    """The seams a lane decision touches, all faked: whether this machine can
    run a subscription turn, and the API path's config/key/provider
    construction. Returns the list of model ids the API path was asked to
    build, which is how "it fell back" is proved rather than assumed."""
    def preflight() -> None:
        if not available:
            raise SubscriptionUnavailable(_UNAVAILABLE)

    built: list[str] = []

    def build_provider(cfg, *, api_key=None, model=None):
        built.append(model)
        return object()

    monkeypatch.setattr(subscription, "preflight", preflight)
    monkeypatch.setattr(cover_routes, "build_provider", build_provider)
    monkeypatch.setattr(cover_routes, "get_api_key", lambda vendor: "a-key")
    monkeypatch.setattr(cover_routes, "load_config", lambda path: types.SimpleNamespace(
        api=types.SimpleNamespace(effort="low", model="claude-fable-5",
                                  provider="anthropic")))
    monkeypatch.setattr(cover_routes, "_image_client", lambda: object())
    return built


def _lane_recorder(monkeypatch) -> dict:
    """_providers/_critique_client, replaced by recorders that keep the lane
    they were handed -- both are called synchronously inside the request, so
    a response coming back is proof the lane reached them."""
    seen: dict = {}

    def providers(lane: str = "api"):
        seen["providers"] = lane
        return cover_pipeline.Providers(direction=object(), revision=object(),
                                        reality=object())

    def critique_client(lane: str = "api"):
        seen["critique"] = lane
        return object()

    monkeypatch.setattr(cover_routes, "_providers", providers)
    monkeypatch.setattr(cover_routes, "_critique_client", critique_client)
    monkeypatch.setattr(cover_routes, "_image_client", lambda: object())
    return seen


def test_auto_takes_the_subscription_when_the_machine_can_run_one(monkeypatch):
    _lane_seams(monkeypatch, available=True)
    monkeypatch.delenv("COVER_ANTHROPIC_LANE", raising=False)
    lane = cover_routes._resolve_lane(cover_routes._requested_lane())
    providers = cover_routes._providers(lane)
    assert lane == "subscription"
    assert isinstance(providers.direction, SubscriptionProvider)
    assert isinstance(providers.revision, SubscriptionProvider)
    assert isinstance(cover_routes._critique_client(lane),
                      SubscriptionAnthropicClient)


def test_auto_falls_back_to_the_api_key_when_it_cannot(monkeypatch):
    built = _lane_seams(monkeypatch, available=False)
    monkeypatch.delenv("COVER_ANTHROPIC_LANE", raising=False)
    lane = cover_routes._resolve_lane(cover_routes._requested_lane())
    providers = cover_routes._providers(lane)
    assert lane == "api"
    assert not isinstance(providers.direction, SubscriptionProvider)
    assert built == ["claude-fable-5", "claude-sonnet-5"]


def test_the_api_lane_never_reaches_for_the_subscription(monkeypatch):
    # Today's behaviour, untouched -- on a machine that COULD run a
    # subscription turn.
    built = _lane_seams(monkeypatch, available=True)
    monkeypatch.setenv("COVER_ANTHROPIC_LANE", "api")
    lane = cover_routes._resolve_lane(cover_routes._requested_lane())
    assert lane == "api"
    assert not isinstance(cover_routes._providers(lane).direction,
                          SubscriptionProvider)
    assert built == ["claude-fable-5", "claude-sonnet-5"]


def test_a_pinned_subscription_lane_502s_rather_than_billing_the_api(
        monkeypatch):
    # The owner's whole complaint: a silent fall back onto an empty credit
    # balance is the failure, so a pin has no fallback -- just the sentence.
    _lane_seams(monkeypatch, available=False)
    monkeypatch.setenv("COVER_ANTHROPIC_LANE", "subscription")
    with pytest.raises(HTTPException) as excinfo:
        cover_routes._resolve_lane(cover_routes._requested_lane())
    assert excinfo.value.status_code == 502
    assert "setup-token" in excinfo.value.detail


def test_a_role_on_another_vendor_ignores_the_lane(monkeypatch):
    # A model id the catalog serves from OpenAI has no subscription behind
    # it, so the lane must not reach for one.
    built = _lane_seams(monkeypatch, available=True)
    provider = cover_routes._build_role_provider(
        "gpt-5.6-luna", role="direction", lane="subscription")
    assert not isinstance(provider, SubscriptionProvider)
    assert built == ["gpt-5.6-luna"]


def test_a_junk_lane_is_refused_with_the_sentence():
    with pytest.raises(HTTPException) as excinfo:
        cover_routes._checked_lane("free")
    assert excinfo.value.status_code == 422
    assert "subscription" in excinfo.value.detail and "api" in excinfo.value.detail


def test_the_request_body_outranks_the_environment(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, lane="api")
    _lane_seams(monkeypatch, available=True)
    seen = _lane_recorder(monkeypatch)
    headers = {"X-Cover-Key": COVER_KEY}
    with TestClient(app) as client:
        resp = client.post("/api/cover/jobs",
                           data={"brief": _brief_json(),
                                 "anthropic_lane": "subscription"},
                           headers=headers)
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        assert seen == {"providers": "subscription", "critique": "subscription"}
        state = client.get(f"/api/cover/jobs/{job_id}", headers=headers).json()
    assert state["anthropic_lane"] == "subscription"


def test_the_environment_answers_when_the_body_says_nothing(monkeypatch,
                                                            tmp_path):
    app = _app(monkeypatch, tmp_path, lane="api")
    _lane_seams(monkeypatch, available=True)
    seen = _lane_recorder(monkeypatch)
    headers = {"X-Cover-Key": COVER_KEY}
    with TestClient(app) as client:
        resp = client.post("/api/cover/jobs", data={"brief": _brief_json()},
                           headers=headers)
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        state = client.get(f"/api/cover/jobs/{job_id}", headers=headers).json()
    assert seen["providers"] == "api"
    assert state["anthropic_lane"] == "api"


def test_a_junk_lane_on_create_is_a_422_and_no_job(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    _lane_recorder(monkeypatch)
    headers = {"X-Cover-Key": COVER_KEY}
    with TestClient(app) as client:
        resp = client.post("/api/cover/jobs",
                           data={"brief": _brief_json(),
                                 "anthropic_lane": "whatever"},
                           headers=headers)
        assert resp.status_code == 422
        assert "anthropic_lane must be" in resp.json()["detail"]
        assert client.get("/api/cover/jobs", headers=headers).json() == {"jobs": []}


def test_a_revision_reuses_the_lane_its_job_was_started_on(monkeypatch,
                                                           tmp_path):
    # The environment says "api"; the job was created on the subscription.
    # A revision must not quietly switch purses mid-book.
    app = _app(monkeypatch, tmp_path, lane="api")
    _lane_seams(monkeypatch, available=True)
    seen = _lane_recorder(monkeypatch)
    monkeypatch.setattr(cover_pipeline, "run_revision",
                        lambda *a, **k: _noop_revision())
    headers = {"X-Cover-Key": COVER_KEY}

    job = cover_pipeline.create_job(
        tmp_path, Brief(title="T", author="A", genre="literary", concepts=1),
        anthropic_lane="subscription")
    spec = build_spec(_direction("big_type"), job.brief, ARCHETYPES["big_type"])
    job.concepts = [ConceptState(spec=spec, status="ready",
                                 renders=["renders/v1_c0.png"])]
    job.status = "ready"
    cover_pipeline._write_state(tmp_path, job)

    with TestClient(app) as client:
        resp = client.post(f"/api/cover/jobs/{job.job_id}/revise",
                           json={"concept": 0, "notes": "warmer",
                                 "allow_new_art": False},
                           headers=headers)
        assert resp.status_code == 202
    assert seen["providers"] == "subscription"


def test_a_revision_body_can_override_the_stored_lane(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, lane="subscription")
    _lane_seams(monkeypatch, available=True)
    seen = _lane_recorder(monkeypatch)
    monkeypatch.setattr(cover_pipeline, "run_revision",
                        lambda *a, **k: _noop_revision())
    headers = {"X-Cover-Key": COVER_KEY}

    job = cover_pipeline.create_job(
        tmp_path, Brief(title="T", author="A", genre="literary", concepts=1),
        anthropic_lane="subscription")
    spec = build_spec(_direction("big_type"), job.brief, ARCHETYPES["big_type"])
    job.concepts = [ConceptState(spec=spec, status="ready",
                                 renders=["renders/v1_c0.png"])]
    job.status = "ready"
    cover_pipeline._write_state(tmp_path, job)

    with TestClient(app) as client:
        resp = client.post(f"/api/cover/jobs/{job.job_id}/revise",
                           json={"concept": 0, "notes": "warmer",
                                 "allow_new_art": False,
                                 "anthropic_lane": "api"},
                           headers=headers)
        assert resp.status_code == 202
    assert seen["providers"] == "api"


def test_the_job_list_names_each_job_s_lane(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    headers = {"X-Cover-Key": COVER_KEY}
    cover_pipeline.create_job(
        tmp_path, Brief(title="T", author="A", genre="literary", concepts=1),
        anthropic_lane="subscription")
    with TestClient(app) as client:
        listed = client.get("/api/cover/jobs", headers=headers).json()["jobs"]
    assert [j["anthropic_lane"] for j in listed] == ["subscription"]


def test_a_job_from_before_the_lane_existed_still_loads(monkeypatch, tmp_path):
    # job.json files predate this field; they read as "no opinion", not as a
    # validation error.
    app = _app(monkeypatch, tmp_path)
    headers = {"X-Cover-Key": COVER_KEY}
    job = cover_pipeline.create_job(
        tmp_path, Brief(title="T", author="A", genre="literary", concepts=1))
    raw = json.loads((cover_pipeline.job_dir(tmp_path, job.job_id)
                      / cover_pipeline.JOB_MANIFEST).read_text())
    raw.pop("anthropic_lane")
    (cover_pipeline.job_dir(tmp_path, job.job_id)
     / cover_pipeline.JOB_MANIFEST).write_text(json.dumps(raw))
    with TestClient(app) as client:
        state = client.get(f"/api/cover/jobs/{job.job_id}",
                           headers=headers).json()
    assert state["anthropic_lane"] == ""


async def _noop_revision() -> None:
    """A run_revision stand-in: the lane assertion happens before the
    background task matters, and a real revision would need the whole render
    chain."""
    return None


# -- how sharp this job's art is rolled ----------------------------------------
#
# The studio's half of the quality ladder Cover Canvas already had: a "draft"
# job rolls (and bills) every image at 1K so concepts can be shopped cheaply,
# and the keeper is sharpened afterwards in the canvas. Shaped after the lane
# tests above -- body plumbing, payload surfacing, junk refused, and the
# stored value outliving the request that set it -- with one difference the
# tests below pin down deliberately: unlike the lane, this is create-only.

def _tier_over_http(monkeypatch, tmp_path, form: dict) -> tuple[list[str], dict]:
    """POST a brief that actually needs a generated image, let the background
    job run to terminal, and return (the tiers each concept's agent was
    funded for, the job payload). Everything past the HTTP layer is the real
    pipeline, which is the only way to prove the request's tier reached both
    the budget and the price."""
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    _fake_direction_call(monkeypatch, [_direction("full_bleed_art")])
    resolutions: list[str] = []
    _fake_render_chain(monkeypatch, tiers=resolutions)

    headers = {"X-Cover-Key": COVER_KEY}
    with TestClient(app) as client:
        resp = client.post("/api/cover/jobs",
                           data={"brief": _brief_json(), **form},
                           headers=headers)
        assert resp.status_code == 202
        return resolutions, _poll_until_terminal(client, headers,
                                                 resp.json()["job_id"])


def test_the_brief_can_ask_for_draft_art(monkeypatch, tmp_path):
    resolutions, state = _tier_over_http(monkeypatch, tmp_path,
                                         {"image_quality": "draft"})
    assert state["image_quality"] == "draft"
    assert resolutions == ["1K"]                            # rolled cheap...
    rows = [r for r in state["ledger"] if r["kind"] == "image" and r["usd"] > 0]
    assert len(rows) == 1
    assert rows[0]["usd"] == pytest.approx(IMAGE_COST["1K"])  # ...and billed cheap
    assert state["total_usd"] == pytest.approx(IMAGE_COST["1K"] + 0.02)


def test_a_brief_that_says_nothing_gets_the_full_tier(monkeypatch, tmp_path):
    # Today's behaviour, untouched: no field, no opinion, 2K at 2K's price.
    resolutions, state = _tier_over_http(monkeypatch, tmp_path, {})
    assert state["image_quality"] == ""
    assert resolutions == ["2K"]
    rows = [r for r in state["ledger"] if r["kind"] == "image" and r["usd"] > 0]
    assert rows[0]["usd"] == pytest.approx(IMAGE_COST["2K"])


def test_asking_for_full_explicitly_is_the_same_as_saying_nothing(monkeypatch,
                                                                   tmp_path):
    resolutions, state = _tier_over_http(monkeypatch, tmp_path,
                                         {"image_quality": "full"})
    assert state["image_quality"] == "full"
    assert resolutions == ["2K"]
    rows = [r for r in state["ledger"] if r["kind"] == "image" and r["usd"] > 0]
    assert rows[0]["usd"] == pytest.approx(IMAGE_COST["2K"])


def test_a_junk_image_quality_is_refused_with_the_sentence():
    with pytest.raises(HTTPException) as excinfo:
        cover_routes._checked_image_quality("cinematic")
    assert excinfo.value.status_code == 422
    assert "draft" in excinfo.value.detail and "full" in excinfo.value.detail


def test_a_junk_image_quality_on_create_is_a_422_and_no_job(monkeypatch,
                                                             tmp_path):
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    headers = {"X-Cover-Key": COVER_KEY}
    with TestClient(app) as client:
        resp = client.post("/api/cover/jobs",
                           data={"brief": _brief_json(),
                                 "image_quality": "cheap"},
                           headers=headers)
        assert resp.status_code == 422
        assert "image_quality must be" in resp.json()["detail"]
        assert client.get("/api/cover/jobs", headers=headers).json() == {"jobs": []}


def test_a_revision_cannot_change_the_tier(monkeypatch, tmp_path):
    # The tier is the JOB's, fixed at creation: a revision that could switch
    # horses mid-job would leave one ledger quoting two prices for rows that
    # look identical. ReviseBody forbids extras, so asking is a 422 -- not a
    # silent no-op -- and the stored tier is what the revision actually runs
    # on.
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    monkeypatch.setattr(cover_pipeline, "run_revision",
                        lambda *a, **k: _noop_revision())
    headers = {"X-Cover-Key": COVER_KEY}

    job = cover_pipeline.create_job(
        tmp_path, Brief(title="T", author="A", genre="literary", concepts=1),
        image_quality="draft")
    spec = build_spec(_direction("big_type"), job.brief, ARCHETYPES["big_type"])
    job.concepts = [ConceptState(spec=spec, status="ready",
                                 renders=["renders/v1_c0.png"])]
    job.status = "ready"
    cover_pipeline._write_state(tmp_path, job)

    with TestClient(app) as client:
        refused = client.post(f"/api/cover/jobs/{job.job_id}/revise",
                              json={"concept": 0, "notes": "warmer",
                                    "image_quality": "full"},
                              headers=headers)
        assert refused.status_code == 422
        ok = client.post(f"/api/cover/jobs/{job.job_id}/revise",
                         json={"concept": 0, "notes": "warmer"},
                         headers=headers)
        assert ok.status_code == 202
        state = client.get(f"/api/cover/jobs/{job.job_id}",
                           headers=headers).json()
    assert state["image_quality"] == "draft"
    assert cover_pipeline._image_tier(
        cover_pipeline.load_job(tmp_path, job.job_id)) == "1K"


def test_the_job_list_names_each_job_s_image_quality(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    headers = {"X-Cover-Key": COVER_KEY}
    cover_pipeline.create_job(
        tmp_path, Brief(title="T", author="A", genre="literary", concepts=1),
        image_quality="draft")
    with TestClient(app) as client:
        listed = client.get("/api/cover/jobs", headers=headers).json()["jobs"]
    assert [j["image_quality"] for j in listed] == ["draft"]


def test_a_job_from_before_the_tier_existed_still_loads(monkeypatch, tmp_path):
    # The owner has draft jobs in flight; job.json files written before this
    # field existed read as "no opinion", not as a validation error.
    app = _app(monkeypatch, tmp_path)
    headers = {"X-Cover-Key": COVER_KEY}
    job = cover_pipeline.create_job(
        tmp_path, Brief(title="T", author="A", genre="literary", concepts=1))
    manifest = (cover_pipeline.job_dir(tmp_path, job.job_id)
                / cover_pipeline.JOB_MANIFEST)
    raw = json.loads(manifest.read_text())
    raw.pop("image_quality")
    manifest.write_text(json.dumps(raw))
    with TestClient(app) as client:
        state = client.get(f"/api/cover/jobs/{job.job_id}",
                           headers=headers).json()
        listed = client.get("/api/cover/jobs", headers=headers).json()["jobs"]
    assert state["image_quality"] == ""
    assert [j["image_quality"] for j in listed] == [""]
