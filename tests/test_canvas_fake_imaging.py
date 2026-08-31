"""The $0 stand-in lane and the resolution knob (docproof/canvas/regen.py).

DOCPROOF_CANVAS_FAKE_IMAGING exists so the whole regeneration loop — plate
files on disk, history strips, cost math, the routes, the assistant's reroll
tool — can be exercised on a machine with no OpenAI key and no spend. These
tests hold the lane to its two promises: it goes nowhere near a vendor client
(client=None must work), and it charges exactly nothing. The resolution knob
is tested for the opposite reason: when it IS a real call, the tier must
price honestly and an unknown tier must refuse rather than quietly fall back.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from docproof.canvas import regen
from docproof.canvas.model import ArtLayer, CanvasDoc, Frame, Size
from docproof.cover import imaging

ART_ID = "ly_a91f01"


def _job(tmp_path, size=(64, 96)):
    """A job dir holding one real (tiny) plate, and a doc pointing at it."""
    assets = tmp_path / "assets"
    assets.mkdir()
    Image.new("RGBA", size, "#334455").save(assets / "c0_background.png")
    doc = CanvasDoc(
        job_id="20260831T120000Z-fake01",
        canvas=Size(w=1600, h=2560),
        layers=[ArtLayer(id=ART_ID, name="background",
                         frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0),
                         source="assets/c0_background.png",
                         prompt="A smoky brass foundry at dusk.",
                         transparent=False)])
    return tmp_path, doc


@pytest.fixture
def fake_lane(monkeypatch):
    monkeypatch.setenv(regen.FAKE_ENV, "1")


def test_fake_reroll_needs_no_client_and_charges_nothing(tmp_path, fake_lane):
    job_dir, doc = _job(tmp_path)
    cost = regen.reroll(job_dir, doc, ART_ID, client=None)
    layer = doc.layer(ART_ID)
    assert cost == 0.0 and doc.cost_usd == 0.0
    assert layer.source == f"assets/canvas_{ART_ID}_1.png"
    assert (job_dir / layer.source).exists()
    assert layer.plate_history[-1].source == "assets/c0_background.png"


def test_fake_reroll_plate_differs_from_the_original(tmp_path, fake_lane):
    """The stand-in must be VISIBLY a new plate — a byte-identical copy would
    make the UI look wired to nothing during exactly the testing it exists
    for."""
    job_dir, doc = _job(tmp_path)
    before = (job_dir / "assets/c0_background.png").read_bytes()
    regen.reroll(job_dir, doc, ART_ID, client=None)
    after = (job_dir / doc.layer(ART_ID).source).read_bytes()
    assert after != before
    assert Image.open(io.BytesIO(after)).size == (64, 96)


def test_fake_inpaint_tints_only_the_masked_region(tmp_path, fake_lane):
    job_dir, doc = _job(tmp_path)
    # Mask convention (imaging.edit): transparent = regenerate. Left half
    # transparent, right half opaque.
    mask = Image.new("RGBA", (64, 96), (255, 255, 255, 255))
    mask.paste((0, 0, 0, 0), (0, 0, 32, 96))
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    cost = regen.inpaint(job_dir, doc, ART_ID, client=None,
                         instruction="remove the lamp",
                         mask_png=buf.getvalue())
    assert cost == 0.0 and doc.cost_usd == 0.0
    out = Image.open(job_dir / doc.layer(ART_ID).source).convert("RGB")
    # The untinted half is still the plate's own color (modulo the stamped
    # label, which sits in the tinted half's corner); the tinted half is not.
    assert out.getpixel((48, 90)) == (0x33, 0x44, 0x55)
    assert out.getpixel((16, 90)) != (0x33, 0x44, 0x55)


def test_fake_reroll_missing_plate_still_produces_a_card(tmp_path, fake_lane):
    """A doc can point at a plate that never landed (an interrupted job);
    the fake lane paints a flat card rather than failing the test session."""
    job_dir, doc = _job(tmp_path)
    (job_dir / "assets/c0_background.png").unlink()
    regen.reroll(job_dir, doc, ART_ID, client=None)
    assert (job_dir / doc.layer(ART_ID).source).exists()


def test_resolution_env_prices_the_tier_it_names(tmp_path, monkeypatch):
    job_dir, doc = _job(tmp_path)
    monkeypatch.setenv(regen.RESOLUTION_ENV, "1K")
    seen = {}

    def fake_generate(client, prompt, *, transparent, resolution,
                      output_format="png", on_partial=None):
        seen["resolution"] = resolution
        seen["output_format"] = output_format
        buf = io.BytesIO()
        Image.new("RGBA", (8, 12), "#000000").save(buf, format="PNG")
        return buf.getvalue()

    monkeypatch.setattr(regen.imaging, "generate", fake_generate)
    cost = regen.reroll(job_dir, doc, ART_ID, client=object())
    assert seen["resolution"] == "1K"
    # The draft tier ships as webp: a fraction of the bytes for the same
    # picture, and the draft lane is where the waiting hurts (regen.DRAFT_FORMAT).
    assert seen["output_format"] == regen.DRAFT_FORMAT
    assert cost == pytest.approx(imaging.IMAGE_COST["1K"])
    assert doc.cost_usd == pytest.approx(imaging.IMAGE_COST["1K"])


def test_unknown_resolution_refuses_rather_than_falling_back(tmp_path,
                                                             monkeypatch):
    job_dir, doc = _job(tmp_path)
    monkeypatch.setenv(regen.RESOLUTION_ENV, "8K")
    with pytest.raises(regen.RegenError, match="8K"):
        regen.reroll(job_dir, doc, ART_ID, client=object())
    # Refused before anything happened: no plate written, nothing charged.
    assert doc.layer(ART_ID).source == "assets/c0_background.png"
    assert doc.cost_usd == 0.0


# =============================================================================
# M2: the quality ladder, finalize, ground-the-figure, rebalance
# =============================================================================
#
# Same discipline as above -- no vendor is ever reached. The money verbs are
# driven by monkeypatching docproof.cover.imaging's three entry points at
# regen's own import site, which is the seam where the *arguments* are worth
# asserting: the tier a call was priced at has to be the tier it was made at,
# or §8's running total is fiction.

def _plate_job(tmp_path, *, size=(64, 96), color="#334455", prompt="A foundry.",
               layer_id=ART_ID, effects=None):
    """A job dir with one plate of a chosen colour, and a doc pointing at it.
    Local to this section so the M1 fixture above stays exactly as it was."""
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    Image.new("RGBA", size, color).save(assets / "c0_background.png")
    doc = CanvasDoc(
        job_id="20260831T120000Z-fake01",
        canvas=Size(w=1600, h=2560),
        layers=[ArtLayer(id=layer_id, name="background",
                         frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0),
                         source="assets/c0_background.png", prompt=prompt,
                         effects=effects or [], transparent=False)])
    return tmp_path, doc


def _capture(monkeypatch, name):
    """Replace one imaging entry point with a recorder that returns a real
    (tiny) PNG. Returns the list its calls land in."""
    calls: list[dict] = []

    def recorder(client, *args, **kwargs):
        calls.append({"client": client, "args": args, **kwargs})
        buf = io.BytesIO()
        Image.new("RGBA", (8, 12), "#000000").save(buf, format="PNG")
        return buf.getvalue()

    monkeypatch.setattr(regen.imaging, name, recorder)
    return calls


def _mask_png(size, band=None) -> bytes:
    """An imaging.edit-convention mask: opaque everywhere, transparent over
    `band` (a PIL box) or over the whole left half when band is None."""
    w, h = size
    mask = Image.new("RGBA", size, (255, 255, 255, 255))
    mask.paste((0, 0, 0, 0), band or (0, 0, w // 2, h))
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue()


# -- the quality ladder (§5, §8) ---------------------------------------------

def test_draft_quality_rolls_and_prices_the_cheap_tier(tmp_path, monkeypatch):
    """The whole point of the ladder: a draft costs 3 cents no matter what
    tier this machine is set to, and it is ROLLED at the tier it is PRICED
    at -- a 4K roll billed as a draft would quietly break §8."""
    job_dir, doc = _plate_job(tmp_path)
    monkeypatch.setenv(regen.RESOLUTION_ENV, "4K")
    calls = _capture(monkeypatch, "generate")

    cost = regen.reroll(job_dir, doc, ART_ID, client=object(), quality="draft")

    assert calls[0]["resolution"] == regen.DRAFT_TIER == "1K"
    assert cost == pytest.approx(imaging.IMAGE_COST["1K"])
    assert doc.cost_usd == pytest.approx(imaging.IMAGE_COST["1K"])


def test_final_and_session_quality_both_roll_at_the_machine_tier(tmp_path,
                                                                  monkeypatch):
    job_dir, doc = _plate_job(tmp_path)
    monkeypatch.setenv(regen.RESOLUTION_ENV, "2K")
    calls = _capture(monkeypatch, "generate")

    final = regen.reroll(job_dir, doc, ART_ID, client=object(), quality="final")
    session = regen.reroll(job_dir, doc, ART_ID, client=object(),
                           quality="session")

    assert [c["resolution"] for c in calls] == ["2K", "2K"]
    assert final == session == pytest.approx(imaging.IMAGE_COST["2K"])


def test_the_default_quality_is_todays_behaviour(tmp_path, monkeypatch):
    """A caller that never heard of `quality` must behave exactly as it did
    before the ladder existed -- that is what makes this an addition rather
    than a repricing."""
    job_dir, doc = _plate_job(tmp_path)
    monkeypatch.setenv(regen.RESOLUTION_ENV, "2K")
    calls = _capture(monkeypatch, "generate")
    regen.reroll(job_dir, doc, ART_ID, client=object())
    assert calls[0]["resolution"] == "2K"


def test_an_unknown_quality_is_refused_by_name(tmp_path, monkeypatch):
    job_dir, doc = _plate_job(tmp_path)
    calls = _capture(monkeypatch, "generate")
    with pytest.raises(regen.RegenError) as excinfo:
        regen.reroll(job_dir, doc, ART_ID, client=object(), quality="ultra")
    message = str(excinfo.value)
    assert "ultra" in message
    for rung in regen.QUALITY_LEVELS:
        assert rung in message
    # Refused before anything happened: no call, no plate, nothing charged.
    assert calls == []
    assert doc.layer(ART_ID).source == "assets/c0_background.png"
    assert doc.cost_usd == 0.0


def test_inpaint_takes_the_same_ladder(tmp_path, monkeypatch):
    job_dir, doc = _plate_job(tmp_path)
    monkeypatch.setenv(regen.RESOLUTION_ENV, "4K")
    calls = _capture(monkeypatch, "edit")
    mask = _mask_png((64, 96))

    cost = regen.inpaint(job_dir, doc, ART_ID, client=object(),
                         instruction="remove the lamp", mask_png=mask,
                         quality="draft")

    assert calls[0]["resolution"] == "1K"
    assert cost == pytest.approx(imaging.IMAGE_COST["1K"])


def test_inpaint_refuses_an_unknown_quality(tmp_path, monkeypatch):
    job_dir, doc = _plate_job(tmp_path)
    with pytest.raises(regen.RegenError, match="draft"):
        regen.inpaint(job_dir, doc, ART_ID, client=object(),
                      instruction="x", mask_png=_mask_png((64, 96)),
                      quality="cinematic")


def test_a_draft_roll_does_not_consult_the_resolution_knob(tmp_path,
                                                            monkeypatch):
    """RESOLUTION_ENV names what a FULL-quality call rolls at, so a machine
    whose knob is set to a tier this engine cannot price can still roll
    drafts -- and only the calls that would actually have used it refuse."""
    job_dir, doc = _plate_job(tmp_path)
    monkeypatch.setenv(regen.RESOLUTION_ENV, "8K")
    calls = _capture(monkeypatch, "generate")

    cost = regen.reroll(job_dir, doc, ART_ID, client=object(), quality="draft")
    assert calls[0]["resolution"] == "1K"
    assert cost == pytest.approx(imaging.IMAGE_COST["1K"])

    with pytest.raises(regen.RegenError, match="8K"):
        regen.reroll(job_dir, doc, ART_ID, client=object(), quality="final")


# -- finalize: the top of the ladder -----------------------------------------

def test_finalize_refines_the_current_plate_at_the_machine_tier(tmp_path,
                                                                 monkeypatch):
    job_dir, doc = _plate_job(tmp_path, prompt="a lighthouse at dusk")
    monkeypatch.setenv(regen.RESOLUTION_ENV, "4K")
    calls = _capture(monkeypatch, "refine")

    cost = regen.finalize(job_dir, doc, ART_ID, client=object())

    # The DRAFT ITSELF is what was sent -- that is what anchors the
    # composition through a re-render of an engine that has no seed.
    image_png, prompt = calls[0]["args"]
    assert image_png == (job_dir / "assets/c0_background.png").read_bytes()
    assert calls[0]["resolution"] == "4K"
    assert cost == pytest.approx(imaging.IMAGE_COST["4K"])

    assert prompt.startswith(regen.FINALIZE_INSTRUCTION)
    assert "a lighthouse at dusk" in prompt     # subject context, last-but-one

    layer = doc.layer(ART_ID)
    assert layer.source == f"assets/canvas_{ART_ID}_1.png"
    assert (job_dir / layer.source).is_file()
    assert layer.plate_history[-1].source == "assets/c0_background.png"
    # Finalizing is not a new description of the picture.
    assert layer.prompt == "a lighthouse at dusk"
    assert doc.history[-1] == {"op": "finalize", "layer_id": ART_ID,
                               "source": f"assets/canvas_{ART_ID}_1.png",
                               "tier": "4K"}


def test_finalize_appends_the_callers_emphasis_last(tmp_path, monkeypatch):
    job_dir, doc = _plate_job(tmp_path, prompt="a lighthouse at dusk")
    calls = _capture(monkeypatch, "refine")
    regen.finalize(job_dir, doc, ART_ID, client=object(),
                   prompt="especially the water")
    prompt = calls[0]["args"][1]
    assert prompt.startswith(regen.FINALIZE_INSTRUCTION)
    assert prompt.endswith("especially the water")


def test_finalize_needs_no_prompt_on_the_layer(tmp_path, monkeypatch):
    """An uploaded or procedural plate has no prompt, so a re-roll of it is
    correctly refused -- but the plate IS the request for a finalize, so
    this one goes through."""
    job_dir, doc = _plate_job(tmp_path, prompt="")
    calls = _capture(monkeypatch, "refine")
    with pytest.raises(regen.RegenError, match="no prompt"):
        regen.reroll(job_dir, doc, ART_ID, client=object())
    regen.finalize(job_dir, doc, ART_ID, client=object())
    assert calls[0]["args"][1] == regen.FINALIZE_INSTRUCTION


def test_fake_finalize_needs_no_client_and_charges_nothing(tmp_path, fake_lane,
                                                            monkeypatch):
    job_dir, doc = _plate_job(tmp_path)
    labels: list[str] = []
    original = regen._fake_plate
    monkeypatch.setattr(regen, "_fake_plate",
                        lambda job, layer, label, **kw: (
                            labels.append(label) or original(job, layer, label,
                                                             **kw)))

    cost = regen.finalize(job_dir, doc, ART_ID, client=None)

    assert cost == 0.0 and doc.cost_usd == 0.0
    assert labels == ["FAKE FINAL"]
    layer = doc.layer(ART_ID)
    assert (job_dir / layer.source).is_file()
    assert layer.source != "assets/c0_background.png"
    assert doc.history[-1]["op"] == "finalize"


def test_finalize_of_a_missing_plate_is_refused(tmp_path, monkeypatch):
    job_dir, doc = _plate_job(tmp_path)
    (job_dir / "assets/c0_background.png").unlink()
    calls = _capture(monkeypatch, "refine")
    with pytest.raises(regen.RegenError, match="could not be read"):
        regen.finalize(job_dir, doc, ART_ID, client=object())
    assert calls == []


def test_finalize_refuses_a_locked_layer(tmp_path, monkeypatch):
    job_dir, doc = _plate_job(tmp_path)
    doc.layer(ART_ID).locked = True
    with pytest.raises(regen.RegenError, match="locked"):
        regen.finalize(job_dir, doc, ART_ID, client=object())


# -- ground the figure (§15.23) -----------------------------------------------

@pytest.mark.parametrize("size", [(64, 96), (24, 36), (1024, 1536)])
def test_ground_mask_is_a_bottom_band_at_the_plates_own_size(size):
    """imaging.edit's convention: transparent = regenerate. So the band
    across the bottom is transparent, everything above it is opaque, and the
    mask is exactly the plate's own pixels."""
    mask = Image.open(io.BytesIO(regen._ground_mask(size))).convert("RGBA")
    w, h = size
    assert mask.size == size
    alpha = mask.getchannel("A")
    band = round(h * regen.GROUND_BAND_FRACTION)
    # Bottom band: fully transparent, every row of it.
    assert alpha.crop((0, h - band, w, h)).getextrema() == (0, 0)
    # Everything above: fully opaque, right up to the seam -- a hard edge,
    # not a feather (see _ground_mask's docstring).
    assert alpha.crop((0, 0, w, h - band)).getextrema() == (255, 255)


def test_ground_figure_draws_its_own_mask_and_states_the_recipe(tmp_path,
                                                                 monkeypatch):
    job_dir, doc = _plate_job(tmp_path, size=(64, 96))
    monkeypatch.setenv(regen.RESOLUTION_ENV, "2K")
    calls = _capture(monkeypatch, "edit")

    cost = regen.ground_figure(job_dir, doc, ART_ID, client=object())

    image_png, mask_png, instruction = calls[0]["args"]
    assert image_png == (job_dir / "assets/c0_background.png").read_bytes()
    mask = Image.open(io.BytesIO(mask_png)).convert("RGBA")
    assert mask.size == (64, 96)
    band = round(96 * regen.GROUND_BAND_FRACTION)
    assert mask.getchannel("A").getextrema() == (0, 255)
    assert mask.getpixel((32, 95))[3] == 0            # inside the band
    assert mask.getpixel((32, 96 - band - 1))[3] == 255  # just above it

    assert instruction == regen.GROUND_INSTRUCTION
    # The three clauses §15.23 actually requires of the generated ground.
    for clause in ("consistent with its own light", "receding",
                   "contact shadow"):
        assert clause in instruction

    # Charged like an inpaint, at the session tier.
    assert calls[0]["resolution"] == "2K"
    assert cost == pytest.approx(imaging.IMAGE_COST["2K"])
    assert doc.history[-1] == {
        "op": "ground_figure", "layer_id": ART_ID,
        "source": f"assets/canvas_{ART_ID}_1.png",
        "instruction": regen.GROUND_INSTRUCTION}


def test_ground_figure_appends_the_scenes_own_specifics(tmp_path, monkeypatch):
    job_dir, doc = _plate_job(tmp_path)
    calls = _capture(monkeypatch, "edit")
    regen.ground_figure(job_dir, doc, ART_ID, client=object(),
                        instruction="a wet cobbled street")
    instruction = calls[0]["args"][2]
    assert instruction.startswith(regen.GROUND_INSTRUCTION)
    assert instruction.endswith("a wet cobbled street")


def test_ground_figure_leaves_the_layers_own_prompt_alone(tmp_path,
                                                          monkeypatch):
    """A ground band is a local repair, not a new description of the plate
    -- exactly inpaint's rule."""
    job_dir, doc = _plate_job(tmp_path, prompt="a lighthouse at dusk")
    _capture(monkeypatch, "edit")
    regen.ground_figure(job_dir, doc, ART_ID, client=object())
    assert doc.layer(ART_ID).prompt == "a lighthouse at dusk"


def test_fake_ground_tints_only_the_band(tmp_path, fake_lane):
    """The mask reaches _fake_plate, so the $0 lane shows exactly where a
    real ground call would land."""
    job_dir, doc = _plate_job(tmp_path, size=(64, 96))
    cost = regen.ground_figure(job_dir, doc, ART_ID, client=None)
    assert cost == 0.0 and doc.cost_usd == 0.0
    out = Image.open(job_dir / doc.layer(ART_ID).source).convert("RGB")
    band = round(96 * regen.GROUND_BAND_FRACTION)
    assert out.getpixel((32, 95)) != (0x33, 0x44, 0x55)          # in the band
    assert out.getpixel((32, 96 - band - 1)) == (0x33, 0x44, 0x55)  # above it


def test_ground_figure_of_a_missing_plate_is_refused(tmp_path, fake_lane):
    """Unlike an inpaint, this reads the plate in BOTH lanes -- the mask
    geometry is the plate's own size, so there is nothing to draw without
    it."""
    job_dir, doc = _plate_job(tmp_path)
    (job_dir / "assets/c0_background.png").unlink()
    with pytest.raises(regen.RegenError, match="could not be read"):
        regen.ground_figure(job_dir, doc, ART_ID, client=None)


# -- rebalance: measure, nudge, report ($0) -----------------------------------

def test_rebalance_lifts_a_dark_plate_and_clamps_the_lift(tmp_path):
    job_dir, doc = _plate_job(tmp_path, color="#0a0a0a")
    measured = regen.rebalance(job_dir, doc, ART_ID)

    [levels] = doc.layer(ART_ID).effects
    assert levels.type == regen.LEVELS_EFFECT
    assert levels.params["brightness"] > 0
    assert levels.params["brightness"] == pytest.approx(regen.LEVELS_CLAMP)
    assert abs(levels.params["contrast"]) <= regen.LEVELS_CLAMP
    assert "mean luminance" in measured


def test_rebalance_darkens_a_bright_plate_within_the_clamp(tmp_path):
    """The correction has a sign and is not always pinned to the clamp --
    a plate near the target moves by a little, which is what makes this a
    nudge rather than a grade."""
    job_dir, doc = _plate_job(tmp_path, color="#bbbbbb")
    regen.rebalance(job_dir, doc, ART_ID)
    [levels] = doc.layer(ART_ID).effects
    assert -regen.LEVELS_CLAMP < levels.params["brightness"] < 0


def test_rebalance_replaces_its_levels_entry_rather_than_stacking(tmp_path):
    """Two levels entries on one layer would apply twice, so pressing the
    button again would brighten the plate again, forever. It converges
    instead: the plate on disk never changed, so the measurement never
    changed, so the answer never changed."""
    job_dir, doc = _plate_job(tmp_path, color="#0a0a0a")
    first = regen.rebalance(job_dir, doc, ART_ID)
    before = len(doc.history)
    second = regen.rebalance(job_dir, doc, ART_ID)

    effects = doc.layer(ART_ID).effects
    assert len([e for e in effects if e.type == regen.LEVELS_EFFECT]) == 1
    assert len(effects) == 1
    assert first == second
    assert effects[0].params == {"brightness": pytest.approx(regen.LEVELS_CLAMP),
                                 "contrast": pytest.approx(regen.LEVELS_CLAMP)}
    assert len(doc.history) == before + 1        # it did go through ops


def test_rebalance_keeps_other_effects_where_they_were(tmp_path):
    """Effect order is paint order, so re-balancing must not shuffle the
    stack -- the levels entry is replaced IN PLACE."""
    job_dir, doc = _plate_job(tmp_path, color="#0a0a0a", effects=[
        {"type": "bevel", "params": {"depth": 0.2}},
        {"type": "levels", "params": {"brightness": -0.4, "contrast": 0.0}},
        {"type": "edge_glow", "params": {"alpha": 0.3}}])
    regen.rebalance(job_dir, doc, ART_ID)
    effects = doc.layer(ART_ID).effects
    assert [e.type for e in effects] == ["bevel", "levels", "edge_glow"]
    assert effects[1].params["brightness"] == pytest.approx(regen.LEVELS_CLAMP)
    assert effects[0].params == {"depth": 0.2}


def test_rebalance_appends_when_the_layer_has_no_levels_yet(tmp_path):
    job_dir, doc = _plate_job(tmp_path, color="#0a0a0a", effects=[
        {"type": "bevel", "params": {"depth": 0.2}}])
    regen.rebalance(job_dir, doc, ART_ID)
    assert [e.type for e in doc.layer(ART_ID).effects] == ["bevel", "levels"]


def test_rebalance_costs_nothing_and_lands_in_the_op_log(tmp_path):
    job_dir, doc = _plate_job(tmp_path, color="#0a0a0a")
    regen.rebalance(job_dir, doc, ART_ID)
    assert doc.cost_usd == 0.0
    # Not a plate verb's hand-written record -- a real op, so it undoes the
    # way every other edit does.
    assert doc.history[-1]["op"] == "set_effects"
    assert doc.history[-1]["layer_id"] == ART_ID
    # And no plate was written: rebalance never touches the pixels on disk.
    assert doc.layer(ART_ID).source == "assets/c0_background.png"
    assert doc.layer(ART_ID).plate_history == []


def test_rebalance_reports_every_number_it_measured(tmp_path):
    job_dir, doc = _plate_job(tmp_path, color="#0a0a0a")
    measured = regen.rebalance(job_dir, doc, ART_ID)
    for phrase in ("mean luminance", "contrast spread", "mirror symmetry",
                   "centre of mass", "brightness", "contrast"):
        assert phrase in measured
    assert measured.rstrip().endswith(".")


def test_rebalance_refuses_a_locked_layer_and_a_missing_plate(tmp_path):
    job_dir, doc = _plate_job(tmp_path)
    doc.layer(ART_ID).locked = True
    with pytest.raises(regen.RegenError, match="locked"):
        regen.rebalance(job_dir, doc, ART_ID)

    doc.layer(ART_ID).locked = False
    (job_dir / "assets/c0_background.png").unlink()
    with pytest.raises(regen.RegenError, match="could not be read"):
        regen.rebalance(job_dir, doc, ART_ID)


def test_measure_plate_uses_the_balance_engines_own_numbers(tmp_path,
                                                            monkeypatch):
    """The symmetry and centre-of-mass figures are docproof.cover.balance's,
    passed through verbatim; only the exposure pair is computed here,
    because balance has no exposure measurement at all."""
    job_dir, doc = _plate_job(tmp_path, color="#0a0a0a")
    seen: list[str] = []
    original = regen.balance.measure_composite

    def spy(rgb, axis):
        seen.append(axis)
        return original(rgb, axis)

    monkeypatch.setattr(regen.balance, "measure_composite", spy)
    reading = regen.measure_plate(job_dir, doc, doc.layer(ART_ID))
    assert seen == ["center"]              # no axis in source_spec
    assert 0.0 <= reading.symmetry <= 1.0
    assert 0.0 <= reading.center_of_mass_x <= 1.0
    assert reading.mean_luminance < 0.05
    assert reading.warnings == []


def test_measure_plate_judges_against_the_source_specs_own_axis(tmp_path,
                                                                monkeypatch):
    job_dir, doc = _plate_job(tmp_path)
    doc.source_spec = {"axis": "left", "axis_x": 0.08}
    seen: list[str] = []
    original = regen.balance.measure_composite
    monkeypatch.setattr(regen.balance, "measure_composite",
                        lambda rgb, axis: (seen.append(axis)
                                           or original(rgb, axis)))
    regen.measure_plate(job_dir, doc, doc.layer(ART_ID))
    assert seen == ["left"]


def test_plan_levels_is_pure(tmp_path):
    """It takes plain dicts and returns plain dicts -- the caller's own list
    is never mutated, so a failed op cannot leave a half-edited stack."""
    stack = [{"type": "levels", "params": {"brightness": 0.0, "contrast": 0.0}}]
    out = regen.plan_levels(stack, 0.1, -0.1)
    assert out == [{"type": "levels",
                    "params": {"brightness": 0.1, "contrast": -0.1}}]
    assert stack == [{"type": "levels",
                      "params": {"brightness": 0.0, "contrast": 0.0}}]
