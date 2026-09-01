#!/usr/bin/env python
"""Compose one Cover Studio concept from a hand-authored Direction.

Cover Studio normally asks an Anthropic model to invent each Direction. This
runner skips that call: YOU (the agent) are the art director, so you write the
Direction JSON yourself and this only does the deterministic half -- build the
spec, generate the art slots with gpt-image-2, compose, and print the render
report. No Anthropic credentials are involved; only the image calls cost money.

    cover_direct.py build   --brief b.json --direction d.json --out DIR
    cover_direct.py compose --out DIR            # recompose, $0, no new art
    cover_direct.py list                         # archetypes + fonts + recipes

`build` writes DIR/spec.json, DIR/assets/*.png and DIR/*.png|jpg. Once the art
exists, `compose` re-renders from an EDITED DIR/spec.json for free -- that is
the loop to iterate typography, zones, scrims and recipes in. Only re-run
`build` (or delete an asset) when the pixels themselves must change.

Direction JSON keys: concept_name, rationale, archetype, palette
{background, primary, accent, text, scrim}, title_font, author_font,
art_prompts [{slot, prompt, treatment}], texture, recipe, type_move,
emphasis_word. Run `list` to see every legal value.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.settings import get_api_key
from docproof.cover.archetypes import ARCHETYPES, describe_archetypes
from docproof.cover.compose import compose
from docproof.cover.imaging import IMAGE_COST, generate, has_real_alpha, make_client
from docproof.cover.model import (ART_SLOT_IDS, ART_TREATMENTS, RECIPES,
                                  Brief, Direction, build_spec)
from docproof.cover.fonts import FAMILIES
from docproof.cover.pipeline import save_renders

DRAFT = "1K"


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _spec_path(out: Path) -> Path:
    return out / "spec.json"


def _render(spec, out: Path) -> None:
    image, report = compose(spec, out)
    renders = save_renders(image, out, spec.version, 0)
    _spec_path(out).write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    print("\n-- render report --")
    print(f"contrast      {report.contrast}")
    print(f"scrim_final   {report.scrim_final}")
    print(f"fitted_sizes  {report.fitted_sizes}")
    print(f"occlusion     {report.occlusion}")
    print(f"dead_band     {report.dead_band_frac:.3f}")
    if report.adjustments:
        print("AUTOPILOT     " + "; ".join(report.adjustments))
    if report.warnings:
        print("WARNINGS      " + "; ".join(report.warnings))
    for r in renders:
        print(f"render        {out / r}")


def cmd_list() -> int:
    print(describe_archetypes())
    print("\n-- art slot ids --\n" + ", ".join(sorted(ART_SLOT_IDS)))
    print("\n-- treatments --\n" + ", ".join(sorted(ART_TREATMENTS)))
    print("\n-- recipes --\n" + ", ".join(sorted(RECIPES)))
    print("\n-- fonts --\n" + ", ".join(sorted(FAMILIES)))
    return 0


def cmd_build(args) -> int:
    out = Path(args.out)
    (out / "assets").mkdir(parents=True, exist_ok=True)
    brief = Brief(**_load(args.brief))
    direction = Direction(**_load(args.direction))
    archetype = ARCHETYPES.get(direction.archetype)
    if archetype is None:
        sys.exit(f"no such archetype: {direction.archetype!r} "
                 f"(run `cover_direct.py list`)")
    spec = build_spec(direction, brief, archetype)

    tier = "2K" if args.full else DRAFT
    key = get_api_key("openai")
    if not key:
        sys.exit("No OpenAI key: set OPENAI_API_KEY.")
    client = make_client(key)

    # Mirror pipeline._generate_art_slot: the archetype's composition note and
    # the fixed negative suffix are what keep text out of the art, so borrow
    # the pipeline's own assembler rather than sending the bare prompt.
    from docproof.cover.pipeline import _assemble_prompt

    spend = 0.0
    for slot in spec.art:
        if not slot.prompt or slot.asset:
            continue
        rel = f"assets/{slot.id}.png"
        dest = out / rel
        if dest.exists() and not args.repaint:
            slot.asset = rel
            print(f"art  {slot.id}: reusing {dest}")
            continue
        prompt = _assemble_prompt(slot, archetype)
        print(f"art  {slot.id} ({tier}, transparent={slot.transparent}): "
              f"{prompt[:160]}...", flush=True)
        png = generate(client, prompt, transparent=slot.transparent,
                       resolution=tier)
        dest.write_bytes(png)
        slot.asset = rel
        spend += IMAGE_COST[tier]
        if slot.transparent and not has_real_alpha(png):
            print(f"     NOTE: {slot.id} came back opaque despite the cutout "
                  f"request; compose will fall back to the §5.2.3 layer order.")

    print(f"\nart spend ${spend:.2f}")
    _render(spec, out)
    return 0


def cmd_compose(args) -> int:
    out = Path(args.out)
    from docproof.cover.model import CoverSpec
    spec = CoverSpec(**_load(str(_spec_path(out))))
    _render(spec, out)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("build")
    p.add_argument("--brief", required=True)
    p.add_argument("--direction", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--full", action="store_true", help="2K art, ~$0.05/image")
    p.add_argument("--repaint", action="store_true",
                   help="regenerate art even where an asset already exists")
    p = sub.add_parser("compose")
    p.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "build":
        return cmd_build(args)
    return cmd_compose(args)


if __name__ == "__main__":
    raise SystemExit(main())
