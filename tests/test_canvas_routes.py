"""app/routes/canvas.py + docproof/canvas/regen.py: the editor's HTTP surface.

Driven through the real app (app.main.create_app, where app/routes/__init__.py
registers these routes) with a TestClient, the way tests/test_cover_routes.py
drives Cover Studio's — same key gate, same job store, same "monkeypatch at
the seam, never reach a vendor" rule.

Three seams stand in for the outside world:

- `app.routes.cover._image_client` returns a stub OpenAI client whose
  `images.generate` / `images.edit` record what they were asked for and hand
  back a real (tiny) PNG. Stubbing the CLIENT rather than
  imaging.generate/edit means the real parameter assembly, retry policy and
  b64 decoding in docproof.cover.imaging all run — and the inpaint test can
  read the mask bytes back out of the multipart stream it was handed.
- `docproof.canvas.assistant` is injected into sys.modules as a fake module,
  because the real one is a sibling still being built. The 501 path is
  tested by injecting `None` there instead, which makes the lazy import fail
  the same way a missing claude-agent-sdk would — so that test keeps passing
  once the real module lands.
- Every canvas document is fabricated in-test rather than ingested, except
  where the open endpoint's ingest path is itself under test; the conversion
  has its own suite (tests/test_canvas_ingest.py).
"""
from __future__ import annotations

import base64
import io
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import docproof.canvas
from app.main import create_app
from docproof.canvas.model import (ArtLayer, CanvasDoc, Frame, Size, TextLayer,
                                   load_doc, save_doc)
from docproof.cover import pipeline as cover_pipeline
from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.fonts import FAMILIES
from docproof.cover.imaging import IMAGE_COST
from docproof.cover.model import (Brief, ConceptState, Direction, Palette,
                                  build_spec)

COVER_KEY = "test-cover-key"
HEADERS = {"X-Cover-Key": COVER_KEY}
JOB_ID = "20260831-a1b2c3"
PLATE = "assets/c0_focal.png"
ART_ID = "ly_art001"
TEXT_ID = "ly_txt001"


# -- fixtures -----------------------------------------------------------------

def _png(size=(24, 36), color=(10, 20, 30, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _doc(job_id: str = JOB_ID) -> CanvasDoc:
    """One art layer over a real plate, one live text layer — the smallest
    document that exercises every op path and both regeneration verbs."""
    return CanvasDoc(
        job_id=job_id,
        canvas=Size(w=400, h=640),
        layers=[
            ArtLayer(id=ART_ID, name="focal",
                     frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0),
                     source=PLATE, prompt="a lighthouse at dusk, oil painting"),
            TextLayer(id=TEXT_ID, name="title",
                      frame=Frame(x=0.5, y=0.2, w=0.8, h=0.2),
                      text="The Lighthouse", family="Spectral", size=0.08,
                      color="#f5f1e8"),
        ])


@pytest.fixture
def jobs_root(tmp_path, monkeypatch) -> Path:
    """The cover job store this app answers out of. Set through the
    environment (COVER_DATA_PATH) rather than app.state, so the fallback in
    app/routes/canvas.py:_data_root — the one the Mac shell and the desktop
    app rely on — is what the whole suite exercises."""
    root = tmp_path / "cover_jobs"
    root.mkdir()
    monkeypatch.setenv("COVER_DATA_PATH", str(root))
    monkeypatch.setenv("COVER_KEY", COVER_KEY)
    return root


@pytest.fixture
def client(tmp_path, jobs_root, monkeypatch):
    app = create_app(tmp_path / "home", start_runner=False)
    with TestClient(app) as c:
        yield c


def _job_dir(jobs_root: Path, job_id: str = JOB_ID, *, doc: CanvasDoc | None = None,
             plate: bool = True) -> Path:
    """A job directory holding a canvas session (and, by default, the plate
    its art layer points at)."""
    job_dir = jobs_root / job_id
    (job_dir / "assets").mkdir(parents=True, exist_ok=True)
    if plate:
        (job_dir / PLATE).write_bytes(_png())
    save_doc(doc or _doc(job_id), job_dir / "canvas.json")
    return job_dir


def _palette() -> Palette:
    return Palette(background="#101820", primary="#f5f1e8", accent="#c9a227",
                   text="#f5f1e8", scrim="#000000")


def _cover_job(jobs_root: Path, *, statuses=("ready",)) -> str:
    """A real cover job on disk, written the way the pipeline writes one:
    a JobState per concept with a built spec. `statuses` names each
    concept's state, so a test can put an unfinished concept in front of a
    finished one."""
    job = cover_pipeline.create_job(
        jobs_root, Brief(title="The Lighthouse at Gull Point",
                         author="J. R. Vance", genre="literary", concepts=1))
    concepts = []
    for i, status in enumerate(statuses):
        direction = Direction(
            concept_name=f"Concept {i}", rationale="A test concept.",
            archetype="big_type", palette=_palette(),
            title_font="Playfair Display", author_font="Spectral",
            art_prompts={}, texture=False)
        spec = build_spec(direction, job.brief, ARCHETYPES["big_type"])
        concepts.append(ConceptState(spec=spec, status=status))
    job.concepts = concepts
    job.status = "ready"
    cover_pipeline._write_state(jobs_root, job)
    return job.job_id


# -- the stub image client ------------------------------------------------------

class _StubImages:
    """The bits of client.images the plate verbs touch, in both response
    shapes: one finished image, or — when the caller asked for `stream` —
    `partials` progressive frames followed by the finished one."""

    def __init__(self, png: bytes, partials: int = 2):
        self.png = png
        self.partials = partials
        self.generate_calls: list[dict] = []
        self.edit_calls: list[dict] = []

    def _reply(self, params: dict | None = None):
        b64 = base64.b64encode(self.png).decode("ascii")
        if params and params.get("stream"):
            events = [types.SimpleNamespace(
                type="image_generation.partial_image", b64_json=b64)
                for _ in range(self.partials)]
            events.append(types.SimpleNamespace(
                type="image_generation.completed", b64_json=b64))
            return iter(events)
        return types.SimpleNamespace(data=[types.SimpleNamespace(b64_json=b64)])

    def generate(self, **params):
        self.generate_calls.append(params)
        return self._reply(params)

    def edit(self, **params):
        # The SDK is handed file-like streams; read them here so the test can
        # assert on the actual mask bytes that would have gone over the wire.
        captured = dict(params)
        for field in ("image", "mask"):
            stream = captured.get(field)
            if stream is not None:
                stream.seek(0)
                captured[field] = stream.read()
        self.edit_calls.append(captured)
        return self._reply(params)


class _StubClient:
    def __init__(self, png: bytes):
        self.images = _StubImages(png)


def _stub_image_client(monkeypatch, png: bytes | None = None) -> _StubClient:
    client = _StubClient(png or _png((32, 48), (200, 180, 40, 255)))
    monkeypatch.setattr("app.routes.cover._image_client", lambda: client)
    return client


# -- the fake assistant ---------------------------------------------------------

class _Result:
    def __init__(self, reply, doc, ops_applied, cost_usd):
        self.reply = reply
        self.doc = doc
        self.ops_applied = ops_applied
        self.cost_usd = cost_usd


def _fake_assistant(monkeypatch, chat):
    """Install a module at docproof.canvas.assistant. Both the sys.modules
    entry and the package attribute are set: `from docproof.canvas import
    assistant` checks the attribute first and falls back to importing the
    submodule, and this has to win either way."""
    module = types.ModuleType("docproof.canvas.assistant")

    class AssistantUnavailable(RuntimeError):
        pass

    module.AssistantUnavailable = AssistantUnavailable
    module.chat = chat
    monkeypatch.setitem(sys.modules, "docproof.canvas.assistant", module)
    monkeypatch.setattr(docproof.canvas, "assistant", module, raising=False)
    return module


# -- the key gate ---------------------------------------------------------------

def test_canvas_endpoints_are_behind_the_cover_key(client, jobs_root, monkeypatch):
    _job_dir(jobs_root)
    assert client.get(f"/api/canvas/{JOB_ID}").status_code == 401
    wrong = client.get(f"/api/canvas/{JOB_ID}", headers={"X-Cover-Key": "nope"})
    assert wrong.status_code == 401

    monkeypatch.delenv("COVER_KEY")
    off = client.get(f"/api/canvas/{JOB_ID}", headers=HEADERS)
    assert off.status_code == 503
    assert "not enabled" in off.json()["detail"]


# -- open -----------------------------------------------------------------------

def test_open_returns_an_existing_session_and_ignores_concept(client, jobs_root):
    _job_dir(jobs_root)
    resp = client.post("/api/canvas/open",
                       json={"job_id": JOB_ID, "concept": 3}, headers=HEADERS)
    assert resp.status_code == 200
    doc = resp.json()["doc"]
    assert doc["job_id"] == JOB_ID
    assert [l["id"] for l in doc["layers"]] == [ART_ID, TEXT_ID]


def test_open_on_a_job_that_is_not_there_is_404(client, jobs_root):
    resp = client.post("/api/canvas/open", json={"job_id": JOB_ID},
                       headers=HEADERS)
    assert resp.status_code == 404
    assert "No cover job" in resp.json()["detail"]


def test_open_ingests_a_finished_cover_job_and_saves_it(client, jobs_root):
    job_id = _cover_job(jobs_root)
    resp = client.post("/api/canvas/open", json={"job_id": job_id},
                       headers=HEADERS)
    assert resp.status_code == 200, resp.text
    doc = resp.json()["doc"]
    assert doc["job_id"] == job_id
    assert doc["layers"]                       # the cover arrived as layers
    assert doc["cost_usd"] == 0.0
    # Persisted, and job.json was left exactly as the pipeline wrote it.
    saved = load_doc(jobs_root / job_id / "canvas.json")
    assert saved.job_id == job_id
    manifest = json.loads((jobs_root / job_id / "job.json").read_text("utf-8"))
    assert "cost_usd" not in manifest


def test_open_defaults_to_the_first_ready_concept(client, jobs_root):
    job_id = _cover_job(jobs_root, statuses=("error", "ready"))
    resp = client.post("/api/canvas/open", json={"job_id": job_id},
                       headers=HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json()["doc"]["source_spec"]["concept_name"] == "Concept 1"


def test_open_on_a_job_with_no_finished_concept_is_409(client, jobs_root):
    job = cover_pipeline.create_job(
        jobs_root, Brief(title="T", author="A", genre="literary", concepts=1))
    resp = client.post("/api/canvas/open", json={"job_id": job.job_id},
                       headers=HEADERS)
    assert resp.status_code == 409
    assert "no concepts" in resp.json()["detail"]


# -- get / put -------------------------------------------------------------------

def test_get_404s_until_a_session_exists(client, jobs_root):
    _cover_job(jobs_root)                       # a cover job, but no canvas.json
    resp = client.get(f"/api/canvas/{JOB_ID}", headers=HEADERS)
    assert resp.status_code == 404
    assert "No canvas session" in resp.json()["detail"]


def test_get_returns_the_saved_document(client, jobs_root):
    _job_dir(jobs_root)
    resp = client.get(f"/api/canvas/{JOB_ID}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["doc"]["canvas"] == {"w": 400, "h": 640}


def test_put_persists_the_whole_document(client, jobs_root):
    job_dir = _job_dir(jobs_root)
    payload = _doc().model_dump(mode="json")
    payload["layers"][1]["text"] = "The Lighthouse at Gull Point"
    resp = client.put(f"/api/canvas/{JOB_ID}", json={"doc": payload},
                      headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["doc"]["layers"][1]["text"] == "The Lighthouse at Gull Point"
    assert load_doc(job_dir / "canvas.json").layers[1].text == (
        "The Lighthouse at Gull Point")


def test_put_with_an_invalid_document_is_422_and_names_the_field(client, jobs_root):
    job_dir = _job_dir(jobs_root)
    payload = _doc().model_dump(mode="json")
    payload["layers"][1]["color"] = "puce"
    resp = client.put(f"/api/canvas/{JOB_ID}", json={"doc": payload},
                      headers=HEADERS)
    assert resp.status_code == 422
    assert "color" in resp.json()["detail"]
    # Nothing was written over.
    assert load_doc(job_dir / "canvas.json").layers[1].color == "#f5f1e8"


def test_put_of_another_jobs_document_is_409(client, jobs_root):
    _job_dir(jobs_root)
    payload = _doc("20260830-ffffff").model_dump(mode="json")
    resp = client.put(f"/api/canvas/{JOB_ID}", json={"doc": payload},
                      headers=HEADERS)
    assert resp.status_code == 409
    assert "20260830-ffffff" in resp.json()["detail"]


# -- ops --------------------------------------------------------------------------

def test_ops_mutates_and_persists(client, jobs_root):
    job_dir = _job_dir(jobs_root)
    resp = client.post(f"/api/canvas/{JOB_ID}/ops", headers=HEADERS, json={
        "ops": [{"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2},
                {"op": "set_text", "layer_id": TEXT_ID, "text": "Gull Point"}]})
    assert resp.status_code == 200, resp.text
    doc = resp.json()["doc"]
    assert doc["layers"][1]["frame"]["x"] == pytest.approx(0.3)
    assert doc["layers"][1]["text"] == "Gull Point"
    assert [h["op"] for h in doc["history"]] == ["nudge", "set_text"]

    saved = load_doc(job_dir / "canvas.json")
    assert saved.layers[1].text == "Gull Point"


def test_a_refused_op_is_409_and_lands_none_of_the_batch(client, jobs_root):
    job_dir = _job_dir(jobs_root)
    resp = client.post(f"/api/canvas/{JOB_ID}/ops", headers=HEADERS, json={
        "ops": [{"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2},
                {"op": "set_text", "layer_id": ART_ID, "text": "nope"}]})
    assert resp.status_code == 409
    assert "op 2 of 2" in resp.json()["detail"]
    saved = load_doc(job_dir / "canvas.json")
    assert saved.layers[1].frame.x == 0.5      # the first op did not land
    assert saved.history == []


def test_ops_refuse_a_locked_layer(client, jobs_root):
    doc = _doc()
    doc.layers[0].locked = True
    _job_dir(jobs_root, doc=doc)
    resp = client.post(f"/api/canvas/{JOB_ID}/ops", headers=HEADERS, json={
        "ops": [{"op": "nudge", "layer_id": ART_ID, "dy": 0.1}]})
    assert resp.status_code == 409
    assert "locked" in resp.json()["detail"]

    # set_layer is the exemption, because unlocking lives there.
    unlock = client.post(f"/api/canvas/{JOB_ID}/ops", headers=HEADERS, json={
        "ops": [{"op": "set_layer", "layer_id": ART_ID, "locked": False}]})
    assert unlock.status_code == 200


# -- reroll ------------------------------------------------------------------------

def test_reroll_writes_a_new_plate_and_charges_for_it(client, jobs_root, monkeypatch):
    job_dir = _job_dir(jobs_root)
    stub = _stub_image_client(monkeypatch)

    resp = client.post(f"/api/canvas/{JOB_ID}/reroll", headers=HEADERS,
                       json={"layer_id": ART_ID})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    layer = body["doc"]["layers"][0]

    assert layer["source"] == f"assets/canvas_{ART_ID}_1.png"
    assert (job_dir / layer["source"]).is_file()
    assert (job_dir / PLATE).is_file()          # the old plate is still there
    assert layer["plate_history"] == [
        {"source": PLATE, "prompt": "a lighthouse at dusk, oil painting"}]
    assert layer["prompt"] == "a lighthouse at dusk, oil painting"

    cost = IMAGE_COST[cover_pipeline.IMAGE_RESOLUTION]
    assert body["cost_usd"] == pytest.approx(cost)
    assert body["doc"]["cost_usd"] == pytest.approx(cost)
    assert body["doc"]["history"][-1] == {
        "op": "reroll", "layer_id": ART_ID,
        "source": f"assets/canvas_{ART_ID}_1.png", "prompt_overridden": False}

    [call] = stub.images.generate_calls
    assert call["prompt"] == "a lighthouse at dusk, oil painting"
    assert call["resolution"] == cover_pipeline.IMAGE_RESOLUTION

    # Persisted, and a second roll neither overwrites the first nor loses it.
    assert load_doc(job_dir / "canvas.json").layers[0].source == layer["source"]
    again = client.post(f"/api/canvas/{JOB_ID}/reroll", headers=HEADERS,
                        json={"layer_id": ART_ID, "prompt": "a brass foundry"})
    assert again.status_code == 200
    rolled = again.json()["doc"]["layers"][0]
    assert rolled["source"] == f"assets/canvas_{ART_ID}_2.png"
    assert rolled["prompt"] == "a brass foundry"
    assert len(rolled["plate_history"]) == 2
    assert again.json()["doc"]["history"][-1]["prompt_overridden"] is True
    assert again.json()["doc"]["cost_usd"] == pytest.approx(2 * cost)


def test_reroll_of_a_text_layer_is_502_with_a_sentence(client, jobs_root, monkeypatch):
    _job_dir(jobs_root)
    _stub_image_client(monkeypatch)
    resp = client.post(f"/api/canvas/{JOB_ID}/reroll", headers=HEADERS,
                       json={"layer_id": TEXT_ID})
    assert resp.status_code == 502
    assert "only art layers" in resp.json()["detail"]


def test_reroll_of_a_locked_layer_is_refused(client, jobs_root, monkeypatch):
    doc = _doc()
    doc.layers[0].locked = True
    _job_dir(jobs_root, doc=doc)
    _stub_image_client(monkeypatch)
    resp = client.post(f"/api/canvas/{JOB_ID}/reroll", headers=HEADERS,
                       json={"layer_id": ART_ID})
    assert resp.status_code == 502
    assert "locked" in resp.json()["detail"]


# -- inpaint -----------------------------------------------------------------------

def test_inpaint_sends_the_mask_and_writes_a_new_plate(client, jobs_root, monkeypatch):
    job_dir = _job_dir(jobs_root)
    stub = _stub_image_client(monkeypatch)
    mask = _png((24, 36), (255, 255, 255, 0))

    resp = client.post(f"/api/canvas/{JOB_ID}/inpaint", headers=HEADERS, json={
        "layer_id": ART_ID, "instruction": "remove the lamp",
        "mask_b64": "data:image/png;base64," + base64.b64encode(mask).decode()})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    layer = body["doc"]["layers"][0]

    assert layer["source"] == f"assets/canvas_{ART_ID}_1.png"
    assert (job_dir / layer["source"]).is_file()
    assert layer["plate_history"][0]["source"] == PLATE
    # An inpaint is a local repair, so the layer's own prompt is untouched.
    assert layer["prompt"] == "a lighthouse at dusk, oil painting"
    assert body["doc"]["history"][-1] == {
        "op": "inpaint", "layer_id": ART_ID,
        "source": f"assets/canvas_{ART_ID}_1.png",
        "instruction": "remove the lamp"}
    assert body["cost_usd"] == pytest.approx(
        IMAGE_COST[cover_pipeline.IMAGE_RESOLUTION])

    [call] = stub.images.edit_calls
    assert call["prompt"] == "remove the lamp"
    assert call["mask"] == mask                 # exactly the bytes we sent
    assert call["image"] == (job_dir / PLATE).read_bytes()


def test_inpaint_with_unreadable_base64_is_400(client, jobs_root, monkeypatch):
    _job_dir(jobs_root)
    _stub_image_client(monkeypatch)
    resp = client.post(f"/api/canvas/{JOB_ID}/inpaint", headers=HEADERS, json={
        "layer_id": ART_ID, "instruction": "x", "mask_b64": "   "})
    assert resp.status_code == 400
    assert "mask" in resp.json()["detail"]


def test_inpaint_with_a_missing_plate_is_502(client, jobs_root, monkeypatch):
    job_dir = _job_dir(jobs_root)
    (job_dir / PLATE).unlink()
    _stub_image_client(monkeypatch)
    resp = client.post(f"/api/canvas/{JOB_ID}/inpaint", headers=HEADERS, json={
        "layer_id": ART_ID, "instruction": "remove the lamp",
        "mask_b64": base64.b64encode(_png()).decode()})
    assert resp.status_code == 502
    assert "could not be read" in resp.json()["detail"]


# -- chat --------------------------------------------------------------------------

def test_chat_persists_the_document_the_assistant_returns(client, jobs_root,
                                                          monkeypatch):
    job_dir = _job_dir(jobs_root)
    seen = {}

    async def chat(job_dir_arg, doc, messages, mode, *, snapshot_png, model,
                   image_client):
        seen.update(job_dir=job_dir_arg, messages=messages, mode=mode,
                    snapshot=snapshot_png, model=model,
                    callable_client=callable(image_client))
        from docproof.canvas import ops as canvas_ops
        ops = [{"op": "nudge", "layer_id": TEXT_ID, "dy": 0.05}]
        canvas_ops.apply_many(doc, ops)
        return _Result("Moved the title down a touch.", doc, ops, 0.0)

    _fake_assistant(monkeypatch, chat)
    snapshot = base64.b64encode(_png()).decode()
    resp = client.post(f"/api/canvas/{JOB_ID}/chat", headers=HEADERS, json={
        "messages": [{"role": "user", "content": "nudge the title down"}],
        "mode": "act", "snapshot_b64": snapshot})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == "Moved the title down a touch."
    assert body["ops_applied"] == [{"op": "nudge", "layer_id": TEXT_ID, "dy": 0.05}]
    assert body["cost_usd"] == 0.0
    assert body["doc"]["layers"][1]["frame"]["y"] == pytest.approx(0.25)

    assert load_doc(job_dir / "canvas.json").layers[1].frame.y == pytest.approx(0.25)
    assert seen["job_dir"] == job_dir
    assert seen["mode"] == "act"
    assert seen["model"] is None
    assert seen["snapshot"] == base64.b64decode(snapshot)
    assert seen["callable_client"] is True


def test_chat_reports_an_unavailable_assistant_as_501(client, jobs_root,
                                                      monkeypatch):
    _job_dir(jobs_root)

    async def chat(*a, **k):
        raise module.AssistantUnavailable(
            "Claude Code is not logged in on this machine.")

    module = _fake_assistant(monkeypatch, chat)
    resp = client.post(f"/api/canvas/{JOB_ID}/chat", headers=HEADERS, json={
        "messages": [{"role": "user", "content": "hi"}], "mode": "plan"})
    assert resp.status_code == 501
    assert "not logged in" in resp.json()["detail"]


def test_chat_refuses_a_mode_that_is_not_plan_or_act(client, jobs_root, monkeypatch):
    _job_dir(jobs_root)

    async def chat(*a, **k):                     # pragma: no cover - never reached
        raise AssertionError("the assistant should not have been called")

    _fake_assistant(monkeypatch, chat)
    resp = client.post(f"/api/canvas/{JOB_ID}/chat", headers=HEADERS, json={
        "messages": [{"role": "user", "content": "hi"}], "mode": "meddle"})
    assert resp.status_code == 422


def test_chat_is_501_when_the_assistant_module_is_not_installed(client, jobs_root,
                                                               monkeypatch):
    _job_dir(jobs_root)
    # None in sys.modules makes the lazy `from docproof.canvas import
    # assistant` raise ImportError, exactly as a missing claude-agent-sdk
    # would — so this stays a real test of the 501 path after the module
    # itself lands.
    monkeypatch.setitem(sys.modules, "docproof.canvas.assistant", None)
    monkeypatch.delattr(docproof.canvas, "assistant", raising=False)
    resp = client.post(f"/api/canvas/{JOB_ID}/chat", headers=HEADERS, json={
        "messages": [{"role": "user", "content": "hi"}], "mode": "act"})
    assert resp.status_code == 501
    assert "claude-agent-sdk" in resp.json()["detail"]


# -- file serving -------------------------------------------------------------------

def test_file_serving_and_its_traversal_defense(client, jobs_root):
    job_dir = _job_dir(jobs_root)
    (jobs_root / "secret.png").write_bytes(_png())

    good = client.get(f"/api/canvas/{JOB_ID}/file/{PLATE}", headers=HEADERS)
    assert good.status_code == 200
    assert good.headers["content-type"] == "image/png"
    assert good.content == (job_dir / PLATE).read_bytes()

    for name in ("..%2Fsecret.png", "%2e%2e%2fsecret.png",
                 "%2Fetc%2Fpasswd", "job.json", "canvas.json",
                 "assets%2Fnope.png"):
        resp = client.get(f"/api/canvas/{JOB_ID}/file/{name}", headers=HEADERS)
        assert resp.status_code == 404, name


# -- fonts --------------------------------------------------------------------------

def test_fonts_css_declares_the_shelf_and_only_the_shelf_is_served(client):
    resp = client.get("/api/canvas/fonts.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    css = resp.text

    # A family whose name has a space, quoted so the client reads it whole,
    # and one that ships style companions.
    assert 'font-family: "IM FELL English";' in css
    assert 'font-family: "Spectral";' in css
    assert 'url("/api/canvas/font/Spectral-SemiBold.ttf")' in css
    assert 'url("/api/canvas/font/Spectral-Bold.ttf")' in css
    assert "font-weight: 700;" in css
    assert "font-style: italic;" in css
    assert css.count("@font-face") >= len(FAMILIES)

    served = client.get("/api/canvas/font/Spectral-SemiBold.ttf")
    assert served.status_code == 200
    assert served.headers["content-type"] == "font/ttf"
    assert served.content[:4] in (b"\x00\x01\x00\x00", b"true", b"ttcf")

    for bad in ("nope.ttf", "..%2F..%2Fjob.json", "%2Fetc%2Fpasswd"):
        assert client.get(f"/api/canvas/font/{bad}").status_code == 404


# -- export -------------------------------------------------------------------------

def test_export_writes_the_composite_into_the_job(client, jobs_root):
    job_dir = _job_dir(jobs_root)
    png = _png((48, 72))
    resp = client.post(f"/api/canvas/{JOB_ID}/export", headers=HEADERS,
                       json={"png_b64": base64.b64encode(png).decode()})
    assert resp.status_code == 200
    assert resp.json() == {"name": "renders/canvas_export.png"}
    assert (job_dir / "renders" / "canvas_export.png").read_bytes() == png


def test_export_on_a_job_that_is_not_there_is_404(client, jobs_root):
    resp = client.post(f"/api/canvas/{JOB_ID}/export", headers=HEADERS,
                       json={"png_b64": base64.b64encode(_png()).decode()})
    assert resp.status_code == 404


# -- the page ------------------------------------------------------------------------

def test_canvas_page_redirects_to_the_spa_keeping_the_job(client):
    resp = client.get(f"/canvas?job={JOB_ID}", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == f"/canvas/index.html?job={JOB_ID}"


# =============================================================================
# M2: the quality ladder, /finalize, /ground, /rebalance
# =============================================================================
#
# Same three seams as above -- the stub image client stands in for OpenAI, so
# the real parameter assembly in docproof.cover.imaging runs and the tests can
# read back exactly what would have gone over the wire (including, for
# /ground, the mask the SERVER drew).

# -- the quality ladder (§5, §8) --------------------------------------------

def test_reroll_at_draft_quality_rolls_and_prices_the_cheap_tier(
        client, jobs_root, monkeypatch):
    _job_dir(jobs_root)
    stub = _stub_image_client(monkeypatch)

    resp = client.post(f"/api/canvas/{JOB_ID}/reroll", headers=HEADERS,
                       json={"layer_id": ART_ID, "quality": "draft"})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    [call] = stub.images.generate_calls
    assert call["resolution"] == "1K"
    assert body["cost_usd"] == pytest.approx(IMAGE_COST["1K"])
    assert body["doc"]["cost_usd"] == pytest.approx(IMAGE_COST["1K"])


def test_reroll_at_final_quality_rolls_at_the_machine_tier(
        client, jobs_root, monkeypatch):
    _job_dir(jobs_root)
    stub = _stub_image_client(monkeypatch)
    resp = client.post(f"/api/canvas/{JOB_ID}/reroll", headers=HEADERS,
                       json={"layer_id": ART_ID, "quality": "final"})
    assert resp.status_code == 200, resp.text
    [call] = stub.images.generate_calls
    assert call["resolution"] == cover_pipeline.IMAGE_RESOLUTION
    assert resp.json()["cost_usd"] == pytest.approx(
        IMAGE_COST[cover_pipeline.IMAGE_RESOLUTION])


def test_a_quality_that_is_not_a_rung_is_422_at_the_door(client, jobs_root,
                                                          monkeypatch):
    """Closed with a Literal, exactly the way chat's `mode` is: a fourth
    value is a client bug, and 422 before the request reaches the money
    layer beats a 502 out of it."""
    _job_dir(jobs_root)
    _stub_image_client(monkeypatch)
    for endpoint, payload in (
            ("reroll", {"layer_id": ART_ID, "quality": "ultra"}),
            ("inpaint", {"layer_id": ART_ID, "instruction": "x",
                         "mask_b64": base64.b64encode(_png()).decode(),
                         "quality": "cinematic"})):
        resp = client.post(f"/api/canvas/{JOB_ID}/{endpoint}", headers=HEADERS,
                           json=payload)
        assert resp.status_code == 422, (endpoint, resp.text)


def test_inpaint_takes_the_quality_ladder_too(client, jobs_root, monkeypatch):
    _job_dir(jobs_root)
    stub = _stub_image_client(monkeypatch)
    resp = client.post(f"/api/canvas/{JOB_ID}/inpaint", headers=HEADERS, json={
        "layer_id": ART_ID, "instruction": "remove the lamp",
        "mask_b64": base64.b64encode(_png()).decode(), "quality": "draft"})
    assert resp.status_code == 200, resp.text
    [call] = stub.images.edit_calls
    assert call["resolution"] == "1K"
    assert resp.json()["cost_usd"] == pytest.approx(IMAGE_COST["1K"])


# -- finalize ----------------------------------------------------------------

def test_finalize_re_renders_the_kept_plate_with_no_mask(client, jobs_root,
                                                          monkeypatch):
    """The finalize call is images.edit with the plate and NO mask: the
    draft anchors the composition (gpt-image-2 has no seed) while the whole
    frame is re-rendered at the machine's tier."""
    job_dir = _job_dir(jobs_root)
    stub = _stub_image_client(monkeypatch)
    draft = (job_dir / PLATE).read_bytes()

    resp = client.post(f"/api/canvas/{JOB_ID}/finalize", headers=HEADERS,
                       json={"layer_id": ART_ID})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    layer = body["doc"]["layers"][0]

    assert layer["source"] == f"assets/canvas_{ART_ID}_1.png"
    assert (job_dir / layer["source"]).is_file()
    assert (job_dir / PLATE).is_file()          # the draft is still there
    assert layer["plate_history"][0]["source"] == PLATE
    assert layer["prompt"] == "a lighthouse at dusk, oil painting"

    tier = cover_pipeline.IMAGE_RESOLUTION
    assert body["cost_usd"] == pytest.approx(IMAGE_COST[tier])
    assert body["doc"]["history"][-1] == {
        "op": "finalize", "layer_id": ART_ID,
        "source": f"assets/canvas_{ART_ID}_1.png", "tier": tier}

    [call] = stub.images.edit_calls
    assert "mask" not in call                   # the whole point
    assert call["image"] == draft               # the draft itself was sent
    assert call["resolution"] == tier
    assert call["prompt"].startswith("Re-render this exact image")
    assert "a lighthouse at dusk, oil painting" in call["prompt"]
    assert not stub.images.generate_calls       # never a fresh generate()


def test_finalize_appends_the_callers_emphasis(client, jobs_root, monkeypatch):
    _job_dir(jobs_root)
    stub = _stub_image_client(monkeypatch)
    resp = client.post(f"/api/canvas/{JOB_ID}/finalize", headers=HEADERS,
                       json={"layer_id": ART_ID, "prompt": "especially the sea"})
    assert resp.status_code == 200, resp.text
    assert stub.images.edit_calls[0]["prompt"].endswith("especially the sea")


def test_finalize_of_a_text_layer_is_502_with_a_sentence(client, jobs_root,
                                                          monkeypatch):
    _job_dir(jobs_root)
    _stub_image_client(monkeypatch)
    resp = client.post(f"/api/canvas/{JOB_ID}/finalize", headers=HEADERS,
                       json={"layer_id": TEXT_ID})
    assert resp.status_code == 502
    assert "only art layers" in resp.json()["detail"]


def test_finalize_with_a_missing_plate_is_502(client, jobs_root, monkeypatch):
    job_dir = _job_dir(jobs_root)
    (job_dir / PLATE).unlink()
    _stub_image_client(monkeypatch)
    resp = client.post(f"/api/canvas/{JOB_ID}/finalize", headers=HEADERS,
                       json={"layer_id": ART_ID})
    assert resp.status_code == 502
    assert "could not be read" in resp.json()["detail"]


def test_finalize_runs_in_the_fake_lane_with_no_image_key(client, jobs_root,
                                                           monkeypatch):
    job_dir = _job_dir(jobs_root)
    monkeypatch.setenv("DOCPROOF_CANVAS_FAKE_IMAGING", "1")

    def no_key():                              # what a keyless machine does
        raise AssertionError("the fake lane must not build a vendor client")

    monkeypatch.setattr("app.routes.cover._image_client", no_key)
    resp = client.post(f"/api/canvas/{JOB_ID}/finalize", headers=HEADERS,
                       json={"layer_id": ART_ID})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cost_usd"] == 0.0
    assert (job_dir / body["doc"]["layers"][0]["source"]).is_file()


# -- ground the figure (§15.23) ----------------------------------------------

def test_ground_draws_its_own_bottom_band_mask(client, jobs_root, monkeypatch):
    """Unlike /inpaint, no mask crosses the wire: the band IS the recipe, so
    the server rasterizes it at the plate's own size -- transparent (=
    regenerate) across the bottom, opaque above."""
    job_dir = _job_dir(jobs_root)
    stub = _stub_image_client(monkeypatch)

    resp = client.post(f"/api/canvas/{JOB_ID}/ground", headers=HEADERS,
                       json={"layer_id": ART_ID})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    [call] = stub.images.edit_calls
    assert call["image"] == (job_dir / PLATE).read_bytes()
    mask = Image.open(io.BytesIO(call["mask"])).convert("RGBA")
    plate_w, plate_h = Image.open(io.BytesIO(_png())).size
    assert mask.size == (plate_w, plate_h)
    band = round(plate_h * 0.18)
    alpha = mask.getchannel("A")
    assert alpha.crop((0, plate_h - band, plate_w, plate_h)
                      ).getextrema() == (0, 0)
    assert alpha.crop((0, 0, plate_w, plate_h - band)
                      ).getextrema() == (255, 255)

    # The instruction template, not a free-text prompt.
    for clause in ("credible ground", "standing figure", "contact shadow",
                   "receding"):
        assert clause in call["prompt"]

    tier = cover_pipeline.IMAGE_RESOLUTION
    assert body["cost_usd"] == pytest.approx(IMAGE_COST[tier])
    assert body["doc"]["history"][-1]["op"] == "ground_figure"
    assert body["doc"]["layers"][0]["source"] == f"assets/canvas_{ART_ID}_1.png"
    # A local repair: the layer's own prompt is untouched.
    assert body["doc"]["layers"][0]["prompt"] == (
        "a lighthouse at dusk, oil painting")


def test_ground_appends_the_scenes_own_specifics(client, jobs_root,
                                                  monkeypatch):
    _job_dir(jobs_root)
    stub = _stub_image_client(monkeypatch)
    resp = client.post(f"/api/canvas/{JOB_ID}/ground", headers=HEADERS,
                       json={"layer_id": ART_ID,
                             "instruction": "a wet cobbled street"})
    assert resp.status_code == 200, resp.text
    assert stub.images.edit_calls[0]["prompt"].endswith("a wet cobbled street")


def test_ground_of_a_locked_layer_is_502(client, jobs_root, monkeypatch):
    doc = _doc()
    doc.layers[0].locked = True
    _job_dir(jobs_root, doc=doc)
    _stub_image_client(monkeypatch)
    resp = client.post(f"/api/canvas/{JOB_ID}/ground", headers=HEADERS,
                       json={"layer_id": ART_ID})
    assert resp.status_code == 502
    assert "locked" in resp.json()["detail"]


# -- rebalance ($0) -----------------------------------------------------------

def _dark_job(jobs_root: Path, color=(8, 8, 8, 255)) -> Path:
    """A job whose plate is nearly black -- the case the rebalance button
    exists for (§15.23 term 2: lift the plane BEFORE darkening it)."""
    job_dir = _job_dir(jobs_root)
    (job_dir / PLATE).write_bytes(_png(color=color))
    return job_dir


def test_rebalance_measures_nudges_and_reports(client, jobs_root, monkeypatch):
    job_dir = _dark_job(jobs_root)

    def no_key():
        raise AssertionError("rebalance must never build a vendor client")

    monkeypatch.setattr("app.routes.cover._image_client", no_key)

    resp = client.post(f"/api/canvas/{JOB_ID}/rebalance", headers=HEADERS,
                       json={"layer_id": ART_ID})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    [levels] = body["doc"]["layers"][0]["effects"]
    assert levels["type"] == "levels"
    assert levels["params"]["brightness"] > 0            # a dark plate lifts
    assert abs(levels["params"]["brightness"]) <= 0.15   # and stays clamped
    assert abs(levels["params"]["contrast"]) <= 0.15

    for phrase in ("mean luminance", "mirror symmetry", "brightness"):
        assert phrase in body["measured"]

    # $0, and it landed as a real op so it undoes like every other edit.
    assert body["doc"]["cost_usd"] == 0.0
    assert body["doc"]["history"][-1]["op"] == "set_effects"
    # Persisted, and no plate was written.
    assert load_doc(job_dir / "canvas.json").layers[0].effects[0].type == "levels"
    assert body["doc"]["layers"][0]["source"] == PLATE


def test_rebalance_twice_converges_instead_of_stacking(client, jobs_root):
    _dark_job(jobs_root)
    first = client.post(f"/api/canvas/{JOB_ID}/rebalance", headers=HEADERS,
                        json={"layer_id": ART_ID}).json()
    second = client.post(f"/api/canvas/{JOB_ID}/rebalance", headers=HEADERS,
                         json={"layer_id": ART_ID}).json()

    effects = second["doc"]["layers"][0]["effects"]
    assert len(effects) == 1                    # replaced, never appended
    assert effects == first["doc"]["layers"][0]["effects"]
    assert first["measured"] == second["measured"]
    assert len(second["doc"]["history"]) == len(first["doc"]["history"]) + 1


def test_rebalance_of_a_text_layer_is_409(client, jobs_root):
    """409, not 502: nothing upstream was asked anything, so naming a
    gateway would name something that was never involved."""
    _job_dir(jobs_root)
    resp = client.post(f"/api/canvas/{JOB_ID}/rebalance", headers=HEADERS,
                       json={"layer_id": TEXT_ID})
    assert resp.status_code == 409
    assert "only art layers" in resp.json()["detail"]


def test_rebalance_with_a_missing_plate_is_409(client, jobs_root):
    job_dir = _job_dir(jobs_root)
    (job_dir / PLATE).unlink()
    resp = client.post(f"/api/canvas/{JOB_ID}/rebalance", headers=HEADERS,
                       json={"layer_id": ART_ID})
    assert resp.status_code == 409
    assert "could not be read" in resp.json()["detail"]


def test_the_new_endpoints_are_behind_the_cover_key(client, jobs_root):
    _job_dir(jobs_root)
    for endpoint, payload in (("finalize", {"layer_id": ART_ID}),
                              ("ground", {"layer_id": ART_ID}),
                              ("rebalance", {"layer_id": ART_ID})):
        resp = client.post(f"/api/canvas/{JOB_ID}/{endpoint}", json=payload)
        assert resp.status_code == 401, (endpoint, resp.text)


def test_the_new_bodies_forbid_stray_fields(client, jobs_root):
    _job_dir(jobs_root)
    for endpoint, payload in (
            ("finalize", {"layer_id": ART_ID, "resolution": "4K"}),
            ("ground", {"layer_id": ART_ID, "mask_b64": "x"}),
            ("rebalance", {"layer_id": ART_ID, "strength": 0.5})):
        resp = client.post(f"/api/canvas/{JOB_ID}/{endpoint}", headers=HEADERS,
                           json=payload)
        assert resp.status_code == 422, (endpoint, resp.text)


# =============================================================================
# M3: the print wrap and the PDF export
# =============================================================================
#
# Two endpoints and one new answer shape. The wrap conversion itself is
# covered arithmetic-by-arithmetic in tests/test_canvas_ops.py; what is under
# test here is the HTTP shape of it -- the 409 a second conversion earns, and
# the panel geometry riding back with the document so the client can draw its
# fold lines from the same numbers the server used.

import re

from app.routes.canvas import PRINT_DPI, page_inches, pdf_bytes
from docproof.canvas.model import Wrap
from docproof.canvas.wrap import to_wrap

WRAP_BODY = {"trim_w_in": 6.0, "trim_h_in": 9.0, "spine_in": 0.75}


def _media_box(pdf: bytes) -> tuple[float, float]:
    """The page's size in POINTS, read back out of the PDF itself — 72 to
    the inch, which is the only number a printer's preflight will look at."""
    match = re.search(rb"MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
    assert match, "the PDF declares no MediaBox"
    return float(match.group(1)), float(match.group(2))


def _wrapped_job(jobs_root: Path) -> Path:
    """A job whose canvas session is already a full wrap."""
    job_dir = _job_dir(jobs_root)
    save_doc(to_wrap(_doc(), Wrap(**WRAP_BODY)), job_dir / "canvas.json")
    return job_dir


# -- POST /wrap ---------------------------------------------------------------

def test_wrap_converts_the_front_cover_and_answers_with_the_geometry(
        client, jobs_root):
    job_dir = _job_dir(jobs_root)
    resp = client.post(f"/api/canvas/{JOB_ID}/wrap", headers=HEADERS,
                       json=WRAP_BODY)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    doc = body["doc"]
    assert doc["wrap"] == {"trim_w_in": 6.0, "trim_h_in": 9.0,
                           "spine_in": 0.75, "bleed_in": 0.125, "dpi": 300}
    assert (doc["canvas"]["w"], doc["canvas"]["h"]) == (3900, 2775)
    # The front cover's own layers came across, plus the seeded panels.
    assert {ART_ID, TEXT_ID} <= {l["id"] for l in doc["layers"]}
    assert doc["layers"][0]["name"] == "wrap sheet"

    panels = body["panels"]
    assert panels["sheet"]["w_px"] == 3900
    assert panels["back"]["x1"] == pytest.approx(panels["spine"]["x0"])
    assert panels["spine"]["x1"] == pytest.approx(panels["front"]["x0"])

    # ...and it is what is on disk, not just what came back.
    assert load_doc(job_dir / "canvas.json").wrap is not None


def test_wrapping_a_wrap_is_409_with_the_reason(client, jobs_root):
    _wrapped_job(jobs_root)
    resp = client.post(f"/api/canvas/{JOB_ID}/wrap", headers=HEADERS,
                       json={**WRAP_BODY, "spine_in": 1.25})
    assert resp.status_code == 409
    assert "already a wrap" in resp.json()["detail"]
    assert "set_wrap" in resp.json()["detail"]


def test_the_spine_is_re_measured_through_ops_not_a_second_conversion(
        client, jobs_root):
    _wrapped_job(jobs_root)
    resp = client.post(f"/api/canvas/{JOB_ID}/ops", headers=HEADERS,
                       json={"ops": [{"op": "set_wrap", "spine_in": 1.25}]})
    assert resp.status_code == 200, resp.text
    doc = resp.json()["doc"]
    assert doc["wrap"]["spine_in"] == 1.25
    assert doc["canvas"]["w"] == round(13.5 * 300)


def test_wrapping_a_job_with_no_canvas_session_is_404(client, jobs_root):
    (jobs_root / JOB_ID).mkdir()
    resp = client.post(f"/api/canvas/{JOB_ID}/wrap", headers=HEADERS,
                       json=WRAP_BODY)
    assert resp.status_code == 404


@pytest.mark.parametrize("bad", [
    {**WRAP_BODY, "spine_in": 0.0},
    {**WRAP_BODY, "dpi": 5000},
    {**WRAP_BODY, "pages": 312},                    # not a field of the wrap
    {"trim_w_in": 6.0, "trim_h_in": 9.0},           # no spine at all
])
def test_a_wrap_that_is_not_a_book_is_422(client, jobs_root, bad):
    _job_dir(jobs_root)
    resp = client.post(f"/api/canvas/{JOB_ID}/wrap", headers=HEADERS, json=bad)
    assert resp.status_code == 422, resp.text


def test_wrap_is_behind_the_cover_key(client, jobs_root):
    _job_dir(jobs_root)
    assert client.post(f"/api/canvas/{JOB_ID}/wrap",
                       json=WRAP_BODY).status_code == 401


# -- the PDF export -----------------------------------------------------------

def test_a_wrap_exports_as_a_pdf_at_its_own_physical_size(client, jobs_root):
    job_dir = _wrapped_job(jobs_root)
    resp = client.post(f"/api/canvas/{JOB_ID}/export", headers=HEADERS,
                       json={"png_b64": base64.b64encode(_png((390, 278))).decode(),
                             "format": "pdf"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"name": "renders/canvas_export.pdf"}

    pdf = (job_dir / "renders" / "canvas_export.pdf").read_bytes()
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000
    # 13 x 9.25in at 72 points to the inch — the sheet, whatever resolution
    # the client happened to composite at.
    assert _media_box(pdf) == (pytest.approx(936.0), pytest.approx(666.0))


def test_a_front_only_export_reads_its_pixels_at_300dpi(client, jobs_root):
    job_dir = _job_dir(jobs_root)                   # a 400x640 canvas
    resp = client.post(f"/api/canvas/{JOB_ID}/export", headers=HEADERS,
                       json={"png_b64": base64.b64encode(_png((400, 640))).decode(),
                             "format": "pdf"})
    assert resp.status_code == 200, resp.text
    width, height = _media_box(
        (job_dir / "renders" / "canvas_export.pdf").read_bytes())
    assert width == pytest.approx(400 / PRINT_DPI * 72)
    assert height == pytest.approx(640 / PRINT_DPI * 72)


def test_the_png_export_is_untouched_by_the_pdf_lane(client, jobs_root):
    job_dir = _wrapped_job(jobs_root)
    png = _png((48, 72))
    resp = client.post(f"/api/canvas/{JOB_ID}/export", headers=HEADERS,
                       json={"png_b64": base64.b64encode(png).decode()})
    assert resp.json() == {"name": "renders/canvas_export.png"}
    assert (job_dir / "renders" / "canvas_export.png").read_bytes() == png


def test_the_exported_pdf_can_be_fetched_back_out_of_the_job(client, jobs_root):
    _wrapped_job(jobs_root)
    client.post(f"/api/canvas/{JOB_ID}/export", headers=HEADERS,
                json={"png_b64": base64.b64encode(_png((390, 278))).decode(),
                      "format": "pdf"})
    served = client.get(f"/api/canvas/{JOB_ID}/file/renders/canvas_export.pdf",
                        headers=HEADERS)
    assert served.status_code == 200
    assert served.headers["content-type"] == "application/pdf"
    assert served.content.startswith(b"%PDF")


def test_a_pdf_export_of_something_that_is_not_an_image_is_400(client, jobs_root):
    _wrapped_job(jobs_root)
    resp = client.post(f"/api/canvas/{JOB_ID}/export", headers=HEADERS,
                       json={"png_b64": base64.b64encode(b"not a png").decode(),
                             "format": "pdf"})
    assert resp.status_code == 400
    assert "could not be read" in resp.json()["detail"]


def test_a_third_export_format_is_refused_at_the_door(client, jobs_root):
    _job_dir(jobs_root)
    resp = client.post(f"/api/canvas/{JOB_ID}/export", headers=HEADERS,
                       json={"png_b64": base64.b64encode(_png()).decode(),
                             "format": "tiff"})
    assert resp.status_code == 422


# -- the inches-to-pixels arithmetic, on its own ------------------------------

def test_page_inches_reads_a_wrap_off_its_own_numbers():
    doc = to_wrap(_doc(), Wrap(**WRAP_BODY))
    assert page_inches(doc) == (13.0, 9.25)


def test_page_inches_reads_a_front_cover_at_print_resolution():
    assert page_inches(_doc()) == (400 / 300, 640 / 300)


@pytest.mark.parametrize("pixels", [(390, 278), (1300, 925), (3900, 2775)])
def test_the_page_is_the_same_size_at_every_composite_resolution(pixels):
    # The resolution is derived from the pixels actually handed over, so a
    # client that exported small gets a softer page, never a smaller one.
    assert _media_box(pdf_bytes(_png(pixels), 13.0, 9.25)) == (
        pytest.approx(936.0), pytest.approx(666.0))


def test_transparency_is_flattened_onto_the_paper():
    # A PDF page has no alpha channel and paper has no transparency; the
    # white IS the paper.
    pdf = pdf_bytes(_png((60, 90), color=(0, 0, 0, 0)), 2.0, 3.0)
    assert pdf.startswith(b"%PDF")
    assert _media_box(pdf) == (pytest.approx(144.0), pytest.approx(216.0))


# -- streaming plate calls (§5's wait) ---------------------------------------
#
# The same work, reported as it happens. A plate render is tens of seconds
# of blank screen otherwise, and the frames the vendor already paints on the
# way there are free.

def _ndjson(resp):
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_a_streamed_reroll_reports_partials_then_the_same_finished_payload(
        client, jobs_root, monkeypatch):
    job_dir = _job_dir(jobs_root)
    stub = _stub_image_client(monkeypatch)

    resp = client.post(f"/api/canvas/{JOB_ID}/reroll", headers=HEADERS,
                       json={"layer_id": ART_ID, "stream": True})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-ndjson")

    frames = _ndjson(resp)
    assert [f["event"] for f in frames] == ["partial", "partial", "done"]
    assert [f["index"] for f in frames[:-1]] == [1, 2]
    assert all(f["mime"] == "image/png" for f in frames[:-1])
    assert all(base64.b64decode(f["image_b64"]) for f in frames[:-1])

    done = frames[-1]
    assert done["doc"]["layers"][0]["source"] == f"assets/canvas_{ART_ID}_1.png"
    assert done["cost_usd"] == pytest.approx(
        IMAGE_COST[cover_pipeline.IMAGE_RESOLUTION])
    # ...and the plate and the document landed exactly as they do without
    # streaming: the response shape changed, the work did not.
    assert (job_dir / done["doc"]["layers"][0]["source"]).is_file()
    assert load_doc(job_dir / "canvas.json").layers[0].source == \
        done["doc"]["layers"][0]["source"]
    assert stub.images.generate_calls[0]["stream"] is True


def test_a_streamed_refusal_is_an_error_line_not_a_status_code(
        client, jobs_root, monkeypatch):
    """The 200 is long gone by the time a vendor fails, so the last line
    carries the sentence instead."""
    _job_dir(jobs_root)
    _stub_image_client(monkeypatch)

    resp = client.post(f"/api/canvas/{JOB_ID}/reroll", headers=HEADERS,
                       json={"layer_id": TEXT_ID, "stream": True})
    assert resp.status_code == 200
    [frame] = _ndjson(resp)
    assert frame["event"] == "error"
    assert "art layer" in frame["detail"] or "re-roll" in frame["detail"]


def test_not_asking_to_stream_still_answers_with_plain_json(
        client, jobs_root, monkeypatch):
    _job_dir(jobs_root)
    _stub_image_client(monkeypatch)
    resp = client.post(f"/api/canvas/{JOB_ID}/reroll", headers=HEADERS,
                       json={"layer_id": ART_ID})
    assert resp.headers["content-type"].startswith("application/json")
    assert "event" not in resp.json()


def test_every_money_verb_can_stream(client, jobs_root, monkeypatch):
    _job_dir(jobs_root)
    _stub_image_client(monkeypatch)
    mask = base64.b64encode(_png((32, 48), (0, 0, 0, 0))).decode("ascii")
    for path, body in (
            ("inpaint", {"layer_id": ART_ID, "instruction": "fix her hand",
                         "mask_b64": mask}),
            ("finalize", {"layer_id": ART_ID}),
            ("ground", {"layer_id": ART_ID})):
        resp = client.post(f"/api/canvas/{JOB_ID}/{path}", headers=HEADERS,
                           json={**body, "stream": True})
        assert resp.status_code == 200, (path, resp.text)
        frames = _ndjson(resp)
        assert frames[-1]["event"] == "done", (path, frames[-1])
        assert [f["event"] for f in frames[:-1]] == ["partial", "partial"]
