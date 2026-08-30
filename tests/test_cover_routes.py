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

from fastapi.testclient import TestClient

from app import quest_site
from docproof.cover import pipeline as cover_pipeline
from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.direction import DirectionResult
from docproof.cover.model import Brief, ConceptState, Direction, Palette, \
    RenderReport, build_spec

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


def _app(monkeypatch, tmp_path, *, cover_key: str | None = COVER_KEY):
    if cover_key is None:
        monkeypatch.delenv("COVER_KEY", raising=False)
    else:
        monkeypatch.setenv("COVER_KEY", cover_key)
    monkeypatch.setenv("COVER_DATA_PATH", str(tmp_path))
    return quest_site.create_app()


def _bypass_provider_and_image_client(monkeypatch) -> None:
    """Stand in for _providers()/_image_client()/_critique_client() so a
    route test never needs a real config file, a real API key, or the
    network -- whatever they return only ever gets threaded through to the
    pipeline-level fakes below, which ignore it. One Providers instance
    (BRAIN wave, 2026-08-29) rather than a bare object(), since
    cover_pipeline.run_job/run_revision now expect the real dataclass shape
    (.direction/.revision/.reality), not any old sentinel."""
    monkeypatch.setattr(
        "app.routes.cover._providers",
        lambda: cover_pipeline.Providers(
            direction=object(), revision=object(), reality=object()))
    monkeypatch.setattr("app.routes.cover._image_client", lambda: object())
    monkeypatch.setattr("app.routes.cover._critique_client", lambda: object())


def _fake_direction_call(monkeypatch, directions: list[Direction]) -> None:
    monkeypatch.setattr(cover_pipeline, "run_directions", lambda *a, **k: DirectionResult(
        directions=directions, model="gpt-5.6-luna", cost=0.02))


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


def _fake_render_chain(monkeypatch) -> None:
    """generate/has_real_alpha/compose/save_renders -- every route test that
    lets a job actually run through to 'ready' needs these."""
    monkeypatch.setattr(cover_pipeline, "generate",
                        lambda client, prompt, **k: b"fake-png-bytes")
    monkeypatch.setattr(cover_pipeline, "has_real_alpha", lambda png: True)
    monkeypatch.setattr(cover_pipeline, "compose", lambda spec, job_dir: (
        object(), RenderReport(contrast={}, scrim_final={}, fitted_sizes={},
                              warnings=[])))
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

def test_multipart_create_with_a_manuscript_persists_sample_and_reaches_direction(
        monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    _bypass_provider_and_image_client(monkeypatch)
    _fake_render_chain(monkeypatch)
    received = {}
    def fake_run_directions(brief, provider, *, n, manuscript_sample="", **kw):
        received["sample"] = manuscript_sample
        return DirectionResult(directions=[_direction("big_type")], model="m", cost=0.0)
    monkeypatch.setattr(cover_pipeline, "run_directions", fake_run_directions)
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
        assert "OPENING SAMPLE:" in received["sample"]
        assert "A ship sailed into the fog." in received["sample"]


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
