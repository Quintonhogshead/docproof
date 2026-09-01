#!/usr/bin/env python
"""Pack `cover_direct.py` output directories into one Cover Studio job.

cover_direct.py writes a bare spec + assets + renders; Cover Canvas opens a
JOB -- a directory holding `job.json` (a JobState) whose concepts each carry a
ready CoverSpec. This is the adapter between the two, so hand-directed covers
can be edited on the canvas like pipeline-built ones.

    cover_pack_job.py --job-id longsword --jobs-root DIR DIR1 DIR2 ...

Each input directory becomes one concept, in the order given. Asset filenames
are rewritten to the pipeline's own `assets/c<i>_<slot>.png` convention on the
way in, because six independently-built concepts collide otherwise (three of
them own a slot called `background`).
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from docproof.cover import pipeline as cover_pipeline
from docproof.cover.model import (Brief, ConceptState, CoverSpec, JobState,
                                  RenderReport)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--jobs-root", required=True)
    ap.add_argument("dirs", nargs="+")
    args = ap.parse_args()

    root = Path(args.jobs_root)
    out = cover_pipeline.job_dir(root, args.job_id)
    (out / cover_pipeline.ASSETS_DIR).mkdir(parents=True, exist_ok=True)
    (out / cover_pipeline.RENDERS_DIR).mkdir(parents=True, exist_ok=True)

    brief: Brief | None = None
    concepts: list[ConceptState] = []

    for i, raw in enumerate(args.dirs):
        src = Path(raw)
        spec = CoverSpec(**_load(src / "spec.json"))
        if brief is None and (src / "brief.json").is_file():
            brief = Brief(**_load(src / "brief.json"))

        # Assets: copy under the pipeline's per-concept naming and repoint the
        # slot at its new home, so concept 3's `background` cannot overwrite
        # concept 5's.
        for slot in spec.art:
            if not slot.asset:
                continue
            old = src / slot.asset
            if not old.is_file():
                print(f"  concept {i}: missing asset {old}, slot left unpainted")
                slot.asset = ""
                continue
            rel = f"{cover_pipeline.ASSETS_DIR}/c{i}_{slot.id}{old.suffix}"
            shutil.copy2(old, out / rel)
            slot.asset = rel

        # Renders: same rename, so every concept's v1_c0.* does not land on
        # the same four filenames.
        renders: list[str] = []
        for r in sorted((src / cover_pipeline.RENDERS_DIR).glob("*")):
            rel = f"{cover_pipeline.RENDERS_DIR}/c{i}_{r.name}"
            shutil.copy2(r, out / rel)
            if r.suffix == ".png" and "thumb" not in r.name:
                renders.append(rel)

        report = None
        notes = src / "report.json"
        if notes.is_file():
            report = RenderReport(**_load(notes))

        concepts.append(ConceptState(spec=spec, status="ready",
                                     report=report, renders=renders))
        print(f"concept {i}: {src.name} -- {spec.archetype}, "
              f"{len(renders)} render(s)")

    if brief is None:
        raise SystemExit("no brief.json found in any input directory")

    job = JobState(job_id=args.job_id, brief=brief,
                   anthropic_lane="", image_quality="draft",
                   status="ready", concepts=concepts,
                   created=datetime.now(timezone.utc).isoformat())
    cover_pipeline._write_state(root, job)
    print(f"\njob {args.job_id} written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
