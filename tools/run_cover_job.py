#!/usr/bin/env python
"""Run one Cover Studio job headlessly, off the web app.

Cover Studio normally runs behind app/routes/cover.py; this is the same flow
with the HTTP layer removed, so an agent can drive a job from a shell:

    run_cover_job.py new     --brief brief.json --root DIR [--full]
    run_cover_job.py revise  --job JOB_ID --root DIR --concept 0 --notes "..."
                             [--allow-new-art]
    run_cover_job.py show    --job JOB_ID --root DIR

Anthropic roles (the director, revisions, the planner) and the per-concept
building agents all run on the SUBSCRIPTION lane -- $0 in API dollars. Only
gpt-image-2 art costs money, and --full is the only way to leave the cheap 1K
draft rung.

brief.json is a docproof.cover.model.Brief dump; `manuscript` is an optional
extra key holding a path, stripped before the Brief is built.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.settings import get_api_key
from docproof.cover import pipeline as cover_pipeline
from docproof.cover import subscription
from docproof.cover.direction import REVISION_MODEL
from docproof.cover.imaging import make_client
from docproof.cover.model import Brief


def _providers() -> cover_pipeline.Providers:
    subscription.preflight()          # fails loudly rather than mid-run
    return cover_pipeline.Providers(
        direction=subscription.SubscriptionProvider(),
        revision=subscription.SubscriptionProvider(),
    )


def _image_client():
    key = get_api_key("openai")
    if not key:
        sys.exit("No OpenAI key: set OPENAI_API_KEY.")
    return make_client(key)


def _report(root: Path, job_id: str) -> int:
    job = cover_pipeline.load_job(root, job_id)
    if job is None:
        print(f"no such job: {job_id}")
        return 1
    print(f"job     {job.job_id}")
    print(f"status  {job.status}" + (f"  ERROR: {job.error}" if job.error else ""))
    print(f"spend   ${cover_pipeline.total_usd(job):.2f}"
          f"  ({job.image_quality or 'full'} art, {job.anthropic_lane} Claude)")
    d = cover_pipeline.job_dir(root, job_id)
    for i, c in enumerate(job.concepts):
        print(f"\nconcept {i}: {c.status}"
              f"  archetype={c.spec.archetype}" if c.spec else f"\nconcept {i}: {c.status}")
        if c.error:
            print(f"  error: {c.error}")
        for r in c.renders:
            print(f"  render: {d / r}")
        if c.report:
            if c.report.warnings:
                print("  warnings: " + "; ".join(c.report.warnings))
            if c.report.adjustments:
                print("  autopilot: " + "; ".join(c.report.adjustments))
            print(f"  contrast: {c.report.contrast}")
    print(f"\njob dir {d}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new")
    p.add_argument("--brief", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--full", action="store_true",
                   help="2K art (~$0.05/image) instead of the 1K draft rung")

    p = sub.add_parser("revise")
    p.add_argument("--job", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--concept", type=int, default=0)
    p.add_argument("--notes", required=True)
    p.add_argument("--allow-new-art", action="store_true")

    p = sub.add_parser("show")
    p.add_argument("--job", required=True)
    p.add_argument("--root", required=True)

    args = ap.parse_args()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)

    if args.cmd == "show":
        return _report(root, args.job)

    providers, image_client = _providers(), _image_client()
    critique_client = subscription.SubscriptionAnthropicClient()

    if args.cmd == "new":
        data = json.loads(Path(args.brief).read_text(encoding="utf-8"))
        ms = data.pop("manuscript", "") or None
        brief = Brief(**data)
        job = cover_pipeline.create_job(
            root, brief, manuscript_path=ms,
            manuscript_name=Path(ms).name if ms else "",
            anthropic_lane="subscription",
            image_quality="full" if args.full else "draft")
        # The director reads the whole book; the text is passed in and never
        # written down (cover_pipeline.read_manuscript's own docstring).
        text = cover_pipeline.read_manuscript(ms) if ms else ""
        print(f"job {job.job_id} -- {brief.concepts} concept(s), "
              f"{'full' if args.full else 'draft'} art", flush=True)
        asyncio.run(cover_pipeline.run_job(
            root, job.job_id, providers, image_client,
            manuscript=text))
        return _report(root, job.job_id)

    asyncio.run(cover_pipeline.run_revision(
        root, args.job, args.concept, args.notes, args.allow_new_art,
        providers, image_client, critique_client))
    return _report(root, args.job)


if __name__ == "__main__":
    raise SystemExit(main())
