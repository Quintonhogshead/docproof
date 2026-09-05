"""docproof/cover/direction.py: the art-direction call (brief -> N distinct
Directions) and the revision call (spec + notes -> edited spec).

No network anywhere here — every provider is either tests.fakes.FakeProvider,
scripted with a canned structured reply, or a tiny local stand-in that raises
outright, standing in for "the call itself failed" (a network drop, an SDK
exception) as distinct from "the call answered with something unusable".
"""
from __future__ import annotations

import dataclasses

import pytest

from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.direction import (DIRECTION_MODEL, REVISION_MODEL,
                                      DirectionError, DirectionResult,
                                      RevisionError, RevisionResult,
                                      revise_spec, run_directions)
from docproof.cover.model import Brief, CoverSpec, Direction, build_spec
from docproof.providers import ProviderResult

from .fakes import FakeProvider, USAGE


def _brief(**overrides) -> Brief:
    data = dict(title="The Lighthouse at Gull Point", author="J. R. Vance",
               genre="literary")
    data.update(overrides)
    return Brief(**data)


def _palette_payload(**overrides) -> dict:
    data = dict(background="#101010", primary="#f5f1e8", accent="#c9a227",
               text="#f5f1e8", scrim="#000000")
    data.update(overrides)
    return data


def _direction_payload(**overrides) -> dict:
    """One concept, as the raw JSON dict a structured reply would carry —
    the shape FakeProvider's `parsed` field expects."""
    data = dict(
        concept_name="Ash and Brass",
        rationale="A brooding industrial-fantasy palette with warm brass accents.",
        archetype="probe_scene",
        palette=_palette_payload(),
        title_font="Playfair Display",
        author_font="Spectral",
        art_prompts={"background": "A smoky brass foundry at dusk, oil painting."},
        texture=True)
    data.update(overrides)
    return data


# archetype -> a schema-valid art_prompts dict for it, so the n-concept
# fixture below can cycle through all three shipped archetypes rather than
# accidentally testing only one.
_PROMPTS_FOR_ARCHETYPE = {
    "probe_typographic": {},
    "probe_scene": {"background": "A lonely lighthouse at dusk, oil painting."},
    "probe_sandwich": {"background": "A misty pine forest at dawn, gouache.",
                        "focal": "A cloaked figure walking away, cutout subject only."},
}


def _directions_payload(n: int) -> dict:
    cycle = list(_PROMPTS_FOR_ARCHETYPE)
    concepts = []
    for i in range(n):
        archetype = cycle[i % len(cycle)]
        concepts.append(_direction_payload(
            concept_name=f"Concept {i}", archetype=archetype,
            art_prompts=_PROMPTS_FOR_ARCHETYPE[archetype]))
    return {"concepts": concepts}


def _direction_obj(**overrides) -> Direction:
    return Direction(**_direction_payload(**overrides))


def _base_spec(archetype_name: str = "probe_scene",
              **direction_overrides) -> CoverSpec:
    archetype = ARCHETYPES[archetype_name]
    kwargs = {"archetype": archetype_name,
             "art_prompts": _PROMPTS_FOR_ARCHETYPE[archetype_name]}
    kwargs.update(direction_overrides)
    direction = _direction_obj(**kwargs)
    return build_spec(direction, _brief(), archetype)


def _with_asset(spec: CoverSpec, slot_id: str, asset: str) -> CoverSpec:
    new_art = [a.model_copy(update={"asset": asset}) if a.id == slot_id else a
              for a in spec.art]
    return spec.model_copy(update={"art": new_art})


def _index_of(slots, slot_id: str) -> int:
    """The position of the slot with this id in an art/text list. A
    SpecEdit.path addresses a slot by its POSITION, never its `id` (§6.2) —
    exactly like the model must, this looks the index up rather than
    hardcoding it, so these tests stay honest about the archetype's real
    slot order instead of assuming it."""
    return next(i for i, s in enumerate(slots) if s.id == slot_id)


def _edits_payload(*edits: dict) -> dict:
    """The raw JSON dict a SpecEdits structured reply carries — the shape
    FakeProvider's `parsed` field expects, mirroring _direction_payload's
    role for the direction-call tests above."""
    return {"edits": list(edits)}


class _ExplodingProvider:
    """Stands in for "the call itself failed" — a dropped connection, an SDK
    exception — as opposed to FakeProvider's "the call answered with
    something unusable". Neither run_directions nor revise_spec should ever
    let this escape unwrapped."""
    name = "fake-exploding"

    def complete_structured(self, **kwargs):
        raise RuntimeError("network is down")


# -- DirectionResult / RevisionResult: frozen dataclasses --------------------

def test_direction_result_is_frozen():
    result = DirectionResult(directions=[], model=DIRECTION_MODEL, cost=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.model = "other"


def test_revision_result_is_frozen():
    spec = _base_spec()
    result = RevisionResult(spec=spec, cost=None)
    assert result.skipped == ()             # default: nothing was skipped
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.cost = 1.0


# -- run_directions: happy path -----------------------------------------------

def test_run_directions_happy_path():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(4), usage=USAGE)])
    result = run_directions(_brief(), provider, n=4)

    assert isinstance(result, DirectionResult)
    assert len(result.directions) == 4
    assert all(isinstance(d, Direction) for d in result.directions)
    assert [d.concept_name for d in result.directions] == \
        ["Concept 0", "Concept 1", "Concept 2", "Concept 3"]
    assert result.model == DIRECTION_MODEL
    assert result.cost is not None and result.cost > 0

    call = provider.calls[0]
    assert call["schema_name"] == "cover_directions"
    assert call["max_tokens"] == 8000
    assert call["model"] == DIRECTION_MODEL


def test_run_directions_defaults_to_the_frontier_model_but_accepts_an_override():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(1), usage=USAGE)])
    result = run_directions(_brief(), provider, n=1)
    assert result.model == DIRECTION_MODEL == "claude-fable-5"

    overridden = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(1), usage=USAGE)])
    result = run_directions(_brief(), overridden, n=1, model="claude-opus-5")
    assert result.model == "claude-opus-5"
    assert overridden.calls[0]["model"] == "claude-opus-5"


# -- system/user prompt content (spec §6.1's verbatim requirements) ---------

def test_run_directions_system_prompt_states_the_art_prompt_rules_verbatim():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider, n=2)
    system = provider.calls[0]["system"]
    assert ("never ask for text, letters, numbers, typography, book "
           "covers, mockups, borders, or frames; never name a living "
           "artist; describe a scene, not a cover.") in system


def test_run_directions_system_prompt_states_the_symbolic_object_doctrine():
    # BRAIN v2.1 (owner's live-batch review, 2026-08-29): a literary cover
    # shipped a surreal blob object -- design-only revisions can't fix a
    # generation-time art defect, so the fix is to steer the PROMPT away
    # from surreal/composite objects in the first place.
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider, n=2)
    system = provider.calls[0]["system"]
    assert "symbolic-object slot" in system
    assert "ONE clean, instantly recognizable object" in system
    assert "a brass key, a single feather" in system
    assert "never a surreal composite, never anatomy on inanimate things" in system


def test_run_directions_system_prompt_prefers_illustrated_media_over_photoreal():
    # §6.3's one-line addition to the §6.1 prompt: stylized media hide
    # generation artifacts, so it should steer away from photorealism.
    # v2.2 wave: the flat "unless the brief asks for photography" escape
    # hatch was replaced with a conditional-on-treatment one — a
    # photographic prompt is permitted only when paired with the
    # photo_soft/duotone/silhouette treatment, never bare.
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider, n=2)
    system = provider.calls[0]["system"]
    assert "Prefer illustrated, painterly, or graphic media" in system
    assert 'permitted ONLY when paired with treatment: "photo_soft"' in system
    assert 'untreated (treatment: "none") photographic prompt is never allowed' in system


def test_run_directions_system_prompt_effects_rack_names_photo_soft():
    # The effects-rack enumeration paragraph must also name photo_soft
    # alongside the other four treatments, or a model reading only that
    # paragraph would never learn it exists.
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider, n=2)
    system = provider.calls[0]["system"]
    assert '"photo_soft"' in system
    assert "makes a photographic or photoreal prompt allowed at all" in system


# -- de-muting doctrine, image-simplicity hierarchy, container device -------
# (§6.1, folded in against the owner's beta verdict that concepts were too
# timid and imagery too complex/detailed for AI-generation to hide its own
# artifacts — 2026-08-29)

def test_run_directions_system_prompt_states_the_de_muting_palette_doctrine():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider, n=2)
    system = provider.calls[0]["system"]
    assert "FLAT and CONFIDENT" in system
    assert "2–4 colors per cover" in system
    assert "Tone-on-tone monochrome" in system
    assert "a signature move" in system
    assert "Muddy gray-on-gray" in system


def test_run_directions_system_prompt_states_the_dark_minimal_doctrine():
    # BRAIN v2.1 (owner's live-batch review, 2026-08-29): the judge passed a
    # thriller with a near-invisible dark-on-dark silhouette -- the fix
    # steers art direction away from an all-whisper palette in the first
    # place, verbatim per the owner's own pinned phrasing.
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider, n=2)
    system = provider.calls[0]["system"]
    assert ("a dark, quiet cover must still carry one high-voltage element"
           in system.lower())
    assert "all-values-within-a-whisper is a failure" in system


def test_run_directions_system_prompt_states_the_image_simplicity_hierarchy():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider, n=2)
    system = provider.calls[0]["system"]
    assert "Flat SILHOUETTE" in system
    assert "DUOTONE single subject" in system
    assert "ORNAMENT/PATTERN" in system
    assert "Stylized single-subject illustration" in system
    assert "A full illustrated scene" in system
    assert "never a sweeping or complex vista" in system
    assert "reaches past level 3" in system
    assert "SHAPE and GESTURE" in system


def test_run_directions_system_prompt_states_type_is_the_hero():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider, n=2)
    system = provider.calls[0]["system"]
    assert "Type is the hero of the cover" in system


def test_run_directions_system_prompt_states_the_container_device_doctrine():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider, n=2)
    system = provider.calls[0]["system"]
    assert "The container device" in system
    assert "lighthouse's beam" in system
    assert "a transparent cutout whose shape will clip the title" in system
    assert "Never describe or bake the payload" in system


def test_run_directions_system_prompt_names_the_effects_rack(): # §7.4a
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider, n=2)
    system = provider.calls[0]["system"]
    for effect in ("duotone", "silhouette", "posterize", "sticker", "corners",
                  "scatter", "knockout", "art_fill"):
        assert effect in system


def test_run_directions_system_prompt_effects_rack_says_treatment_is_the_only_field():
    # §7.4a: "the model sets ArtPrompt.treatment only — corners/scatter/
    # mask_from/mode stay archetype/revision territory (say so in the
    # prompt)."
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider, n=2)
    system = provider.calls[0]["system"]
    assert "`treatment` is the ONLY effects-rack field you ever set" in system
    assert "never invent or request them yourself" in system


def test_run_directions_system_prompt_enumerates_archetypes_and_fonts():
    # A genre that isn't one of the ten subject keys normalizes to "no
    # filter" (§5.3) — every archetype is enumerated, exactly the unfiltered
    # behavior this test predates the genre field by checking.
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(genre="upmarket fiction"), provider, n=2)
    system = provider.calls[0]["system"]
    for name in ARCHETYPES:
        assert name in system
    from docproof.cover.fonts import FAMILIES
    for family in FAMILIES:
        assert family in system


def test_run_directions_big_type_requirement_only_at_n_3_or_more():
    provider2 = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider2, n=2)
    assert "MUST use the big_type archetype" not in provider2.calls[0]["system"]

    provider3 = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(3), usage=USAGE)])
    run_directions(_brief(), provider3, n=3)
    assert "MUST use the big_type archetype" in provider3.calls[0]["system"]


# -- genre-filtered archetype enumeration (§5.3) ------------------------------

def test_run_directions_system_prompt_narrows_archetypes_to_the_briefs_genre():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(genre="historical"), provider, n=2)
    system = provider.calls[0]["system"]
    # asserted as the RULE, not as a remembered name: whatever is tagged
    # for this genre must appear, and whatever is tagged only for others
    # must not.
    for name, arch in ARCHETYPES.items():
        if arch.genres and "historical" in arch.genres:
            assert name in system
        elif arch.genres:
            assert name not in system
    for name in ("probe_typographic", "probe_scene", "probe_sandwich"):
        assert name in system                          # untagged: always shown
    # tagged for other genres only -- excluded from the narrowed enumeration
    assert "nonfiction_bold_colorblock_typographic" not in system
    assert "young_readers_character_illustration" not in system
    assert "scifi_geometric_object_minimal" not in system


def test_run_directions_system_prompt_unrecognized_genre_is_unfiltered():
    # Brief.genre is free text or one of the ten subject keys
    # (docproof.cover.model.Brief) -- a string that matches neither must
    # normalize to "no filter", the same as genre=None.
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(genre="a genre nobody has heard of"), provider, n=2)
    system = provider.calls[0]["system"]
    for name in ARCHETYPES:
        assert name in system


def test_run_directions_accepts_a_concept_naming_an_archetype_outside_the_narrowed_genre():
    # The enumeration shown to the model is narrowed to the brief's genre,
    # but validation still checks the FULL archetype set (§5.3: "validate
    # chosen archetypes against ALL of ARCHETYPES") -- a concept naming an
    # archetype the narrowed prompt didn't even list must not be rejected.
    # horror_dark_emblem_ornate (tagged horror/romance, not historical) is a
    # stronger case than an untagged archetype, which is always in scope
    # regardless of any narrowing.
    payload = {"concepts": [_direction_payload(
        archetype="romantasy_organic",
        art_prompts={"background": "A dark portrait study, oil painting."})]}
    provider = FakeProvider(results=[ProviderResult(parsed=payload, usage=USAGE)])
    result = run_directions(_brief(genre="historical"), provider, n=1)
    assert result.directions[0].archetype == "romantasy_organic"


def test_run_directions_manuscript_sample_section_present_only_when_passed():
    no_sample = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), no_sample, n=2)
    assert "MANUSCRIPT SAMPLE" not in no_sample.calls[0]["user"]
    assert "the manuscript's actual text" not in no_sample.calls[0]["system"]

    with_sample = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), with_sample, n=2,
                   manuscript_sample="OPENING SAMPLE:\nA ship sailed into the fog.")
    call = with_sample.calls[0]
    assert "MANUSCRIPT SAMPLE:\nOPENING SAMPLE:\nA ship sailed into the fog." \
        in call["user"]
    assert "the manuscript's actual text" in call["system"]
    assert "typed fields" in call["system"] or "typed brief fields" in \
        call["system"] or "ALWAYS win over the sample" in call["system"]


def test_run_directions_sample_rule_frames_the_sample_as_usually_a_reality_sheet():
    # docproof.cover.pipeline.run_job now distills the manuscript sample into
    # a reality sheet before this call ever runs (docproof.cover.reality);
    # the rule text has to name that, while still covering the rare
    # raw-sample fallback.
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(1), usage=USAGE)])
    run_directions(_brief(), provider, n=1,
                   manuscript_sample="Setting: a windswept lighthouse.")
    system = provider.calls[0]["system"]
    assert "REALITY SHEET" in system
    assert "palette cues" in system
    assert "motifs" in system
    # the pre-existing has_sample assertions still hold verbatim
    assert "the manuscript's actual text" in system
    assert "ALWAYS win over the sample" in system


def test_run_directions_user_prompt_labels_brief_fields_and_skips_empty_ones():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(1), usage=USAGE)])
    run_directions(_brief(mood="elegiac, wintry"), provider, n=1)
    user = provider.calls[0]["user"]
    assert "Title: The Lighthouse at Gull Point" in user
    assert "Author: J. R. Vance" in user
    assert "Genre: literary" in user
    assert "Mood: elegiac, wintry" in user
    assert "Pitch:" not in user          # brief.pitch is "" by default
    assert "Subtitle:" not in user


# -- n enforced ----------------------------------------------------------------

def test_run_directions_enforces_that_the_reply_matches_n():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(3), usage=USAGE)])
    with pytest.raises(DirectionError, match="for 4 cover concepts but got 3"):
        run_directions(_brief(), provider, n=4)


# -- bad concept -> DirectionError -------------------------------------------

def test_run_directions_rejects_a_bad_archetype_key():
    payload = {"concepts": [_direction_payload(archetype="not_a_real_archetype")]}
    provider = FakeProvider(results=[ProviderResult(parsed=payload, usage=USAGE)])
    with pytest.raises(DirectionError, match="not_a_real_archetype"):
        run_directions(_brief(), provider, n=1)


def test_run_directions_drops_an_art_prompt_for_a_non_generatable_slot():
    # full_bleed_art's texture slot exists but is procedural (generatable is
    # false). A surplus prompt for it used to be fatal; a live run showed
    # that killing a whole multi-concept job over one stray extra prompt is
    # the wrong trade (the entry is never read by build_spec anyway), so it
    # is now dropped -- the concept survives with only its generatable
    # prompts, and only a fabricated ARCHETYPE stays fatal.
    payload = {"concepts": [_direction_payload(
        archetype="probe_scene",
        art_prompts={"background": "a moody coastal scene, oil painting",
                     "texture": "a grainy film overlay"})]}
    provider = FakeProvider(results=[ProviderResult(parsed=payload, usage=USAGE)])
    result = run_directions(_brief(), provider, n=1)
    slots = {p.slot for p in result.directions[0].art_prompts}
    assert slots == {"background"}


def test_run_directions_accepts_a_well_formed_cutout_sandwich_concept():
    payload = {"concepts": [_direction_payload(
        archetype="probe_sandwich",
        art_prompts=_PROMPTS_FOR_ARCHETYPE["probe_sandwich"])]}
    provider = FakeProvider(results=[ProviderResult(parsed=payload, usage=USAGE)])
    result = run_directions(_brief(), provider, n=1)
    assert result.directions[0].archetype == "probe_sandwich"


def test_run_directions_accepts_an_art_prompts_treatment_from_the_model(): # §7.4a
    # art_prompts also accepts the LIST-of-objects wire shape directly (not
    # just the {slot: prompt} dict convenience form _direction_payload uses
    # elsewhere) — this is the only shape that can carry `treatment` at all.
    payload = {"concepts": [_direction_payload(
        art_prompts=[{"slot": "background", "prompt": "A lonely lighthouse.",
                     "treatment": "duotone"}])]}
    provider = FakeProvider(results=[ProviderResult(parsed=payload, usage=USAGE)])
    result = run_directions(_brief(), provider, n=1)
    assert result.directions[0].art_prompts[0].treatment == "duotone"


# -- failure paths: no fallback, always DirectionError ------------------------

def test_run_directions_raises_on_schema_mismatch():
    provider = FakeProvider(
        results=[ProviderResult(parsed={"nope": 1}, usage=USAGE)])
    with pytest.raises(DirectionError, match="schema"):
        run_directions(_brief(), provider, n=4)


def test_run_directions_raises_when_the_model_returns_no_usable_result():
    provider = FakeProvider(results=[ProviderResult(
        stop_reason="max_tokens", error="output truncated", parsed=None)])
    with pytest.raises(DirectionError, match="truncated"):
        run_directions(_brief(), provider, n=4)


def test_run_directions_raises_when_the_provider_call_itself_fails():
    with pytest.raises(DirectionError, match="network is down"):
        run_directions(_brief(), _ExplodingProvider(), n=4)


# -- revise_spec: happy path (spec §6.2 -- patch edits, not a full echo) -----

def test_revise_spec_happy_path_applies_edits_bumps_version_and_appends_notes():
    original = _with_asset(_base_spec(), "background", "assets/c0_background.png")
    title_i = _index_of(original.text, "title")
    provider = FakeProvider(results=[ProviderResult(parsed=_edits_payload(
        {"path": "palette.primary", "value": "\"#a83250\""},
        {"path": f"text[{title_i}].zone.y", "value": "0.57"},
        {"path": f"text[{title_i}].size_max", "value": "0.13"},
    ), usage=USAGE)])

    result = revise_spec(original, "brighten it and make the title bigger",
                         provider)

    assert isinstance(result, RevisionResult)
    assert result.spec.version == 2
    assert result.spec.notes_log == ["brighten it and make the title bigger"]
    assert result.spec.palette.primary == "#a83250"
    assert result.spec.text[title_i].zone.y == 0.57
    assert result.spec.text[title_i].size_max == 0.13
    assert result.skipped == ()
    assert result.cost is not None
    # an untouched slot's asset rides straight through
    background = next(a for a in result.spec.art if a.id == "background")
    assert background.asset == "assets/c0_background.png"
    # the input spec itself is never mutated
    assert original.version == 1
    assert original.notes_log == []

    call = provider.calls[0]
    assert call["schema_name"] == "cover_revision"
    assert "brighten it and make the title bigger" in call["user"]


def test_revise_spec_can_append_to_a_list():
    original = _base_spec()   # full_bleed_art: 2 scrims
    n = len(original.scrims)
    provider = FakeProvider(results=[ProviderResult(
        parsed=_edits_payload({"path": f"scrims[{n}]", "value": "{}"}),
        usage=USAGE)])

    result = revise_spec(original, "add a third scrim", provider)

    assert len(result.spec.scrims) == n + 1
    assert result.skipped == ()


def test_revise_spec_defaults_to_the_workhorse_model_but_accepts_an_override():
    original = _base_spec()
    provider = FakeProvider(
        results=[ProviderResult(parsed=_edits_payload(), usage=USAGE)])
    result = revise_spec(original, "notes", provider)
    assert provider.calls[0]["model"] == REVISION_MODEL == "claude-sonnet-5"
    assert result.spec.version == 2

    overridden = FakeProvider(
        results=[ProviderResult(parsed=_edits_payload(), usage=USAGE)])
    result = revise_spec(original, "notes", overridden, model="claude-opus-5")
    assert overridden.calls[0]["model"] == "claude-opus-5"
    assert result.spec.version == 2


# -- revise_spec: system prompt content (spec §6.2's verbatim requirements) --

def test_revise_spec_system_prompt_documents_the_path_syntax_and_worked_examples():
    original = _base_spec()
    provider = FakeProvider(
        results=[ProviderResult(parsed=_edits_payload(), usage=USAGE)])
    revise_spec(original, "notes", provider)
    system = provider.calls[0]["system"]
    assert "PATCH EDITS" in system
    assert "`palette.primary`" in system
    assert "`text[1].zone.y`" in system
    assert "`art[0].prompt`" in system
    assert "Move the title zone up" in system
    assert "Recolor the palette" in system
    assert "Resize the title's type" in system


def test_revise_spec_system_prompt_states_the_may_not_rules():
    original = _base_spec()
    provider = FakeProvider(
        results=[ProviderResult(parsed=_edits_payload(), usage=USAGE)])
    revise_spec(original, "notes", provider)
    system = provider.calls[0]["system"]
    assert "write to `version` or `notes_log`" in system
    assert "touch an art slot's `asset` field" in system
    assert "invent a new art, scrim, or text slot" in system
    assert "out of scope for a revision" in system


def test_revise_spec_system_prompt_allows_mask_from_and_container_adjustments():
    # The container device (§6.1) is realized via TextSlot.mask_from /
    # ArtSlot placement-and-scale, so a revision has to be explicitly
    # allowed to touch them, the same way it's allowed to touch any other
    # design field.
    original = _base_spec()
    provider = FakeProvider(
        results=[ProviderResult(parsed=_edits_payload(), usage=USAGE)])
    revise_spec(original, "notes", provider)
    system = provider.calls[0]["system"]
    assert "mask_from" in system
    assert "container art slot" in system


# -- revise_spec: invalid edits are skipped, not fatal ------------------------

def test_revise_spec_skips_guarded_paths_and_records_them():
    original = _with_asset(_base_spec(), "background", "assets/c0_background.png")
    bg_i = _index_of(original.art, "background")
    provider = FakeProvider(results=[ProviderResult(parsed=_edits_payload(
        {"path": "version", "value": "99"},
        {"path": "notes_log", "value": "[\"hacked\"]"},
        {"path": f"art[{bg_i}].asset", "value": "\"sneaky.png\""},
    ), usage=USAGE)])

    result = revise_spec(original, "try to sneak past the guard", provider)

    assert result.spec.version == 2                                  # normal bump, not 99
    assert result.spec.notes_log == ["try to sneak past the guard"]   # not "hacked"
    background = next(a for a in result.spec.art if a.id == "background")
    assert background.asset == "assets/c0_background.png"    # the sneaky value never landed
    assert len(result.skipped) == 3
    assert all("guarded" in reason for reason in result.skipped)


def test_revise_spec_skips_an_unresolvable_path():
    original = _base_spec()
    provider = FakeProvider(results=[ProviderResult(
        parsed=_edits_payload({"path": "text[99].zone.y", "value": "0.1"}),
        usage=USAGE)])

    result = revise_spec(original, "notes", provider)

    assert len(result.skipped) == 1
    assert "text[99].zone.y" in result.skipped[0]
    assert result.spec.model_dump(exclude={"version", "notes_log"}) == \
        original.model_dump(exclude={"version", "notes_log"})


def test_revise_spec_all_edits_invalid_is_a_no_op():
    original = _base_spec()
    provider = FakeProvider(results=[ProviderResult(parsed=_edits_payload(
        {"path": "version", "value": "5"},
        {"path": "not a valid path!!", "value": "1"},
        {"path": "palette.nonexistent", "value": "1"},
    ), usage=USAGE)])

    result = revise_spec(original, "notes that land nowhere", provider)

    assert len(result.skipped) == 3
    assert result.spec.version == 2       # the bookkeeping still runs
    assert result.spec.model_dump(exclude={"version", "notes_log"}) == \
        original.model_dump(exclude={"version", "notes_log"})


def test_revise_spec_zero_edits_is_a_no_op():
    original = _base_spec()
    provider = FakeProvider(
        results=[ProviderResult(parsed=_edits_payload(), usage=USAGE)])

    result = revise_spec(original, "nothing to change", provider)

    assert result.spec.version == 2
    assert result.spec.notes_log == ["nothing to change"]
    assert result.skipped == ()
    assert result.spec.model_dump(exclude={"version", "notes_log"}) == \
        original.model_dump(exclude={"version", "notes_log"})


def test_revise_spec_bad_value_type_raises_and_leaves_the_prior_spec_untouched():
    # "42" parses as valid JSON (an int), so the edit APPLIES -- it only
    # fails once the patched dict is validated as a whole CoverSpec, since
    # palette.primary must be a #rrggbb hex string.
    original = _base_spec()
    provider = FakeProvider(results=[ProviderResult(
        parsed=_edits_payload({"path": "palette.primary", "value": "42"}),
        usage=USAGE)])

    with pytest.raises(RevisionError, match="schema"):
        revise_spec(original, "notes", provider)
    assert original.version == 1
    assert original.palette.primary != "42"


# -- revise_spec: art-asset diffing (the regen signal) ------------------------

def test_revise_spec_prompt_change_edit_still_clears_the_asset():
    original = _with_asset(_base_spec(), "background", "assets/c0_background.png")
    bg_i = _index_of(original.art, "background")
    provider = FakeProvider(results=[ProviderResult(parsed=_edits_payload(
        {"path": f"art[{bg_i}].prompt",
         "value": "\"A lighthouse at night under a full moon, oil painting.\""},
    ), usage=USAGE)])

    result = revise_spec(original, "different imagery -- a lighthouse at night",
                         provider)

    background = next(a for a in result.spec.art if a.id == "background")
    assert background.prompt == \
        "A lighthouse at night under a full moon, oil painting."
    assert background.asset == ""


def test_revise_spec_leaves_other_art_slots_asset_untouched():
    original = _base_spec()   # full_bleed_art: background + texture
    original = _with_asset(original, "background", "assets/c0_background.png")
    original = _with_asset(original, "texture", "assets/c0_texture.png")
    bg_i = _index_of(original.art, "background")
    provider = FakeProvider(results=[ProviderResult(parsed=_edits_payload(
        {"path": f"art[{bg_i}].prompt",
         "value": "\"Something completely different, gouache.\""},
    ), usage=USAGE)])

    result = revise_spec(original, "new background art", provider)

    by_id = {a.id: a for a in result.spec.art}
    assert by_id["background"].asset == ""                        # cleared
    assert by_id["texture"].asset == "assets/c0_texture.png"       # preserved


def test_revise_spec_clears_asset_when_only_transparent_changes():
    original = _base_spec(archetype_name="probe_sandwich")
    original = _with_asset(original, "focal", "assets/c0_focal.png")
    focal = next(a for a in original.art if a.id == "focal")
    assert focal.transparent is True    # cutout_sandwich's archetype default
    focal_i = _index_of(original.art, "focal")

    provider = FakeProvider(results=[ProviderResult(parsed=_edits_payload(
        {"path": f"art[{focal_i}].transparent", "value": "false"},
    ), usage=USAGE)])
    result = revise_spec(
        original, "make the focal figure opaque, not a cutout", provider)

    result_focal = next(a for a in result.spec.art if a.id == "focal")
    assert result_focal.transparent is False
    assert result_focal.asset == ""


def test_revise_spec_clears_asset_for_a_slot_the_input_never_had():
    # Defensive: the model is told never to invent a new slot, but appending
    # one is still mechanically legal (append is a generic, uniform list
    # operation -- see _apply_edit) -- the asset-diffing code should not
    # choke (or, worse, leak a stray asset path) if it happens anyway --
    # there's no prior value to compare against, so it reads as changed.
    original = _base_spec(archetype_name="probe_scene")
    # fx_grain rides in from the archetype's default cinematic_duotone
    # recipe (PR4 retrofit) — one more real art slot like any other here.
    assert {a.id for a in original.art} == {"background", "texture", "fx_grain"}
    n = len(original.art)
    provider = FakeProvider(results=[ProviderResult(parsed=_edits_payload(
        {"path": f"art[{n}]",
         "value": ('{"id": "focal", "prompt": "should not exist", '
                   '"asset": "sneaky-preexisting-path.png"}')},
    ), usage=USAGE)])

    result = revise_spec(original, "notes that never asked for this", provider)

    focal = next(a for a in result.spec.art if a.id == "focal")
    assert focal.asset == ""


# -- revise_spec: failure paths -----------------------------------------------

def test_revise_spec_raises_on_schema_mismatch_and_keeps_the_old_spec():
    original = _base_spec()
    provider = FakeProvider(
        results=[ProviderResult(parsed={"nope": 1}, usage=USAGE)])
    with pytest.raises(RevisionError, match="schema"):
        revise_spec(original, "notes", provider)
    assert original.version == 1


def test_revise_spec_raises_when_the_model_returns_no_usable_result():
    original = _base_spec()
    provider = FakeProvider(results=[ProviderResult(
        stop_reason="error", error="the model refused", parsed=None)])
    with pytest.raises(RevisionError, match="the model refused"):
        revise_spec(original, "notes", provider)
    assert original.version == 1


def test_revise_spec_raises_when_the_provider_call_itself_fails():
    original = _base_spec()
    with pytest.raises(RevisionError, match="network is down"):
        revise_spec(original, "notes", _ExplodingProvider())
    assert original.version == 1


# ===========================================================================
# Deep-stack wave, PR6 (§15.14): direction vocabulary — recipes, type_move,
# mask_intent — plus the §6.2 worked examples and the build_spec mappings.
# ===========================================================================

from docproof.cover.archetypes import (Archetype, ArchetypeArt, ArchetypeText,
                                       ArchetypeZone)
from docproof.cover.model import TextSlot
from docproof.cover.recipes import describe_recipes

# The §15.12 typeset fields land in a parallel PR of this same wave;
# build_spec's type_move mapping feature-detects them and degrades a
# requested move to a logged no-op until they exist. The mapping tests
# below assert the mapped VALUES, so they skip (and self-activate) on the
# same detection.
_HAS_TYPE_MOVE_FIELDS = {"fit_mode", "arc", "rotate", "emphasis"} <= set(
    TextSlot.model_fields)
_needs_type_move_fields = pytest.mark.skipif(
    not _HAS_TYPE_MOVE_FIELDS,
    reason="TextSlot's §15.12 fields (fit_mode/arc/rotate/emphasis) have "
           "not landed yet — build_spec degrades the move to a logged "
           "no-op until they do")


def _direction_system() -> str:
    provider = FakeProvider(
        results=[ProviderResult(parsed=_directions_payload(2), usage=USAGE)])
    run_directions(_brief(), provider, n=2)
    return provider.calls[0]["system"]


# -- §6.1 system prompt: the recipe roster ------------------------------------

def test_run_directions_system_prompt_enumerates_the_recipe_roster():
    system = _direction_system()
    # The whole shelf, exactly as describe_recipes() renders it (§15.14:
    # "enumerate recipes with describe lines").
    assert describe_recipes() in system
    for name in ("quiet_literary", "cinematic_duotone", "vintage_matte"):
        assert name in system


def test_run_directions_system_prompt_gives_recipe_guidance():
    system = _direction_system()
    assert "Pick the recipe whose look matches this genre's shelf" in system
    assert "big_type usually wants quiet_literary or nothing" in system
    # "" defers to the archetype's own default; a named pick wins.
    assert "lets that default apply" in system
    assert "a named pick always wins over it" in system


# -- §6.1 system prompt: the role-grouped font roster -------------------------

def test_run_directions_system_prompt_font_roster_is_role_grouped():
    system = _direction_system()
    # describe_fonts() is role-grouped (§15.11) and the prompt both uses it
    # verbatim and tells the model to think in roles and pairings.
    from docproof.cover.fonts import describe_fonts
    assert describe_fonts() in system
    assert "grouped by ROLE" in system
    assert "pairs with" in system


# -- §6.1 system prompt: the type_move vocabulary -----------------------------

def test_run_directions_system_prompt_states_the_type_move_vocabulary():
    system = _direction_system()
    assert "`type_move`" in system
    for move in ('"justify_stack"', '"arch"', '"tilt"', '"emphasis"'):
        assert move in system
    assert "`emphasis_word`" in system


def test_run_directions_system_prompt_states_the_one_move_rule():
    system = _direction_system()
    assert "ONE signature typography move" in system
    assert "one move per concept is a hard rule" in system
    # And the one still-expressible two-move combination is forbidden too.
    assert "never as a second move" in system


# -- §6.1 system prompt: the four masking moves -------------------------------

def test_run_directions_system_prompt_names_the_four_masking_moves():
    system = _direction_system()
    for move in ("PLATE-BLEND", "THING-IN-TEXT", "TEXT-IN-THING",
                 "REGION-GRADE"):
        assert move in system
    # each with a when-it-earns-its-place sentence
    assert system.count("earns its place") >= 4


def test_run_directions_system_prompt_maps_the_mask_intent_vocabulary():
    system = _direction_system()
    assert "`mask_intent`" in system
    for intent in ('"blend_into_background"', '"inside_title"',
                   '"inside_focal"'):
        assert intent in system


def test_run_directions_system_prompt_requires_transparent_cutouts_for_inside_intents():
    system = _direction_system()
    assert "Required with any inside_* intent" in system
    assert "TRANSPARENT-BACKGROUND cutout" in system


def test_run_directions_system_prompt_states_the_direction_time_vocabulary_boundary():
    # §15.14: the model never sets adjust-layer/effect fields directly at
    # direction time — recipes, type_move, and mask_intent are its whole
    # vocabulary there.
    system = _direction_system()
    assert ("`recipe`, `type_move`, and `mask_intent`" in system)
    assert "WHOLE design-machinery vocabulary" in system
    assert ("never set adjust-layer, mask, or effect fields directly at "
            "direction time") in system


# -- the wire schema accepts the new vocabulary -------------------------------

def test_run_directions_accepts_recipe_type_move_and_mask_intent_on_the_wire():
    payload = {"concepts": [_direction_payload(
        recipe="quiet_literary", type_move="arch",
        art_prompts=[{"slot": "background", "prompt": "A lonely lighthouse.",
                      "mask_intent": "blend_into_background"}])]}
    provider = FakeProvider(results=[ProviderResult(parsed=payload, usage=USAGE)])
    result = run_directions(_brief(), provider, n=1)
    direction = result.directions[0]
    assert direction.recipe == "quiet_literary"
    assert direction.type_move == "arch"
    assert direction.art_prompts[0].mask_intent == "blend_into_background"


# -- build_spec: mask_intent mappings (§15.13 part 2) -------------------------

def _spec_with_intent(archetype_name: str, slot: str, intent: str,
                      archetype=None):
    direction = _direction_obj(
        archetype=archetype_name,
        art_prompts=[{"slot": slot, "prompt": "A quiet scene, gouache.",
                      "mask_intent": intent}])
    return build_spec(direction, _brief(),
                      archetype if archetype is not None
                      else ARCHETYPES[archetype_name])


def test_build_spec_blend_into_background_becomes_a_linear_gradient_mask():
    spec = _spec_with_intent("probe_scene", "background",
                             "blend_into_background")
    background = next(a for a in spec.art if a.id == "background")
    assert background.mask is not None
    assert background.mask.gradient is not None
    assert background.mask.gradient.kind == "linear"
    assert background.mask.from_layer == "" and background.mask.from_text == ""


def test_build_spec_inside_title_becomes_mask_from_text_title():
    spec = _spec_with_intent("probe_sandwich", "focal", "inside_title")
    focal = next(a for a in spec.art if a.id == "focal")
    assert focal.mask is not None
    assert focal.mask.from_text == "title"


def _focal_pair_archetype(overlay_after_focal: bool = True) -> Archetype:
    """A synthetic archetype with a `focal` cutout and a generatable
    `overlay` slot — drawn after focal by default (the inside_focal happy
    path), or before it to exercise the ordering drop."""
    order = (["background", "focal", "overlay", "title", "author"]
             if overlay_after_focal
             else ["background", "overlay", "focal", "title", "author"])
    return Archetype(
        name="synthetic_focal", describe="d", composition_note="c",
        art=[ArchetypeArt(id="background", generatable=False),
             ArchetypeArt(id="focal", generatable=True, fit="contain",
                          transparent=True),
             ArchetypeArt(id="overlay", generatable=True, fit="contain",
                          transparent=True)],
        text=[ArchetypeText(id="title",
                            zone=ArchetypeZone(x=0.1, y=0.4, w=0.8, h=0.2),
                            size_min=0.02, size_max=0.1),
              ArchetypeText(id="author",
                            zone=ArchetypeZone(x=0.1, y=0.9, w=0.8, h=0.05),
                            size_min=0.015, size_max=0.03)],
        layers=order)


def test_build_spec_inside_focal_clips_to_the_archetypes_focal_slot():
    archetype = _focal_pair_archetype()
    spec = _spec_with_intent("synthetic_focal", "overlay", "inside_focal",
                             archetype=archetype)
    overlay = next(a for a in spec.art if a.id == "overlay")
    assert overlay.mask is not None
    assert overlay.mask.from_layer == "focal"


def test_build_spec_inside_focal_dropped_when_the_archetype_has_no_focal(caplog):
    # full_bleed_art has no focal slot at all — the intent is dropped with
    # a log line (the §6.1 surplus-prompt precedent), never fatal.
    with caplog.at_level("INFO", logger="docproof.cover.model"):
        spec = _spec_with_intent("probe_scene", "background",
                                 "inside_focal")
    background = next(a for a in spec.art if a.id == "background")
    assert background.mask is None
    assert any("inside_focal" in r.message and "dropped" in r.message
               for r in caplog.records)


def test_build_spec_inside_focal_dropped_when_focal_draws_after_the_slot(caplog):
    # A mask can only clip to pixels already positioned (CoverSpec's
    # from_layer ordering rule) — an intent the archetype's own layer
    # order cannot honor is dropped, not built into an invalid spec.
    archetype = _focal_pair_archetype(overlay_after_focal=False)
    with caplog.at_level("INFO", logger="docproof.cover.model"):
        spec = _spec_with_intent("synthetic_focal", "overlay", "inside_focal",
                                 archetype=archetype)
    overlay = next(a for a in spec.art if a.id == "overlay")
    assert overlay.mask is None
    assert any("inside_focal" in r.message for r in caplog.records)


def test_build_spec_intent_dropped_on_a_slot_the_archetype_already_clips(caplog):
    # woven_emblem-style precedence in miniature: a slot the archetype
    # already masks (legacy mask_from sugar here) keeps the template's own
    # clip; the direction's intent is dropped with a log line.
    archetype = Archetype(
        name="synthetic_clipped", describe="d", composition_note="c",
        art=[ArchetypeArt(id="base", generatable=False),
             ArchetypeArt(id="over", generatable=True, mask_from="base")],
        text=[ArchetypeText(id="title",
                            zone=ArchetypeZone(x=0.1, y=0.4, w=0.8, h=0.2),
                            size_min=0.02, size_max=0.1)],
        layers=["base", "over", "title"])
    with caplog.at_level("INFO", logger="docproof.cover.model"):
        spec = _spec_with_intent("synthetic_clipped", "over",
                                 "blend_into_background", archetype=archetype)
    over = next(a for a in spec.art if a.id == "over")
    # the legacy sugar folds to from_layer — the template's clip, not the
    # intent's gradient
    assert over.mask is not None and over.mask.from_layer == "base"
    assert over.mask.gradient is None
    assert any("already clips" in r.message for r in caplog.records)


# -- build_spec: type_move mappings (§15.12) ----------------------------------

def _title_of(spec: CoverSpec):
    return next(t for t in spec.text if t.id == "title")


def _spec_with_move(**direction_overrides) -> CoverSpec:
    return _base_spec("probe_scene", **direction_overrides)


@_needs_type_move_fields
def test_build_spec_justify_stack_maps_to_the_title_fit_mode():
    title = _title_of(_spec_with_move(type_move="justify_stack"))
    assert title.fit_mode == "justify_stack"


@_needs_type_move_fields
def test_build_spec_arch_maps_to_arc_018():
    title = _title_of(_spec_with_move(type_move="arch"))
    assert title.arc == 0.18


@_needs_type_move_fields
def test_build_spec_tilt_maps_to_rotate_minus_6():
    title = _title_of(_spec_with_move(type_move="tilt"))
    assert title.rotate == -6.0


@_needs_type_move_fields
def test_build_spec_emphasis_resolves_the_word_case_insensitively():
    # brief title: "The Lighthouse at Gull Point" — word index 1.
    title = _title_of(_spec_with_move(type_move="emphasis",
                                      emphasis_word="lighthouse"))
    assert title.emphasis == [1]
    assert title.emphasis_style == "accent_color"


@_needs_type_move_fields
def test_build_spec_emphasis_word_absent_from_title_is_dropped(caplog):
    with caplog.at_level("INFO", logger="docproof.cover.model"):
        title = _title_of(_spec_with_move(type_move="emphasis",
                                          emphasis_word="kraken"))
    assert title.emphasis == []
    assert any("kraken" in r.message for r in caplog.records)


@_needs_type_move_fields
def test_build_spec_one_move_rule_ignores_a_stray_emphasis_word(caplog):
    # The closed Literal already makes two type_move values inexpressible;
    # the ONE still-expressible two-move request — emphasis_word riding
    # alongside a different move — applies the named move and drops the
    # stray word with a log line.
    with caplog.at_level("INFO", logger="docproof.cover.model"):
        spec = _spec_with_move(type_move="arch", emphasis_word="lighthouse")
    title = _title_of(spec)
    assert title.arc == 0.18
    assert title.emphasis == []
    assert any("one" in r.message.lower() and "move" in r.message.lower()
               for r in caplog.records)


@_needs_type_move_fields
def test_build_spec_type_move_never_touches_non_title_slots():
    spec = _spec_with_move(type_move="tilt")
    for slot in spec.text:
        if slot.id == "title":
            continue
        assert slot.rotate == 0.0
        assert slot.fit_mode == "uniform"


def test_build_spec_with_a_type_move_never_crashes_even_without_the_fields():
    # The degrade path (and, once the §15.12 fields land, the mapped path):
    # a direction requesting a move must always build a valid spec.
    spec = _spec_with_move(type_move="justify_stack")
    assert _title_of(spec) is not None


# -- §6.2 revision prompt: the four §15.14 worked examples --------------------

def test_revise_spec_system_prompt_carries_the_four_deep_stack_worked_examples():
    original = _base_spec()
    provider = FakeProvider(
        results=[ProviderResult(parsed=_edits_payload(), usage=USAGE)])
    revise_spec(original, "notes", provider)
    system = provider.calls[0]["system"]
    # warmer and moodier -> adjust temperature + vignette strength
    assert '"warmer and moodier"' in system
    assert "`adjust[0].temperature`" in system
    assert "vignette" in system
    # type feels pasted on -> whole-list layers replace, fx_ above text
    assert "the type feels pasted on" in system
    assert "`fx_`-prefixed" in system
    assert "path `layers`" in system
    # stacked poster title -> text[0].fit_mode
    assert '"make the title a stacked poster title"' in system
    assert "`text[0].fit_mode`" in system
    assert '"justify_stack"' in system
    # put the forest inside the title -> art[k].mask.from_text
    assert '"put the forest inside the title"' in system
    assert '{"from_text": "title"}' in system
    assert "`art[1].mask.from_text`" in system
    # the original three examples survive verbatim alongside them
    assert "Move the title zone up" in system
    assert "Recolor the palette" in system
    assert "Resize the title's type" in system


def test_revise_spec_system_prompt_reaches_the_new_fields_in_the_may_list():
    # §15.14: "the patch grammar reaches every new field" — the may-list
    # names the adjust/mask/expressive-type vocabulary so the model knows
    # it is allowed to touch what the examples demonstrate.
    original = _base_spec()
    provider = FakeProvider(
        results=[ProviderResult(parsed=_edits_payload(), usage=USAGE)])
    revise_spec(original, "notes", provider)
    system = provider.calls[0]["system"]
    assert "adjust layer's grade and strength fields" in system
    assert "reorder `layers`" in system
    assert "set or edit a layer's `mask`" in system
    assert "fit_mode, arc, rotate, emphasis" in system
