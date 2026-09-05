from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import logging
import re
import sys
from pathlib import Path

from . import __version__
from . import batch as batchlib
from .analyzer import MockAnalyzer
from .config import load_config
from .cli_parser import (DEFAULT_WORKSPACE, _common, _engine_arg, _galley_parser,
                         _galley_spend_args, _genre_arg, _genre_choices,
                         _profile_arg, _stage_arg, _stage_choices, build_parser)
from .ingest import IngestError
from .logging_setup import setup_logging
from .models import CoverageLedger, Usage
from .pipeline import chunk_outline, finish, prepare, run_sync
from .profiles import CANDIDATE_ONLY, DETECTOR_ONLY, apply_profile
from .providers import ProviderError, build_provider, estimate_cost

log = logging.getLogger("docproof.main")


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "capabilities":
        return _cmd_capabilities(ap)
    return {"inventory": cmd_inventory, "review": cmd_review,
            "submit": cmd_submit, "status": cmd_status,
            "collect": cmd_collect, "prep": cmd_prep, "rejudge": cmd_rejudge,
            "eval": cmd_eval, "compare": cmd_compare, "sweep": cmd_sweep,
            "tense": cmd_tense, "cites": cmd_cites,
            "merge": cmd_merge,
            "import-findings": cmd_import_findings, "replay": cmd_replay,
            "galley": cmd_galley}[args.cmd](args)


def _capabilities_args(parser) -> list[dict]:
    """A verb's arguments — name, flags, required, choices, first line of
    help. Without these a headless practitioner can learn that a verb EXISTS
    but not how to call it, and the context-discipline rule bans the big
    `--help` dumps that would otherwise fill the gap (Purpura beta)."""
    out: list[dict] = []
    for a in parser._actions:
        if isinstance(a, (argparse._SubParsersAction, argparse._HelpAction)):
            continue
        entry: dict = {"name": a.dest}
        if a.option_strings:
            entry["flags"] = list(a.option_strings)
            if a.required:
                entry["required"] = True
        else:
            entry["positional"] = True
            if a.nargs not in ("?", "*"):
                entry["required"] = True
        if a.choices:
            entry["choices"] = [str(c) for c in a.choices]
        if a.help:
            entry["help"] = " ".join(a.help.split())[:140]
        out.append(entry)
    return out


def _capabilities_tree(parser) -> list[dict] | None:
    """Walk an argparse parser's subcommands into a JSON tree of
    {name, help, args?, subcommands?}. Introspective, so it can never drift
    from the real verbs the way a hand-maintained list would."""
    action = None
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            action = a
            break
    if action is None:
        return None
    help_by_name = {ca.dest: (ca.help or "") for ca in action._choices_actions}
    out: list[dict] = []
    for name, subparser in action.choices.items():
        entry: dict = {"name": name, "help": help_by_name.get(name, "")}
        args = _capabilities_args(subparser)
        if args:
            entry["args"] = args
        child = _capabilities_tree(subparser)
        if child:
            entry["subcommands"] = child
        out.append(entry)
    return out


def _cmd_capabilities(ap) -> int:
    """The compact capability manifest — the whole command tree plus the config
    section names, as one JSON document."""
    from .config import Config
    manifest = {
        "tool": "docproof",
        "version": __version__,
        "commands": _capabilities_tree(ap) or [],
        "config_sections": sorted(Config.model_fields.keys()),
        "genres": list(_genre_choices()),
        "stages": list(_stage_choices()),
    }
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


# The subagent lane's ceiling on calls in flight, whatever api.concurrency
# says: each turn is a whole Claude Code CLI process, not a socket.
SUBAGENT_MAX_INFLIGHT = 4


def _lane_concurrency(cfg, engine: str, model: str) -> int:
    """How many verify/settle calls a galley verb keeps in flight: the
    config's own limiter for the API lane (`Config.concurrency_for`, the one
    the ladder runs under), a small fixed ceiling for the subagent lane, and
    1 for the deterministic lane. `api.concurrency: 1` still means serial
    everywhere."""
    if engine == "provider":
        return cfg.concurrency_for(model)
    if engine == "subagent":
        return max(1, min(cfg.api.concurrency, SUBAGENT_MAX_INFLIGHT))
    return 1


def _resolve_engine(args, cfg, *, default_model: str | None = None
                    ) -> tuple[str, Any, str]:
    """(engine, provider, model) for --engine. `provider` is None for the
    deterministic lane. A model named with --model picks the lane when
    --engine is auto: a Claude id/alias -> subagent, anything else ->
    provider."""
    from .providers.subagent import (SubagentProvider, availability,
                                     is_subagent_model, resolve_model)
    engine = getattr(args, "engine", None) or "auto"
    model = getattr(args, "model", None) or ""
    if engine == "auto":
        ok, why = (True, "") if (model and not is_subagent_model(model)) \
            else availability()
        if model and not is_subagent_model(model):
            engine = "provider"
        elif ok:
            engine = "subagent"
        elif model or default_model:
            engine = "provider"
            model = model or default_model or ""
            print(f"note: the $0 subagent lane is unavailable ({why}); "
                  f"--engine auto falls back to the API provider "
                  f"({model}), which bills.", file=sys.stderr)
        else:
            engine = "none"
            print(f"note: the $0 subagent lane is unavailable ({why}) and "
                  f"no model is configured; --engine auto falls back to "
                  f"deterministic-only.", file=sys.stderr)
    if engine == "subagent":
        prov = SubagentProvider(model=model or None)
        return "subagent", prov, f"subagent:{resolve_model(model or None)}"
    if engine == "provider":
        model = model or default_model or cfg.api.model
        prov = build_provider(cfg, model=model)
        return "provider", prov, model
    return "none", None, ""


def _selection(args) -> list[str] | None:
    raw = getattr(args, "only", None)
    if not raw:
        return None
    return [c.strip() for c in raw.split(",") if c.strip()]


def _resolve_error_dir(config_path: str | Path) -> Path:
    """Locate the ``error_types/`` prompt directory for a run config.

    Prefer the directory beside the config — ``config/error_types`` when running
    the shipped ``config/default.yaml``, or a workspace's own edited copy. When a
    RELOCATED config has no sibling ``error_types/`` (the common case for a
    materialized genre-pack config written into a book workspace), fall back to
    the packaged shipped directory, so a materialized config is self-contained
    wherever it lives. Before this fallback, such a config failed ``inventory``
    (and every typed pass) by looking for ``error_types/`` beside itself and
    finding nothing. When neither exists, return the beside-path unchanged so the
    downstream FileNotFoundError still names the location the user expected."""
    beside = Path(config_path).parent / "error_types"
    if beside.is_dir():
        return beside
    packaged = Path(__file__).resolve().parent.parent / "config" / "error_types"
    if packaged.is_dir():
        return packaged
    return beside


def _resolve_stage_genre(config_path, stage, genre) -> tuple[str | None, str | None]:
    """Which --stage/--genre to actually apply on top of `config_path`.

    A materialized genre-pack config already has its stage and genre baked into
    the YAML and stamped in a `# galley: genre=… stage=…` header. Re-applying the
    flag on top of it is the P2-8 trap: it composes a DIFFERENT effective config
    than the pack materialized, so approve and review disagree on the config hash
    and `review --approval` refuses (exit 5). So an axis the config already
    stamps is dropped here — with a warning — for EVERY consumer (review,
    approve, routes, certify) identically, which is what keeps their hashes equal.
    A flag that CONFLICTS with the stamp is dropped too, but loudly: the
    materialized value is what governs, not the stray flag."""
    prov = _sniff_pack_provenance(config_path)
    out_stage, out_genre = stage, genre
    for axis, flag in (("stage", stage), ("genre", genre)):
        stamped = prov.get(axis)
        if not stamped or flag is None:
            continue
        if flag == stamped:
            print(f"note: --{axis} {flag} is already materialized into "
                  f"{config_path} (galley header) — not re-applying it",
                  file=sys.stderr)
        else:
            print(f"warning: {config_path} was materialized with {axis}="
                  f"{stamped}, but --{axis} {flag} was passed — the materialized "
                  f"{axis}={stamped} governs; the flag is ignored", file=sys.stderr)
        if axis == "stage":
            out_stage = None
        else:
            out_genre = None
    return out_stage, out_genre


def _configure(args):
    cfg = load_config(args.config)
    if getattr(args, "error_types", None):
        cfg.error_types = [[k.strip() for k in group.split("+") if k.strip()]
                           for group in args.error_types.split(",")
                           if group.strip()]
    if getattr(args, "min_confidence", None):
        cfg.min_confidence = args.min_confidence
    if getattr(args, "variant", None):
        cfg.variant = args.variant
    if getattr(args, "dictionary", None):
        cfg.spellcheck.dictionary = args.dictionary
    if getattr(args, "no_comments", False):
        cfg.comments = False
    if getattr(args, "out", None):
        cfg.output_dir = args.out
    # Precedence: profile > stage > genre. Reassert stage locks after genre
    # application; --model may override the profile for detector comparisons.
    _stage, _genre = _resolve_stage_genre(
        args.config, getattr(args, "stage", None), getattr(args, "genre", None))
    stage_locks: dict = {}
    if _stage:
        from .stages import apply_stage
        cfg, stage_locks = apply_stage(cfg, _stage)
    genre_pending = {}
    if _genre:
        from .genre import apply_genre
        cfg, genre_pending = apply_genre(cfg, _genre)
        for key in genre_pending:
            log.warning("genre %r set %s, which this build cannot apply yet "
                       "(no matching config section) — ignored", _genre,
                       key)
    if stage_locks:
        from .stages import enforce_locks
        violated = enforce_locks(cfg, stage_locks)
        for key in violated:
            log.warning("stage %r locks %s; the genre %r tried to change it — "
                        "the stage lock wins", _stage, key, _genre)
    apply_profile(cfg, getattr(args, "profile", None))
    if getattr(args, "model", None):
        cfg.api.model = args.model
    error_dir = _resolve_error_dir(args.config)
    return cfg, error_dir


def _print_cost_estimate(cfg, prepared, doc_tokens: int) -> None:
    """The no-API cost projection shared by `inventory` and `review --dry-run`:
    the review reads (chunks x models x token budgets), plus the whole-book
    continuity and chapter-continuity reads on their own models. Every number is
    an order-of-magnitude guide from the price table, not a quote — output token
    counts are unknowable up front."""
    # Output tokens are unknowable up front; assume a modest cap per request so
    # the number is an order-of-magnitude guide, not a quote.
    out_guess = prepared.request_count * 600
    now = estimate_cost(cfg.api.model, input_tokens=prepared.est_document_tokens,
                        output_tokens=out_guess)
    if now is not None:
        print(f"\nRough cost on {cfg.api.model}: ~${now:.2f} now, "
              f"~${now / 2:.2f} as a batch (50% cheaper, results within hours)")

    # Price continuity once at the document token count, separately from the
    # review batch. Reads over max_input_tokens are skipped by the pipeline.
    if cfg.continuity.enabled:
        if doc_tokens > cfg.continuity.max_input_tokens:
            print(f"  + continuity: book exceeds max_input_tokens "
                  f"({cfg.continuity.max_input_tokens:,}); the read is skipped, "
                  f"only the free calendar check runs")
        else:
            cont = estimate_cost(cfg.continuity.model, input_tokens=doc_tokens,
                                 output_tokens=cfg.continuity.max_output_tokens)
            if cont is not None:
                print(f"  + continuity read on {cfg.continuity.model}: "
                      f"~${cont:.2f} (one whole-book read, query-only, "
                      f"not batchable)")

    # Chapter continuity reads the book once, split across chapters, plus a small
    # judge — priced at the document's tokens in, and each chapter's output
    # ceiling out, so the estimate scales with how the book segments.
    if cfg.chapter_continuity.enabled:
        from .continuity import chapters as _chapters
        cc = cfg.chapter_continuity
        cc_model = cc.model or cfg.continuity.model or cfg.api.model
        units = _chapters(prepared.doc.paragraphs, cfg.skip.is_sweep_only,
                          min_tokens=cc.min_chapter_tokens,
                          max_tokens=cc.max_chapter_tokens)
        chap = estimate_cost(cc_model, input_tokens=doc_tokens,
                             output_tokens=len(units) * cc.max_output_tokens)
        if chap is not None:
            print(f"  + chapter continuity on {cc_model}: ~${chap:.2f} "
                  f"({len(units)} chapter read(s), query-only, plus a small "
                  f"judge on {cc.judge_model}); the reads ride their own batch, "
                  f"so a batch submission roughly halves this")


def cmd_inventory(args) -> int:
    cfg, error_dir = _configure(args)
    setup_logging(cfg.output_dir)
    try:
        # Inventory must not spend: disable the story-sheet and candidate-judge
        # reads while retaining the free analyses.
        prepared = prepare(cfg, args.input, error_dir, dry_run=True)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if getattr(args, "para_map", False):
        # The id/anchor reference: canonical (post-normalization) text, which
        # is exactly what an import-findings/replay row's original_text must
        # match and what an intent-zones regex runs against. Tab-separated so
        # it greps and cuts cleanly.
        for para in prepared.doc.paragraphs:
            # Full text, not a preview: rows exist to be matched against.
            # Tabs/newlines are escaped so the TSV stays one row per paragraph;
            # the length column is the unescaped canonical length.
            full = para.text.replace("\t", "\\t").replace("\n", "\\n")
            print(f"{para.para_id}\t{len(para.text)}\t{full}")
        return 0

    doc_tokens = sum(c.est_tokens for c in prepared.chunks)
    plan = prepared.effective_pass_plan
    print(f"{len(prepared.doc.paragraphs)} reviewable paragraphs → "
          f"{len(prepared.chunks)} chunks (~{doc_tokens:,} document tokens)")
    print(f"{len(cfg.error_type_keys)} error type(s) in "
          f"{len(cfg.error_type_groups)} category/-ies, {len(plan)} pass(es) → "
          f"{prepared.request_count} API calls, "
          f"~{prepared.est_document_tokens:,} document tokens sent")
    for spec in cfg.error_type_specs:
        extra = []
        if spec.passes > 1:
            extra.append(f"x{spec.passes}")
        if spec.token_budget:
            extra.append(f"@{spec.token_budget}t")
        suffix = f"  ({', '.join(extra)})" if extra else ""
        print(f"  category: {' + '.join(spec.keys)}{suffix}")

    swept = sum(r.flagged for r in prepared.sweep_reports)
    if prepared.sweep_reports:
        print(f"\n{len(prepared.sweep_reports)} scripted sweep(s) → "
              f"{swept} correction(s), already found, no API call:")
        for r in prepared.sweep_reports:
            print(f"  {r.key:<28} {r.flagged:>4} flagged, "
                  f"{r.remaining} remaining")

    _print_cost_estimate(cfg, prepared, doc_tokens)

    print("\nSections (pass any of these to --only):")
    for row in chunk_outline(prepared):
        print(f"  {row['chunk_id']:<12} {row['paragraphs']:>3} para "
              f"~{row['est_tokens']:>5,}tok  {row['preview'][:60]}")
    for pid, reason in prepared.doc.skipped:
        print(f"  skipped {pid:<24} {reason}")
    return 0


def _approval_guard(args, cfg) -> int | None:
    """Refuse a paid run whose inputs deviate from an approval manifest. Returns
    an exit code to abort with, or None when the run is within approval (or no
    --approval was given). The immutable-manifest gate: source hash, config
    hash, and active model routes must all match what a human approved."""
    approval = getattr(args, "approval", None)
    if not approval:
        return None
    from galley.manifest import verify_plan
    try:
        manifest = json.loads(Path(approval).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not read approval {approval}: {e}", file=sys.stderr)
        return 5
    devs = verify_plan(manifest, source=args.input, cfg=cfg)
    if devs:
        print(f"REFUSED: this run deviates from {approval} —", file=sys.stderr)
        for d in devs:
            print(f"  - {d.kind}: {d.detail}", file=sys.stderr)
        print("  re-approve with `docproof galley approve` if the change is "
              "intended.", file=sys.stderr)
        return 5
    print(f"approval {approval}: source, config, and model routes match — "
          f"proceeding.")
    return None


def cmd_review(args) -> int:
    cfg, error_dir = _configure(args)
    if getattr(args, "dry_run", False):
        # dry_run skips paid preparation stages but retains real ingest and
        # chunking counts for the estimate.
        try:
            prepared = prepare(cfg, args.input, error_dir, dry_run=True)
        except (IngestError, FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        doc_tokens = sum(c.est_tokens for c in prepared.chunks)
        print(f"dry run — {len(prepared.doc.paragraphs)} reviewable paragraph(s) "
              f"→ {len(prepared.chunks)} chunk(s), {len(cfg.error_type_keys)} "
              f"error type(s) in {len(prepared.effective_pass_plan)} pass(es) → "
              f"{prepared.request_count} API call(s)")
        _print_cost_estimate(cfg, prepared, doc_tokens)
        print("\nno API call made, no output written.")
        return 0
    profile = getattr(args, "profile", None)
    if profile in (DETECTOR_ONLY, CANDIDATE_ONLY):
        incompatible = []
        if args.rounds not in (None, 1):
            incompatible.append("--rounds")
        if args.meaning_check or args.meaning_model:
            incompatible.append("--meaning-check/--meaning-model")
        if args.fix_check or args.fix_model:
            incompatible.append("--fix-check/--fix-model")
        if incompatible:
            print(f"error: {profile} cannot be combined with "
                  + ", ".join(incompatible), file=sys.stderr)
            return 2
    # Apply all config overrides before approval so the verified hash
    # matches the run that executes.
    if args.rounds is not None:
        cfg.rounds.count = args.rounds
    # The gate is a flag rather than a tri-state: absent leaves whatever the
    # config says, so a house config that ships it on is not silently disarmed
    # by every CLI run. Naming a model implies wanting the gate.
    if getattr(args, "meaning_model", None):
        cfg.meaning_check.model = args.meaning_model
        cfg.meaning_check.enabled = True
    if getattr(args, "meaning_check", False):
        cfg.meaning_check.enabled = True
    if getattr(args, "fix_model", None):
        cfg.fix_check.model = args.fix_model
        cfg.fix_check.enabled = True
    if getattr(args, "fix_check", False):
        cfg.fix_check.enabled = True
    guard = _approval_guard(args, cfg)
    if guard is not None:
        return guard
    out = Path(cfg.output_dir)
    setup_logging(out)

    if cfg.rounds.count > 1:                          # multi-round review
        from .rounds import run_sync_rounds
        canned = None
        if args.mock_findings:
            canned = _load_mocks(args.mock_findings)
            if canned is None:
                return 2
        try:
            outputs = run_sync_rounds(cfg, args.input, error_dir, out_dir=out,
                                      source_path=args.input,
                                      mock_findings=canned)
        except (IngestError, FileNotFoundError, ValueError, ProviderError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"\n{_result_line(outputs)}.")
        try:
            _rounds_usage = _usage_from_payload(
                json.loads(outputs.findings_json.read_text("utf-8")))
        except (OSError, json.JSONDecodeError):
            _rounds_usage = Usage()
        print(f"  {_cost_line(_rounds_usage, cfg.api.model)}")
        for p in (outputs.reviewed_path, outputs.change_log,
                  outputs.summary_md, outputs.findings_json, out / "run.log"):
            if p is not None:
                print(f"  {p}")
        if args.json:
            from .contract import build_envelope
            payload = json.loads(outputs.findings_json.read_text("utf-8"))
            envelope = build_envelope(
                findings=payload.get("findings", []),
                usage=_usage_from_payload(payload), fallback_model=cfg.api.model,
                # Multi-round review runs its own per-round driver rather than
                # this module's run_sync/checkpoint path, so neither a
                # CoverageLedger nor a resumable checkpoint is available here
                # to report — both simply read as empty/null.
                coverage=None, checkpoint=None)
            print(json.dumps(envelope, ensure_ascii=False))
        return 0

    # prepare() is deterministic and API-free, so it runs before any provider is
    # built: its content hash fingerprints the checkpoint, and a --resume needs
    # the Prepared to hand to finish() regardless.
    try:
        prepared = prepare(cfg, args.input, error_dir,
                           max_chunks=args.max_chunks,
                           selection=_selection(args))
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    from . import run_checkpoint
    from .batch import pass_prompts
    from .checkpoint import Checkpoint
    # Both checkpoint mechanisms hash the config contents, not its path,
    # so editing a config invalidates stale findings.
    fingerprint = {"kind": "review", "content_hash": prepared.content_hash,
                   "config": cfg.model_dump(mode="json"),
                   "prompts": pass_prompts(cfg, prepared),
                   "selection": _selection(args)}

    coverage = None
    resumed = False
    if args.resume and not args.mock_findings:
        hit = run_checkpoint.load(out, fingerprint=fingerprint)
        if hit is not None:
            findings, usage, coverage = hit.findings, hit.usage, hit.coverage
            resumed = True
            print(f"Resumed from checkpoint: {len(findings)} finding(s) "
                  f"replayed, paid reads skipped.")
        else:
            print("No usable checkpoint in the output directory; "
                  "reviewing fresh.")

    call_checkpoint_path = out / "checkpoint.json"
    if not resumed:
        if args.mock_findings:
            canned = _load_mocks(args.mock_findings)
            if canned is None:
                return 2
            findings, usage = _run_mock(cfg, prepared, canned)
        else:
            try:
                provider = build_provider(cfg)
            except ProviderError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            coverage = CoverageLedger()
            # Resume completed calls automatically when the checkpoint fingerprint
            # matches; --resume controls the later findings-boundary replay.
            call_checkpoint = Checkpoint(call_checkpoint_path,
                                         fingerprint=fingerprint)
            call_checkpoint.load()
            findings, usage = run_sync(cfg, prepared, provider,
                                       coverage=coverage,
                                       checkpoint=call_checkpoint)
            # The paid reads are done. Snapshot them before finish()'s late
            # stages so a crash there never buys the detector passes again.
            run_checkpoint.save(out, findings=findings, usage=usage,
                                coverage=coverage, fingerprint=fingerprint)

    outputs = finish(prepared, findings, usage, cfg, out_dir=out,
                     source_path=args.input, coverage=coverage)
    # finish() wrote the deliverable and findings.json; both checkpoints have
    # done their job and must not shadow a later, different run.
    run_checkpoint.clear(out)
    call_checkpoint_path.unlink(missing_ok=True)
    print(f"\n{_result_line(outputs)}.")
    print(f"  {_cost_line(usage, cfg.api.model)}")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json, out / "run.log"):
        if p is not None:
            print(f"  {p}")
    if args.json:
        from .contract import build_envelope
        payload = json.loads(outputs.findings_json.read_text("utf-8"))
        envelope = build_envelope(findings=payload.get("findings", []),
                                  usage=usage, fallback_model=cfg.api.model,
                                  coverage=coverage, checkpoint=None)
        print(json.dumps(envelope, ensure_ascii=False))
    return 0


def cmd_submit(args) -> int:
    cfg, error_dir = _configure(args)
    setup_logging(cfg.output_dir)
    try:
        provider = build_provider(cfg)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        job = batchlib.submit(cfg, args.input, error_dir, provider,
                              args.workspace, max_chunks=args.max_chunks,
                              selection=_selection(args))
    except (IngestError, batchlib.BatchError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"Queued {job.source_name} as job {job.job_id}")
    print(f"  {job.request_count} request(s) at batch prices on {job.model}")
    print(f"  Results usually arrive within a few hours (24h at the outside).")
    print(f"\nCheck on it:  docproof status {job.job_id}")
    print(f"Finish it:    docproof collect {job.job_id}")
    return 0


def cmd_status(args) -> int:
    cfg, _ = _configure(args)
    setup_logging(cfg.output_dir)
    try:
        jobs = ([batchlib.load(args.workspace, args.job_id)] if args.job_id
                else batchlib.load_all(args.workspace))
    except (batchlib.BatchError, json.JSONDecodeError, TypeError) as e:
        # load_all shrugs off unreadable manifests; asking for one by id has
        # to answer in words too, not a traceback.
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not jobs:
        print(f"No queued reviews in {args.workspace}/")
        return 0

    provider = None
    for job in jobs:
        if job.active:
            if provider is None:
                try:
                    provider = build_provider(cfg)
                except ProviderError as e:
                    print(f"error: {e}", file=sys.stderr)
                    return 2
            try:
                batchlib.poll(job, provider, args.workspace)
            except Exception as e:            # noqa: BLE001 - report, continue
                log.warning("Could not refresh %s: %s", job.job_id, e)
        print(f"{job.job_id:<40} {_plain_state(job):<28} {job.source_name}")
        if job.error:
            print(f"    {job.error}")
    return 0


def cmd_collect(args) -> int:
    cfg, error_dir = _configure(args)
    setup_logging(cfg.output_dir)
    try:
        job = batchlib.load(args.workspace, args.job_id)
        provider = build_provider(cfg)
        outputs = batchlib.collect(job, provider, error_dir, args.workspace,
                                   out_dir=args.out)
    except (batchlib.BatchError, ProviderError, IngestError, ValueError,
            TypeError) as e:
        # ValueError is the edited-document case: the manifest names chunks
        # the re-chunked source no longer has, which the content-hash check
        # never gets to see. (It also covers a corrupt manifest's JSON.)
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"\n{_result_line(outputs)}.")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json):
        if p is not None:
            print(f"  {p}")
    return 0


def cmd_rejudge(args) -> int:
    """Put a finished review's corrections to the judge gates, and nothing else.

    Its own command rather than a flag on `review` because it is a different
    job: `review` finds corrections, this one rules on corrections already
    found. It makes no detector call and re-reads nothing — the record the
    original run left is the input, so a book proofread before these gates
    existed can be gated now for the price of the gates alone."""
    from .rejudge import RejudgeError, rejudge

    cfg = load_config(args.config)
    error_dir = _resolve_error_dir(args.config)
    if args.meaning_model:
        cfg.meaning_check.model = args.meaning_model
        cfg.meaning_check.enabled = True
    if args.meaning_check:
        cfg.meaning_check.enabled = True
    if args.fix_model:
        cfg.fix_check.model = args.fix_model
        cfg.fix_check.enabled = True
    if args.fix_check:
        cfg.fix_check.enabled = True

    out = Path(args.out) if args.out else Path(args.results, "rejudged")
    setup_logging(out)
    usage = Usage()
    try:
        outputs = rejudge(cfg, args.results, out_dir=out, error_dir=error_dir,
                          source=args.source, usage=usage)
    except (RejudgeError, ProviderError, IngestError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"\n{_result_line(outputs)} ({usage.api_calls} judge call(s)).")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json):
        if p is not None:
            print(f"  {p}")
    return 0


def cmd_prep(args) -> int:
    """Tag a manuscript for the house template.

    Deliberately its own command rather than a flag on `review`: prep answers
    what each paragraph IS, review answers what is wrong inside one, and a run
    that tried to do both would have to choose which of two very different
    documents to hand back."""
    from . import prep as preplib
    from .prep.convert import ConversionError, ensure_docx

    cfg = load_config(args.config)
    if args.model:
        cfg.api.model = args.model
    if args.out:
        cfg.output_dir = args.out
    if args.style_sheet:
        cfg.prep.style_sheet = args.style_sheet
    if args.no_verify:
        cfg.prep.verify = False
    kinds = (["indesign", "tracked"] if args.output == "both"
             else list(preplib.OUTPUT_KINDS) if args.output == "all"
             else [args.output] if args.output else list(cfg.prep.outputs))

    out = Path(cfg.output_dir)
    setup_logging(out)
    config_dir = Path(args.config).parent

    try:
        source, note = ensure_docx(args.input, out)
        prepared = preplib.prepare(cfg, source, config_dir=config_dir)
    except (ConversionError, IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if note:
        print(f"note: {note}")

    print(f"{prepared.paragraph_count} paragraphs → {prepared.request_count} "
          f"request(s) on {cfg.api.model}, ~"
          f"{prepared.est_document_tokens:,} tokens sent")

    provider = None
    if args.mock_tags:
        tags, usage = preplib.run_mock(prepared)
    else:
        try:
            provider = build_provider(cfg)
        except ProviderError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        tags, usage = preplib.run(
            cfg, prepared, provider,
            progress=lambda done, total: print(
                f"  labelled window {done} of {total}", flush=True))

    meta = None
    if "book" in kinds:
        detected = (preplib.detect_meta(cfg, prepared, provider, usage=usage)
                    if provider is not None else preplib.BookMeta())
        meta = preplib.merge_meta(detected, subject=args.subject,
                                  title=args.book_title,
                                  author=args.book_author)
        print(f"  book sketch: subject '{meta.subject or 'default'}', "
              f"title {meta.title or Path(args.input).stem!r}, "
              f"author {meta.author or '(none)'}")

    try:
        outputs = preplib.finish(prepared, tags, usage, cfg, out_dir=out,
                                 source_path=args.input, outputs=kinds,
                                 meta=meta)
    except preplib.VerificationFailed as e:
        print(f"error: {e}", file=sys.stderr)
        print(f"  see {out / 'prep_notes.md'} for what prep intended to do.",
              file=sys.stderr)
        return 3

    print(f"\n{outputs.tagged} paragraph(s) tagged, "
          f"{outputs.plan.inserted_breaks} scene break(s) written, "
          f"{outputs.flags} flag(s) for the designer.")
    for check in outputs.verifications:
        print(f"  ✓ {check.describe()}")
    for path in list(outputs.documents.values()) + [outputs.notes_md,
                                                    outputs.notes_json]:
        print(f"  {path}")
    return 0


def cmd_compare(args) -> int:
    import json as _json

    from .trackdiff import (TrackDiffError, compare_edits, extract_edits,
                            open_docx, render_markdown, report_json)

    cfg = load_config(args.config)
    if getattr(args, "out", None):
        cfg.output_dir = args.out
    out = Path(cfg.output_dir)
    setup_logging(out)

    if getattr(args, "formatting", False):
        return _cmd_compare_formatting(args, out)

    try:
        a = extract_edits(open_docx(args.doc_a))
        b = extract_edits(open_docx(args.doc_b))
    except TrackDiffError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    report = compare_edits(a, b, label_a=args.label_a, label_b=args.label_b)

    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "compare.md"
    json_path = out / "compare.json"
    md_path.write_text(render_markdown(report, a.base), encoding="utf-8")
    json_path.write_text(
        _json.dumps(report_json(report, a.base), indent=2, ensure_ascii=False),
        encoding="utf-8")

    def pct(x): return f"{x * 100:.0f}%" if x is not None else "—"
    print(f"{args.label_a}: {a.edit_count} edit(s)  ·  "
          f"{args.label_b}: {b.edit_count} edit(s)  ·  "
          f"{report.aligned_paras} paragraph(s) compared")
    if report.unaligned:
        print(f"  {len(report.unaligned)} paragraph(s) skipped "
              f"(base text differs — not comparable)")
    print(f"\n  agree {report.agree}  ·  different fix {report.diff_fix}  ·  "
          f"only {args.label_a} {report.only_a}  ·  "
          f"only {args.label_b} {report.only_b}")
    if report.query_located:
        print(f"  ({report.query_located} of the only-{args.label_a} were "
              f"flagged by a {args.label_b} query)")
    print(f"\nScored against {args.label_a} as ground truth:")
    print(f"  located recall {pct(report.located_recall)}  ·  "
          f"exact recall {pct(report.exact_recall)}  ·  "
          f"precision {pct(report.precision)}  ·  F1 {pct(report.f1)}")
    if report.query_located:
        print(f"  located recall + queries "
              f"{pct(report.located_recall_with_queries)} "
              f"(crediting a query that points at the right span)")
    print(f"\n  {md_path}\n  {json_path}")
    return 0


def _cmd_compare_formatting(args, out: Path) -> int:
    """`compare --formatting`: diff the InDesign paragraph styling on two docs
    instead of their tracked changes."""
    import json as _json

    from . import formatdiff

    try:
        a = formatdiff.extract_styles(formatdiff.open_docx(args.doc_a))
        b = formatdiff.extract_styles(formatdiff.open_docx(args.doc_b))
    except formatdiff.TrackDiffError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    report = formatdiff.compare_styles(a, b, label_a=args.label_a,
                                       label_b=args.label_b)

    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "compare-formatting.md"
    json_path = out / "compare-formatting.json"
    md_path.write_text(formatdiff.render_markdown(report), encoding="utf-8")
    json_path.write_text(
        _json.dumps(formatdiff.report_json(report), indent=2,
                    ensure_ascii=False),
        encoding="utf-8")

    def pct(x): return f"{x * 100:.0f}%" if x is not None else "—"
    print(f"{args.label_a}: {a.styled_count} paragraph(s)  ·  "
          f"{args.label_b}: {b.styled_count} paragraph(s)  ·  "
          f"{report.aligned_paras} compared")
    print(f"\n  same style {report.agree}  ·  different {report.different}  ·  "
          f"only {args.label_a} {len(report.only_a)}  ·  "
          f"only {args.label_b} {len(report.only_b)}")
    print(f"\n  style agreement {pct(report.agreement)}")
    print(f"\n  {md_path}\n  {json_path}")
    return 0


def cmd_eval(args) -> int:
    from .error_registry import shipped_keys
    from .eval.corpus import CorpusError, check_no_leakage, load_corpus
    from .eval.runner import run_eval
    from .eval.scorecard import write_scorecard
    from .eval.scorer import family, score_curve
    from .labels import DID_NOT_RUN, will_produce

    cfg, error_dir = _configure(args)
    out = Path(cfg.output_dir)
    setup_logging(out)

    try:
        cases = load_corpus(args.cases_dir)
        check_no_leakage(cases, error_dir)
    except (CorpusError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Include cases when any label in their family can be produced, including
    # sweep aliases absent from cfg.error_type_keys. Dropping those cases
    # would hide the sweeps from the score.
    known = shipped_keys(error_dir)
    kept, dropped = [], []
    for case in cases:
        producible = any(
            will_produce(cfg, label, known_types=known) != DID_NOT_RUN
            for label in family(case.error_type))
        (kept if producible else dropped).append(case)
    if dropped:
        print(f"Not scored — no pass in this run produces them: "
              f"{', '.join(sorted({c.error_type for c in dropped}))} "
              f"({len(dropped)} case(s))")
    cases = kept
    if not cases:
        print("error: no corpus cases match the passes this run performs.",
              file=sys.stderr)
        return 2

    provider = None
    mock = None
    if args.mock_findings:
        mock = _load_mocks(args.mock_findings)
        if mock is None:
            return 2
    else:
        try:
            provider = build_provider(cfg)
        except ProviderError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    n_err = sum(1 for c in cases if not c.is_clean)
    n_trap = len(cases) - n_err
    print(f"Scoring {len(cases)} cases ({n_err} seeded errors, {n_trap} traps) "
          f"across {len({c.error_type for c in cases})} type(s) on "
          f"{cfg.api.model}{' [mock]' if mock else ''}")

    def progress(done, total):
        print(f"  reviewing {done}/{total}", end="\r", flush=True)

    work = out / "eval"
    try:
        run = run_eval(cfg, cases, error_dir, work, provider=provider,
                       mock_findings=mock,
                       progress=None if mock else progress)
    except (IngestError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    curve = score_curve(run)
    json_path, md_path = write_scorecard(curve, work, default_gate=args.gate)

    card = curve[args.gate]
    p, r, f = card.micro
    def pct(x): return f"{x * 100:.0f}%" if x is not None else "—"
    print(f"\nAt the {args.gate} gate: "
          f"precision {pct(p)}, recall {pct(r)}, F1 {pct(f)}")
    print(f"  trap false-positive rate: {pct(card.trap_fp_rate)}")
    print(f"  anchor-failure rate:      {pct(card.anchor_failure_rate)}")
    if card.cost is not None:
        print(f"  cost: ${card.cost:.2f}")
    print(f"\n  {md_path}\n  {json_path}")
    return 0


# --- bespoke sweep ------------------------------------------------------------

def cmd_sweep(args) -> int:
    """Run one bespoke, book-scoped sweep over a manuscript — the $0
    deterministic front door for an author tic no shipped detector anticipates
    (doubled dialogue quotes, a home-made scene break). Dry-run by default:
    count, sample, and check idempotency, writing nothing. `--apply` writes the
    tracked-changes .docx, refusing a non-idempotent rule unless `--force`."""
    from .custom_sweep import RuleError, load_rule
    from .replay import zero_paid_passes
    from .sweeps import run_sweep_objects

    cfg = load_config(args.config)
    if getattr(args, "variant", None):
        cfg.variant = args.variant
    if args.out:
        cfg.output_dir = args.out
    # Disable all other analyses, including paid preparation stages, so only
    # this rule contributes findings.
    cfg.sweeps = []
    cfg.style.unclosed_quote_queries = False
    cfg.style.heading_title_case = False
    cfg.consistency.enabled = False
    cfg.spellcheck.enabled = False
    zero_paid_passes(cfg)

    try:
        sweep = load_rule(args.rule)
    except RuleError as e:
        print(f"error: --rule {args.rule}: {e}", file=sys.stderr)
        return 2

    out = Path(cfg.output_dir)
    setup_logging(out)
    error_dir = _resolve_error_dir(args.config)
    try:
        prepared = prepare(cfg, args.input, error_dir)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        findings, reports = run_sweep_objects(
            prepared.doc.paragraphs, [sweep], prepared.variant,
            ellipsis_style=cfg.style.ellipsis)
    except Exception as e:                # noqa: BLE001 - a rule's scan is user code
        print(f"error: rule '{sweep.key}' failed while scanning: {e}",
              file=sys.stderr)
        return 2
    report = reports[0]
    idempotent = report.remaining == 0

    print(f"\nBespoke sweep '{report.key}' — {report.name}")
    print(f"  {report.flagged} match(es) across "
          f"{len(prepared.doc.paragraphs)} paragraph(s).")
    if not idempotent:
        print(f"  ⚠ not idempotent: {report.remaining} match(es) remain after "
              f"the fix — the rule would fire again on its own output.")
    shown = findings[:max(0, args.sample)]
    for f in shown:
        print(f"    {f.para_id}: {f.original_text!r} → {f.corrected_text!r}")
    if len(findings) > len(shown):
        print(f"    … and {len(findings) - len(shown)} more")

    result = {"key": report.key, "name": report.name,
              "flagged": report.flagged, "remaining": report.remaining,
              "idempotent": idempotent, "paragraphs": len(prepared.doc.paragraphs),
              "applied": False}

    if not args.apply:
        print("\n  Dry run — nothing written. Re-run with --apply to write "
              "tracked changes.")
        if args.json:
            print(json.dumps(_envelope(findings=findings, usage=Usage(),
                                       model=cfg.api.model, extra=result),
                             ensure_ascii=False))
        return 0

    if report.flagged == 0:
        print("\n  Nothing to apply.")
        if args.json:
            print(json.dumps(_envelope(findings=findings, usage=Usage(),
                                       model=cfg.api.model, extra=result),
                             ensure_ascii=False))
        return 0
    if not idempotent and not args.force:
        print(f"\nerror: refusing to apply a non-idempotent rule "
              f"({report.remaining} match(es) remain after its own fix). Fix "
              f"the rule, or pass --force to apply it anyway.", file=sys.stderr)
        return 3

    # The bespoke findings ride the sweep channel finish() already applies; zero
    # the other prepared channels so ONLY this rule lands.
    prepared.sweep_findings = findings
    prepared.sweep_reports = reports
    prepared.consistency_findings = []
    usage = Usage()
    try:
        outputs = finish(prepared, [], usage, cfg, out_dir=out,
                         source_path=args.input)
    except (IngestError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    result["applied"] = True
    print(f"\n{_result_line(outputs)}.")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json, out / "run.log"):
        if p is not None:
            print(f"  {p}")
    if args.json:
        result["reviewed_path"] = (str(outputs.reviewed_path)
                                   if outputs.reviewed_path else None)
        payload = json.loads(outputs.findings_json.read_text("utf-8"))
        print(json.dumps(_envelope(findings=payload.get("findings", []),
                                   usage=usage, model=cfg.api.model,
                                   extra=result), ensure_ascii=False))
    return 0


# --- import-findings / replay --------------------------------------------------

def _prepared_paragraphs(args):
    """Ingest via the same prepare() every other $0 verb uses, so the
    paragraphs these reports see are the canonical (post-normalization) text
    with the same ids the rest of the rack anchors to."""
    cfg, error_dir = _configure(args)
    setup_logging(cfg.output_dir)
    prepared = prepare(cfg, args.input, error_dir)
    return prepared.doc.paragraphs


def cmd_tense(args) -> int:
    from .tensecheck import profile as tense_profile
    from .tensecheck import render as tense_render
    try:
        paragraphs = _prepared_paragraphs(args)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    prof = tense_profile(paragraphs)
    if args.json:
        print(json.dumps(prof.to_json(), indent=2))
    else:
        print(tense_render(prof))
    return 0


def cmd_cites(args) -> int:
    from .citecheck import check as cite_check
    from .citecheck import render as cite_render
    try:
        paragraphs = _prepared_paragraphs(args)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    report = cite_check(paragraphs)
    if args.json:
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(cite_render(report))
    return 0


def cmd_import_findings(args) -> int:
    """`docproof import-findings`: the front door for injecting a findings file
    produced OUTSIDE docproof (a subagent flight, a hand-built JSON file) into a
    $0 tracked-changes deliverable. See docproof/replay.py for the machinery
    and, in particular, why every row's error_type is forced onto an
    edit-channel type rather than trusted."""
    return _import_or_replay(args, remap_unchanneled=True, id_prefix="import")


def cmd_replay(args) -> int:
    """`docproof replay`: rebuild a deliverable at $0 from a findings checkpoint
    or a finished run's findings.json — docproof's OWN prior output, so unlike
    import-findings, each row's error_type is trusted and left alone. See
    docproof/replay.py."""
    return _import_or_replay(args, remap_unchanneled=False, id_prefix="replay")


def _import_or_replay(args, *, remap_unchanneled: bool, id_prefix: str) -> int:
    from .replay import (DEFAULT_IMPORT_TYPE, WordCountDelta,
                        load_findings_file, rebuild_from_rows)

    cfg = load_config(args.config)
    if args.out:
        cfg.output_dir = args.out
    out = Path(cfg.output_dir)
    setup_logging(out)
    error_dir = _resolve_error_dir(args.config)

    try:
        rows = load_findings_file(args.findings)
    except (OSError, ValueError) as e:
        print(f"error: {args.findings}: {e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"error: {args.findings}: not valid JSON ({e})", file=sys.stderr)
        return 2

    anchor_mode = getattr(args, "anchor", None) or "source"
    settled_note = ""
    if anchor_mode == "accepted":
        # Rows quoted from the ACCEPTED text of an earlier build: translate
        # them to the source through that build's edit map and fold each into
        # the build's own kept rows (a row inside an owned span revises that
        # owner). The result is the whole deliverable's row set, so the
        # rebuild below replays it in full.
        from galley.settle import fold_accepted_rows
        run_dir = getattr(args, "run", None)
        if not run_dir:
            print("error: --anchor accepted needs --run RUN (the build whose "
                  "accepted text the rows quote)", file=sys.stderr)
            return 2
        try:
            folded = fold_accepted_rows(Path(run_dir), rows, cfg=cfg,
                                        manuscript=args.manuscript,
                                        error_dir=error_dir)
        except (OSError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        rows = folded.rows
        settled_note = (f"  --anchor accepted: {folded.absorbed} row(s) revise "
                        f"an applied edit, {folded.added} land on untouched "
                        f"text, {folded.queried} became queries, "
                        f"{folded.dropped} dropped; replaying {len(rows)} "
                        f"row(s) in all")
        for line in folded.notes[:10]:
            settled_note += f"\n    {line}"

    try:
        result = rebuild_from_rows(
            cfg, manuscript=args.manuscript, rows=rows, error_dir=error_dir,
            remap_unchanneled=remap_unchanneled, id_prefix=id_prefix,
            after_sweeps=bool(getattr(args, "after_sweeps", False)),
            dry_run=bool(args.dry_run))
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except WordCountDelta as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if result.reanchored:
        print(f"  --after-sweeps: re-anchored {result.reanchored} row(s) from "
              f"post-sweep to pre-sweep text")
    for hit in result.artifacts:
        print(f"  artifact scan: {hit.get('pattern')} in {hit.get('para_id')}"
              + (f" — dropped the later row {hit.get('dropped_id')}"
                 if hit.get("resolved") else " — UNRESOLVED"))
    findings, rejects, remapped, checked, tally = (
        result.findings, result.rejects, result.remapped, result.checked,
        result.tally)

    verb = "import-findings" if remap_unchanneled else "replay"
    print(f"\n`docproof {verb}`: {len(rows)} row(s) read — {len(findings)} "
          f"usable, {len(rejects)} malformed"
          + (f", {remapped} remapped onto '{DEFAULT_IMPORT_TYPE}' (no reliable "
             f"channel on the row)" if remap_unchanneled else "") + ".")
    if settled_note:
        print(settled_note)
    for status in sorted(tally):
        print(f"  {status:<24} {tally[status]}")
    if rejects:
        print("\n  malformed row(s):")
        for r in rejects[:10]:
            print(f"    [{r['index']}] {r['reason']}")
        if len(rejects) > 10:
            print(f"    … and {len(rejects) - 10} more")

    extra = {"rows": len(rows), "usable": len(findings),
             "malformed": rejects, "remapped": remapped, "tally": tally}

    if args.dry_run:
        print("\n  Dry run — nothing written. Re-run without --dry-run to "
              "write the deliverable.")
        if args.json:
            print(json.dumps(_envelope(findings=checked, usage=Usage(),
                                       model=cfg.api.model, extra=extra),
                             ensure_ascii=False))
        return 0

    outputs = result.outputs
    print(f"\n{_result_line(outputs)}.")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json, out / "run.log"):
        if p is not None:
            print(f"  {p}")
    if args.json:
        payload = json.loads(outputs.findings_json.read_text("utf-8"))
        print(json.dumps(_envelope(findings=payload.get("findings", []),
                                   usage=Usage(), model=cfg.api.model,
                                   extra=extra), ensure_ascii=False))
    return 0


# --- merge desk -----------------------------------------------------------------

def _load_findings_file(path: Path) -> list[dict] | None:
    """A findings JSON file: a bare array, or a `{"findings": [...]}` envelope
    (what `findings.json` and a run checkpoint both write). None on any
    problem, after printing why."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        print(f"error: could not read {path}: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"error: {path}: {e}", file=sys.stderr)
        return None
    rows = raw.get("findings", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        print(f"error: {path}: must be a JSON array of finding objects, or a "
              "{'findings': [...]} envelope", file=sys.stderr)
        return None
    return rows


_LEDGER_VERBS = {"rewrite_clean": "copy-edit rewrite (clean) subsumes",
                 "mechanical_default": "mechanical wins over",
                 "cluster_atomic": "cluster claims its span for",
                 "same_lane_overlap": "keeps the span (same-lane overlap) over",
                 "mechanical_strict": "mechanical wins overlap (strict) over"}


def cmd_merge(args) -> int:
    """The merge desk: reconcile a mechanical/proofread findings set with a
    copy-edit/rewrite findings set into one deliverable with two tracked-
    changes authors (docproof/mergedesk.py). $0, like `docproof sweep`: every
    paid pass is silenced, and the claim rules run over the deterministic
    sweeps + LanguageTool, never a provider. Writes by default; `--dry-run`
    only prints the claim ledger."""
    from . import mergedesk
    from .replay import zero_paid_passes

    cfg = load_config(args.config)
    if getattr(args, "variant", None):
        cfg.variant = args.variant
    if args.out:
        cfg.output_dir = args.out
    # Disable paid passes but retain built-in sweeps: house-style spans must
    # take priority over either lane when merging.
    cfg.consistency.enabled = False
    cfg.spellcheck.enabled = False
    zero_paid_passes(cfg)

    mech_raw = _load_findings_file(Path(args.mechanical))
    if mech_raw is None:
        return 2
    ce_raw: list[dict] = []
    if args.copyedit:
        ce_raw = _load_findings_file(Path(args.copyedit))
        if ce_raw is None:
            return 2

    out = Path(cfg.output_dir)
    setup_logging(out)
    error_dir = _resolve_error_dir(args.config)
    try:
        prepared = prepare(cfg, args.input, error_dir,
                           dry_run=bool(getattr(args, "dry_run", False)))
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        mechanical = mergedesk.tag_lane(mech_raw, mergedesk.MECHANICAL)
        copyedit = mergedesk.tag_lane(ce_raw, mergedesk.COPYEDIT)
    except mergedesk.MergeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    merged = mergedesk.merge_lanes(
        mechanical, copyedit, prepared.doc, min_confidence=cfg.min_confidence,
        query_types=prepared.query_types, format_types=prepared.format_types,
        edit_guard=cfg.edit_guard, variant=prepared.variant,
        ellipsis_style=cfg.style.ellipsis)
    merged, artifacts = mergedesk.iterate_until_clean(
        merged, prepared.doc, min_confidence=cfg.min_confidence,
        query_types=prepared.query_types, format_types=prepared.format_types,
        edit_guard=cfg.edit_guard)

    print(f"\nMerge desk: {len(mechanical)} mechanical + {len(copyedit)} "
          f"copy-edit finding(s) → {len(merged.ledger)} contested span(s).")
    for r in merged.ledger:
        tail = f" — {r.reason}" if r.reason else ""
        loser = f" {r.loser_id}" if r.loser_id else ""
        print(f"  {r.para_id} [{r.start}:{r.end}]  {_LEDGER_VERBS.get(r.rule, r.rule)}"
              f"{loser}{tail}  (winner {r.winner_id}, {r.winner_lane})")
    if artifacts:
        print(f"\n{len(artifacts)} merged-result artifact(s):")
        for hit in artifacts:
            state = f"dropped {hit.dropped_id}" if hit.resolved else "UNRESOLVED"
            print(f"  {hit.para_id or '?'}: {hit.pattern} — {state}")
    unresolved = [h for h in artifacts if not h.resolved]

    result = {
        "findings": [dataclasses.asdict(f) for f in merged.validated],
        "cost": {"total": 0.0},
        "ledger": {"claims": [r.to_json() for r in merged.ledger]},
        "claims": {"contested": len(merged.ledger),
                  "artifacts": [h.to_json() for h in artifacts]},
        "checkpoint": None,
    }

    if args.dry_run:
        print("\n  Dry run — nothing written. Re-run without --dry-run to "
              "write the deliverable.")
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        return 3 if unresolved else 0

    if unresolved:
        print("\nerror: unresolved merged-result artifact(s) — refusing to "
              "write. Re-run with --dry-run to inspect them, or fix the "
              "finding(s) that contribute to them.", file=sys.stderr)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        return 3

    usage = Usage()
    try:
        outputs = finish(prepared, merged.findings, usage, cfg, out_dir=out,
                         source_path=args.input)
    except (IngestError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"\n{_result_line(outputs)}.")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json, out / "run.log"):
        if p is not None:
            print(f"  {p}")
    if outputs.findings_json is not None:
        try:
            written = json.loads(outputs.findings_json.read_text(encoding="utf-8"))
            result["findings"] = written.get("findings", written) \
                if isinstance(written, dict) else written
        except (OSError, json.JSONDecodeError):
            pass
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    return 0


# --- galley -------------------------------------------------------------------

def cmd_galley(args) -> int:
    """Dispatch the `galley` sub-verbs. Kept apart from the review commands: these
    read a finished run rather than producing one."""
    return {"audit": _galley_audit, "verify": _galley_verify,
            "letter": _galley_letter,
            "seed": _galley_seed, "score": _galley_score,
            "ask": _galley_ask,
            "calibrate": _galley_calibrate,
            "flights": _galley_flights,
            "profile": cmd_galley_profile,
            "genre-pack": cmd_galley_genre_pack,
            "export-judgments": _galley_export_judgments,
            "import-judgments": _galley_import_judgments,
            "routes": _galley_routes,
            "approve": _galley_approve,
            "certify": _galley_certify,
            "triage-nouns": _galley_triage_nouns,
            "intent-zones": _galley_intent_zones,
            "ledger": _galley_ledger,
            "state": _galley_state,
            "settle": _galley_settle,
            "residuals": _galley_residuals,
            "drive": _galley_drive,
            "agent": _galley_agent,
            "journal": _galley_journal,
            "outcome": _galley_outcome}[args.galley_cmd](args)


def _galley_journal(args) -> int:
    """`docproof galley journal`: render the decision log (galley/journal.py).

    $0, no model, no clock: the same artifacts always render the same
    document, so a log can be regenerated whenever someone asks how a decision
    was made."""
    from galley.journal import render_journal

    run = Path(args.run)
    if not run.is_dir():
        print(f"error: no run directory at {run}", file=sys.stderr)
        return 2
    try:
        text = render_journal(run, workspace=args.workspace, book=args.book,
                              generated_at=args.at)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out} — {len(text.splitlines())} line(s)")
    else:
        print(text)
    return 0


def _galley_agent(args) -> int:
    """`docproof galley agent`: the long-running proofing agent (galley/agent.py).

    Exit 0 for a poll that ran (whatever it found), 2 for a setup problem the
    message names — a credentials file that is missing, readable by other
    accounts, or short of a key."""
    from galley import agent as ga

    root = Path(args.workspace_root).expanduser() if args.workspace_root \
        else Path("~/galley-workspaces").expanduser()
    env_file = Path(args.env_file).expanduser() if args.env_file \
        else Path(ga.DEFAULT_ENV_FILE).expanduser()

    if args.uninstall:
        removed = ga.uninstall()
        where = ga.service_path()
        print(f"Removed {where}." if removed
              else f"There was no agent service at {where}.")
        return 0

    if args.install:
        try:
            where = ga.install(workspace_root=root, env_file=env_file,
                               poll_interval_s=args.poll_interval
                               or ga.DEFAULT_POLL_INTERVAL_S)
        except ga.AgentError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"Installed and started the proofing agent.\n"
              f"  service  {where}\n"
              f"  polls    {env_file} for credentials\n"
              f"  log      {root / ga.LOG_NAME}\n"
              f"  ledger   {root / ga.LEDGER_NAME}")
        return 0

    try:
        env = ga.read_env(env_file)
    except ga.AgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    # Set process credentials too, including Google authentication on hosts
    # without keyring.
    ga.apply_env(env)

    agent = ga.Agent(env=env, workspace_root=root,
                     budget_usd=args.budget,
                     drive_folder_override=args.drive_folder_id,
                     poll_interval_s=args.poll_interval
                     or ga.DEFAULT_POLL_INTERVAL_S)

    if args.status:
        state = agent.status()
        print(f"Galley agent — {state['app']}")
        print(f"  workspaces {state['workspace_root']}")
        print(f"  ledger     {state['ledger']}")
        if not state["books"]:
            print("  nothing claimed yet.")
        for file_id, entry in sorted(state["books"].items(),
                                     key=lambda kv: kv[1].get("updated_at", "")):
            print(f"  [{entry.get('state', '?'):8}] "
                  f"{entry.get('name', file_id)}"
                  f"{'  ' + entry.get('outcome', '') if entry.get('outcome') else ''}")
            if entry.get("reason"):
                print(f"             {entry['reason'][:160]}")
        if state["pending"]:
            print(f"  {len(state['pending'])} book(s) claimed but unfinished; "
                  f"the next poll resumes them.")
        if args.json:
            print(json.dumps(state, ensure_ascii=False))
        return 0

    _logging_to(root / ga.LOG_NAME)
    if args.once:
        report = agent.poll_once()
        print(f"Looked at {report.looked_at} awaiting book(s)."
              + (f" Ran {report.claimed}: {report.outcome}."
                 if report.claimed else ""))
        for skipped in report.skipped:
            print(f"  skipped {skipped}")
        if args.json:
            print(json.dumps(report.to_json(), ensure_ascii=False))
        return 0

    agent.run_forever()
    return 0


def _logging_to(path: Path) -> None:
    """Say what the agent is doing, on stdout and in the log file.

    A service manager captures stdout to that same file, so this is belt and
    braces for a run started by hand — and it is the only way `--once` from a
    terminal says anything at all."""
    import logging as _logging
    path.parent.mkdir(parents=True, exist_ok=True)
    root = _logging.getLogger("docproof")
    if root.level == _logging.NOTSET or root.level > _logging.INFO:
        root.setLevel(_logging.INFO)
    if not any(isinstance(h, _logging.StreamHandler) for h in root.handlers):
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(handler)


def _galley_drive(args) -> int:
    """`docproof galley drive`: the unattended driver (galley/driver.py).

    Sequences the practitioner phases, each as its own headless session, under
    a stated plan-gate policy; stops on the first failure with runs/outcome.json
    = needs_human; hands the certified deliverable to DocWatch. Exit 0 when the
    run finished `done`, 7 when it stopped needing a human, 2 on a setup error
    the message names."""
    from galley import driver as gd

    mechanical_only = not getattr(args, "copyedit", False)
    if getattr(args, "print_prompt", None):
        try:
            print(gd.phase_prompt(args.print_prompt, Path(args.book).name,
                                  mechanical_only=mechanical_only))
        except gd.DriverError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0

    kwargs: dict = {}
    if args.workspace_root:
        kwargs["workspace_root"] = Path(args.workspace_root)
    if args.budget is not None:
        kwargs["budget_usd"] = args.budget
    if args.model:
        kwargs["model"] = args.model
    if args.permission_mode:
        kwargs["permission_mode"] = args.permission_mode
    if args.wrapbin:
        kwargs["wrapbin"] = Path(args.wrapbin)
    if args.reply_timeout is not None:
        kwargs["reply_timeout_s"] = args.reply_timeout * 3600.0
    if args.poll_interval is not None:
        kwargs["poll_interval_s"] = args.poll_interval
    if args.handoff:
        kwargs["handoff_dir"] = Path(args.handoff)
    if args.max_turns is not None:
        kwargs["max_turns"] = args.max_turns
    if args.timeout is not None:
        kwargs["timeout_s"] = args.timeout * 3600.0
    for name, flag, cast, scale in (
            ("max_turns_by_phase", "phase_max_turns", int, 1.0),
            ("timeout_by_phase", "phase_timeout", float, 3600.0)):
        pairs: dict = {}
        for spec in getattr(args, flag) or []:
            phase, _, value = str(spec).partition("=")
            if not value:
                print(f"error: --{flag.replace('_', '-')} wants PHASE=VALUE, "
                      f"got {spec!r}", file=sys.stderr)
                return 2
            try:
                pairs[phase.strip()] = cast(value) * scale
            except ValueError:
                print(f"error: --{flag.replace('_', '-')} {spec!r}: "
                      f"{value!r} is not a number", file=sys.stderr)
                return 2
        if pairs:
            kwargs[name] = ({k: int(v) for k, v in pairs.items()}
                            if cast is int else pairs)
    for opt, key in (("settle_rounds", "settle_rounds"),
                     ("settle_quiet_floor", "settle_quiet_floor"),
                     ("settle_quiet_share", "settle_quiet_share")):
        value = getattr(args, opt)
        if value is not None:
            kwargs[key] = value

    drv = gd.Driver(
        book=Path(args.book).expanduser(), slug=args.slug,
        approve=args.approve, mechanical_only=mechanical_only,
        start_phase=args.from_phase, only_phases=args.phases,
        drive_folder_id=args.drive_folder_id,
        state_gate=not args.no_state_gate,
        question_gate=not args.no_question_gate, **kwargs)

    if args.dry_run:
        try:
            ws = gd.seed_workspace(drv.book, drv.slug,
                                   workspace_root=drv.workspace_root)
            phases = gd.select_phases(mechanical_only=mechanical_only,
                                      start=args.from_phase, only=args.phases)
        except gd.DriverError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"workspace {ws}")
        print(f"phases: {' -> '.join(phases)}")
        print(f"gate: --approve {args.approve} at ${drv.budget_usd:.2f}"
              f"{' (mechanical only)' if mechanical_only else ''}")
        if args.json:
            print(json.dumps({"workspace": str(ws), "phases": phases,
                              "approve": args.approve,
                              "budget_usd": drv.budget_usd,
                              "mechanical_only": mechanical_only},
                             ensure_ascii=False))
        return 0

    try:
        result = drv.run()
    except gd.DriverError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.to_json(), ensure_ascii=False))
    return result.exit_code


def _paragraph_ids(spec: str | None) -> list[str] | None:
    """`--paragraphs`: comma-separated ids, or @FILE with one per line."""
    if not spec:
        return None
    if spec.startswith("@"):
        text = Path(spec[1:]).read_text(encoding="utf-8")
        return [ln.strip() for ln in text.splitlines() if ln.strip()]
    return [x.strip() for x in spec.split(",") if x.strip()]


def _galley_settle(args) -> int:
    """`docproof galley settle`: the residual-settlement loop (galley/settle.py).
    Reads the run's verify artifacts, closes every open item through the
    engine, rebuilds at $0, re-verifies the touched paragraphs, repeats until
    nothing is open or --rounds is spent, then writes settlement.json and
    outcome.json. Exit 0 when every item is terminal; nonzero only on an
    engine error."""
    from galley.outcome import (DEFAULT_DONE_VALUE, DEFAULT_NEEDS_HUMAN_VALUE,
                                assess)
    from galley.settle import (DEFAULT_ROUNDS as DEFAULT_SETTLE_ROUNDS,
                               HARD_MAX_ROUNDS, SettleOptions, Settler,
                               kept_rows, open_items, resolve)
    from .providers import cost_of_usage

    run = Path(args.run)
    if not (run / "findings.json").exists():
        print(f"error: no findings.json in {run}", file=sys.stderr)
        return 2
    from galley.verify import deliverable_docx
    if deliverable_docx(run) is None:
        print(f"error: no manuscript .docx in {run} — settle reads the "
              f"ACCEPTED deliverable", file=sys.stderr)
        return 2
    try:
        cfg = _effective_cfg(args)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    setup_logging(run)
    error_dir = _resolve_error_dir(args.config)
    context = ""
    if args.context:
        try:
            context = Path(args.context).read_text(encoding="utf-8")
        except OSError as e:
            print(f"error: --context {args.context}: {e}", file=sys.stderr)
            return 2

    # Unset rounds uses the default, except until-clean leaves its own
    # 12-round ceiling in effect.
    _rounds = args.rounds if args.rounds is not None \
        else (0 if args.until_clean else DEFAULT_SETTLE_ROUNDS)

    items = open_items(run)
    if args.dry_run:
        print(f"dry run — {len(items)} open item(s) in {run}")
        try:
            from galley.settle import _source_paragraphs, load_envelope
            from docproof import editmap as _em
            env = load_envelope(run)
            working, _ = kept_rows(env.get("findings") or [])
            source, _zones = _source_paragraphs(cfg, args.source, error_dir)
            em = _em.load_or_build(run, source, env.get("findings") or [])
            from galley.verify import paragraph_views
            _o, accepted = paragraph_views(run)
            for it in items:
                why = resolve(it, em, accepted, working)
                owner = it.owner_finding_id or "-"
                print(f"  {it.kind:12} {it.para_id:10} {it.quote[:40]!r:44} "
                      f"owner={owner:10} {why or 'resolvable'}")
        except Exception as e:                              # noqa: BLE001
            print(f"  (owner resolution unavailable: {e})")
        print(f"\nupper bound: {len(items)} judge call(s) + a delta verify per "
              f"round × {_rounds or HARD_MAX_ROUNDS} round(s); nothing "
              f"written.")
        return 0

    default_model = cfg.continuity.model or cfg.api.model
    try:
        engine, provider, model = _resolve_engine(args, cfg,
                                                 default_model=default_model)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:                                  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 2
    if engine == "provider":
        guard = _galley_spend_guard(args, cfg, [model], projected_usd=None)
        if guard is not None:
            return guard

    # Preserve mechanical-only approval scope during settlement.
    mechanical_only = bool(getattr(args, "mechanical_only", False))
    if getattr(args, "approval", None):
        try:
            manifest = json.loads(Path(args.approval).read_text("utf-8"))
            mechanical_only = mechanical_only or bool(
                manifest.get("mechanical_only"))
        except (OSError, ValueError, AttributeError) as e:
            print(f"error: --approval {args.approval}: {e}", file=sys.stderr)
            return 2
    opts = SettleOptions(rounds=max(0, int(_rounds)), engine=engine,
                         model=model, context=context,
                         verify_delta=not args.no_verify,
                         until_clean=bool(args.until_clean),
                         quiet_floor=int(args.quiet_floor),
                         quiet_share=float(args.quiet_share),
                         max_turns=int(args.max_turns),
                         propagate=not args.no_propagate,
                         mechanical_only=mechanical_only,
                         concurrency=_lane_concurrency(cfg, engine, model))
    settler = Settler(run, cfg=cfg, manuscript=args.source,
                      error_dir=error_dir, provider=provider, options=opts)
    from .agent_lane import AgentLaneUnavailable
    try:
        result = settler.run()
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except AgentLaneUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        print("  the run's settlement.json records the rounds completed so "
              "far; re-run the same command once the lane is available.",
              file=sys.stderr)
        return 2
    cost = cost_of_usage(result.usage, fallback_model=model or None) or 0.0
    _galley_over_budget(args, cost)

    outcome = assess(run, done_value=args.done_value or DEFAULT_DONE_VALUE,
                     needs_human_value=(args.needs_human_value
                                        or DEFAULT_NEEDS_HUMAN_VALUE))
    outcome.save(run)
    st = result.settlement
    counts = st.counts()
    print(f"\nsettle: {len(items)} open item(s) at start → "
          f"{len(st.latest())} settled in {st.rounds} round(s) "
          f"({', '.join(f'{k}={v}' for k, v in sorted(counts.items())) or 'none'}), "
          f"{len(st.open)} open; engine={engine} "
          f"({result.usage.api_calls} model call(s), ${cost:.4f})"
          f"{' — mechanical only' if mechanical_only else ''}.")
    for note in st.notes[-(st.rounds + 1):]:
        print(f"  {note}")
    print(f"  outcome: {outcome.outcome} — {outcome.reason[:200]}")
    print(f"\n  {run / 'settlement.json'}\n  {run / 'outcome.json'}")
    if args.json:
        print(json.dumps(_envelope(findings=(), usage=result.usage,
                                   model=model or cfg.api.model,
                                   extra={"settlement": st.to_json(),
                                          "outcome": outcome.to_json()}),
                         ensure_ascii=False))
    return 0 if not st.open else 1


def _galley_residuals(args) -> int:
    from galley.settle import kept_rows, open_items, resolve
    run = Path(args.run)
    items = open_items(run)
    resolved = False
    if getattr(args, "source", None):
        try:
            cfg = _effective_cfg(args)
            from galley.settle import _source_paragraphs, load_envelope
            from docproof import editmap as _em
            env = load_envelope(run)
            working, _ = kept_rows(env.get("findings") or [])
            error_dir = _resolve_error_dir(args.config)
            source, _z = _source_paragraphs(cfg, args.source, error_dir)
            em = _em.load_or_build(run, source, env.get("findings") or [])
            from galley.verify import paragraph_views
            _o, accepted = paragraph_views(run)
            for it in items:
                why = resolve(it, em, accepted, working)
                it.raw["resolution"] = why or "resolvable"
            resolved = True
        except Exception as e:                              # noqa: BLE001
            print(f"note: owner resolution unavailable ({e})", file=sys.stderr)
    print(f"{len(items)} open item(s) in {run}"
          + ("" if resolved else " (pass --source to resolve owners)"))
    for it in items:
        owner = it.owner_finding_id or "-"
        print(f"  {it.kind:12} {it.para_id:10} sev={it.severity:6} "
              f"{it.quote[:40]!r} → {it.suggestion[:40]!r} owner={owner} "
              f"{it.raw.get('resolution', '')}")
    if args.json:
        print(json.dumps({"run": str(run), "open": [
            {**it.to_json(), "resolution": it.raw.get("resolution")}
            for it in items]}, ensure_ascii=False))
    return 0


def _galley_outcome(args) -> int:
    from galley.outcome import (DEFAULT_DONE_VALUE, DEFAULT_NEEDS_HUMAN_VALUE,
                                Outcome, Thresholds, assess, hubspot_fields)
    run = Path(args.run)
    done_value = args.done_value or DEFAULT_DONE_VALUE
    needs_value = args.needs_human_value or DEFAULT_NEEDS_HUMAN_VALUE
    if args.set:
        if not args.reason:
            print("error: --set needs --reason", file=sys.stderr)
            return 2
        prior = assess(run, done_value=done_value,
                       needs_human_value=needs_value)
        oc = Outcome(outcome=args.set, reason=args.reason,
                     evidence=prior.evidence,
                     hubspot=hubspot_fields(args.set, done_value=done_value,
                                            needs_human_value=needs_value),
                     set_by="human")
    else:
        th = Thresholds()
        if args.rewrite_share is not None:
            th.rewrite_share = args.rewrite_share
        if args.edit_density is not None:
            th.edit_density_per_kword = args.edit_density
        source_paras = None
        if getattr(args, "source", None):
            try:
                cfg = load_config(args.config)
                from galley.settle import _source_paragraphs
                source_paras, _z = _source_paragraphs(
                    cfg, args.source, _resolve_error_dir(args.config))
            except Exception as e:                          # noqa: BLE001
                print(f"note: could not read the source ({e}); counting "
                      f"from the deliverable", file=sys.stderr)
        oc = assess(run, thresholds=th, source_paras=source_paras,
                    done_value=done_value, needs_human_value=needs_value)
    path = oc.save(run)
    print(f"outcome: {oc.outcome} — {oc.reason}")
    ev = oc.evidence
    print(f"  {ev.get('words', 0)} words, {ev.get('applied_edits', 0)} edits "
          f"({ev.get('edit_density_per_kword', 0.0):.1f}/1k), rewrite share "
          f"{ev.get('rewrite_share', 0.0):.0%}, unresolved "
          f"{ev.get('unresolved_queries', 0)}, damage {ev.get('edit_damage', 0)}")
    print(f"  hubspot: {oc.hubspot.get('property')} = {oc.hubspot.get('value')!r}"
          f" (object {oc.hubspot.get('object')})")
    print(f"  {path}")
    if args.json:
        print(json.dumps(oc.to_json(), ensure_ascii=False))
    return 0


def _galley_ask(args) -> int:
    """Push one escalation question over the shared DocWatch Gmail pipe.

    Loud by design: a question that cannot send exits 2 with the fix, so the
    practitioner KNOWS to fall back — log it in QUESTIONS.md and stop the
    blocked thread — instead of waiting on a reply that can never come."""
    import sys

    from app.watch.notify import send_question
    from app.watch.settings import default_watch_home

    if args.body:
        body = args.body
    elif args.body_file:
        body = Path(args.body_file).read_text("utf-8")
    else:
        body = sys.stdin.read()
    if not body.strip():
        print("galley ask: an empty question helps nobody — say what you were "
              "doing, the question, your recommended answer, and what is "
              "blocked.", file=sys.stderr)
        return 2
    try:
        to = send_question(default_watch_home(), args.subject, body,
                           book=args.book)
    except ValueError as e:
        print(f"galley ask: could not send — {e}\nFall back: append the "
              f"question to QUESTIONS.md and stop the blocked work.",
              file=sys.stderr)
        return 2
    except Exception as e:                    # noqa: BLE001 - Gmail refusals land here
        print(f"galley ask: the send failed ({e}).\nFall back: append the "
              f"question to QUESTIONS.md and stop the blocked work.",
              file=sys.stderr)
        return 2
    print(f"Question sent to {to}.")
    return 0


def _galley_spend_guard(args, cfg, models, *,
                        projected_usd: float | None = None) -> int | None:
    """The approval/budget gate for the galley verbs that spend outside the
    review ladder (audit, verify, flights). `_approval_guard` verifies a
    REVIEW's routes from the config's own model fields; these verbs name their
    models on the command line, so the same manifest is checked against the
    models they will actually call: each must sit inside the approval's
    allowed models/providers, and the planned spend (`--budget`, else the
    verb's own projection) must fit under the approved cap. Independently, a
    `--budget` refuses a projection above it. Returns an exit code to abort
    with (5, like `review --approval`), or None to proceed."""
    from galley.manifest import Deviation
    from .providers import provider_for

    approval = getattr(args, "approval", None)
    budget = getattr(args, "budget", None)
    if not approval and budget is None:
        return None
    devs: list[Deviation] = []
    if approval:
        try:
            manifest = json.loads(Path(approval).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: could not read approval {approval}: {e}",
                  file=sys.stderr)
            return 5
        allowed_models = set(manifest.get("allowed_models") or [])
        allowed_providers = set(manifest.get("allowed_providers") or [])
        for m in dict.fromkeys(m for m in models if m):
            prov = provider_for(m, cfg.api.provider)
            if allowed_models and m not in allowed_models:
                devs.append(Deviation(
                    "model_not_approved",
                    f"this verb would call {m} ({prov}), not in the approved "
                    f"set"))
            if allowed_providers and prov not in allowed_providers:
                devs.append(Deviation(
                    "provider_not_approved",
                    f"this verb would send material to {prov} (for {m}), a "
                    f"provider the approval does not allow"))
        cap = float(manifest.get("max_spend_usd", 0.0))
        planned = budget if budget is not None else projected_usd
        if planned is not None and planned > cap:
            devs.append(Deviation(
                "budget_over_cap",
                f"planned spend ${planned:.2f} exceeds the approved ${cap:.2f}"))
    if budget is not None and projected_usd is not None \
            and projected_usd > budget:
        devs.append(Deviation(
            "budget_over_cap",
            f"projected spend ${projected_usd:.2f} exceeds --budget "
            f"${budget:.2f}"))
    if devs:
        src = approval or "--budget"
        print(f"REFUSED: this run deviates from {src} —", file=sys.stderr)
        for d in devs:
            print(f"  - {d.kind}: {d.detail}", file=sys.stderr)
        if approval:
            print("  re-approve with `docproof galley approve` if the change "
                  "is intended.", file=sys.stderr)
        return 5
    if approval:
        print(f"approval {approval}: model routes and budget match — "
              f"proceeding.")
    return None


def _galley_over_budget(args, cost: float) -> None:
    """After a spend: say so, loudly, when it overran the --budget. The money
    is gone either way — this exists so the loop's log never reads as clean."""
    budget = getattr(args, "budget", None)
    if budget is not None and cost > budget:
        print(f"OVER BUDGET: spent ${cost:.4f} against --budget "
              f"${budget:.2f}", file=sys.stderr)


def _galley_audit(args) -> int:
    from galley.audit import audit_run, chapter_densities, read_findings
    from galley.ingest import manuscript_from_source
    from .providers import cost_of_usage

    cfg = load_config(args.config)
    results = Path(args.results)
    if not (results / "findings.json").exists():
        print(f"error: no findings.json in {results} — point the results "
              f"argument at a finished run's output directory", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else results
    setup_logging(out)

    # Resolve the provider for the audit model, which may differ from the
    # detector provider.
    model = args.model or cfg.continuity.model or cfg.api.model
    guard = _galley_spend_guard(args, cfg, [model])
    if guard is not None:
        return guard
    try:
        provider = build_provider(cfg, model=model)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        ms = manuscript_from_source(args.source, cfg)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    usage = Usage()
    hyps = audit_run(results, ms, provider, model, usage,
                     n_samples=args.n_samples, seed=args.seed)
    densities = chapter_densities(read_findings(results), ms)
    cost = cost_of_usage(usage, fallback_model=model) or 0.0
    _galley_over_budget(args, cost)

    payload = {
        "results_dir": str(results),
        "source": args.source,
        "model": model,
        "ran": True,
        "n_samples": args.n_samples,
        **hyps.to_json(),
        "densities": [d.to_json() for d in densities],
        "cost": _cost_field(usage, model),
    }
    out.mkdir(parents=True, exist_ok=True)
    audit_path = out / "audit.json"
    audit_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                          encoding="utf-8")

    print(f"\n{len(hyps)} hypothesis/-es about likely missed errors "
          f"({usage.api_calls} model call(s), ${cost:.4f}).")
    quiet = sorted(densities, key=lambda d: (d.per_1k, d.index))[:5]
    if quiet:
        print("  quietest chapters (a miss most likely hides here):")
        for d in quiet:
            print(f"    ch {d.index:>3}  {d.per_1k:>5.2f}/1k  "
                  f"{d.findings:>4} finding(s) in {d.words:>6} words  "
                  f"{d.title[:40]}")
    for h in hyps[:10]:
        why = h.why[:70] + ("…" if len(h.why) > 70 else "")
        print(f"  - ch {h.chapter}: {h.error_class} ({h.confidence}) — {why}")
    print(f"\n  {audit_path}")
    if args.json:
        # Audit hypotheses belong in extra; findings stays empty.
        print(json.dumps(_envelope(findings=(), usage=usage, model=model,
                                   extra=payload), ensure_ascii=False))
    return 0


def _galley_verify(args) -> int:
    from galley.verify import (DEFAULT_CHANGE_BATCH, DEFAULT_WALK_CHARS,
                               _walk_reads, accepted_text, applied_edits,
                               deliverable_docx, verify_run)
    from .providers import cost_of_usage, estimate_cost

    cfg = load_config(args.config)
    results = Path(args.results)
    if not (results / "findings.json").exists():
        print(f"error: no findings.json in {results} — point the results "
              f"argument at a finished run's output directory", file=sys.stderr)
        return 2
    if deliverable_docx(results) is None:
        print(f"error: no manuscript .docx in {results} — the finished-text "
              f"gates read the ACCEPTED deliverable, which is not there",
              file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else results
    setup_logging(out)

    if args.changes_only and args.walk_only:
        print("error: --changes-only and --walk-only are mutually exclusive",
              file=sys.stderr)
        return 2
    run_changes = not args.walk_only
    run_walk = not args.changes_only

    context = ""
    if args.context:
        try:
            context = Path(args.context).read_text(encoding="utf-8")
        except OSError as e:
            print(f"error: --context {args.context}: {e}", file=sys.stderr)
            return 2

    model = args.model or cfg.continuity.model or cfg.api.model

    # Estimate calls from edit batches and accepted-text windows before
    # constructing a provider.
    edits = applied_edits(results)
    accepted = accepted_text(results)
    n_change = (-(-len(edits) // DEFAULT_CHANGE_BATCH)) if run_changes else 0
    walk_reads = _walk_reads(accepted, DEFAULT_WALK_CHARS) if run_walk else []
    n_walk = len(walk_reads)
    proj_in = n_change * 1200 + (len(edits) * 60 if run_changes else 0)
    proj_in += sum(len(t) for read in walk_reads for _, t in read) // 4
    proj_in += n_walk * 600
    proj_out = (n_change + n_walk) * 300
    projected = estimate_cost(model, input_tokens=proj_in,
                              output_tokens=proj_out)

    if getattr(args, "dry_run", False):
        print(f"dry run — {len(edits)} applied edit(s) → {n_change} change-"
              f"verify call(s); {sum(1 for t in accepted.values() if t.strip())} "
              f"paragraph(s) of accepted text → {n_walk} walk read(s); "
              f"{n_change + n_walk} priced call(s) on {model}"
              + (f", projected ~${projected:.4f}" if projected is not None
                 else " (model not in the price catalog)"))
        print("\nno API call made, no output written.")
        if args.json:
            print(json.dumps({"model": model, "applied_edits": len(edits),
                              "change_calls": n_change, "walk_reads": n_walk,
                              "calls": n_change + n_walk,
                              "projected_usd": projected}, ensure_ascii=False))
        return 0

    engine = getattr(args, "engine", None) or "auto"
    if engine == "auto":
        # Use the configured provider unless a subscription model was
        # explicitly requested.
        from .providers.subagent import is_subagent_model
        engine = "subagent" if (args.model and is_subagent_model(args.model)) \
            else "provider"
    if engine == "none":
        print("error: `galley verify` needs a model lane (--engine provider "
              "or subagent)", file=sys.stderr)
        return 2
    if engine == "subagent":
        from .providers.subagent import SubagentProvider, resolve_model
        try:
            provider = SubagentProvider(model=args.model or None)
        except Exception as e:                              # noqa: BLE001
            print(f"error: {e}", file=sys.stderr)
            return 2
        # Record subscription usage with zero API cost and its token counts.
        model = f"subagent:{resolve_model(args.model or None)}"
        projected = 0.0
    else:
        guard = _galley_spend_guard(args, cfg, [model], projected_usd=projected)
        if guard is not None:
            return guard
        try:
            provider = build_provider(cfg, model=model)
        except ProviderError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    para_ids = _paragraph_ids(getattr(args, "paragraphs", None))

    concurrency = _lane_concurrency(cfg, engine, model)
    # Separate per-gate usage prevents certification from double-counting
    # spend.
    usage_changes, usage_walk = Usage(), Usage()
    from .agent_lane import AgentLaneUnavailable
    try:
        if para_ids is not None:
            from galley.verify import verify_delta
            changes = verify_delta(results, para_ids, provider, model,
                                   usage_changes, concurrency=concurrency,
                                   context=context,
                                   run_changes=run_changes, run_walk=False)
            walk = verify_delta(results, para_ids, provider, model, usage_walk,
                                concurrency=concurrency,
                                context=context, run_changes=False,
                                run_walk=run_walk)
        else:
            changes = verify_run(results, provider, model, usage_changes,
                                 concurrency=concurrency,
                                 context=context, run_changes=run_changes,
                                 run_walk=False)
            walk = verify_run(results, provider, model, usage_walk,
                              concurrency=concurrency,
                              context=context, run_changes=False,
                              run_walk=run_walk)
    except AgentLaneUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    problems, residuals = changes.problems, walk.residuals
    usage = Usage()
    for u in (usage_changes, usage_walk):
        _fold_usage(usage, u)
    cost = cost_of_usage(usage, fallback_model=model) or 0.0
    _galley_over_budget(args, cost)

    from galley.verify import write_artifacts
    # Share the artifact writer with settle to preserve build binding and
    # merge partial-read coverage.
    walk_paras = sum(1 for t in accepted.values() if t.strip())
    if not changes.ran_changes and not args.walk_only and (
            out / "change_verify.json").exists() and not para_ids:
        print("  kept the previous change_verify.json's verdicts: this gate "
              "read nothing", file=sys.stderr)
    if not walk.ran_walk and not args.changes_only and (
            out / "finished_walk.json").exists() and not para_ids:
        print("  kept the previous finished_walk.json's verdicts: this gate "
              "read nothing", file=sys.stderr)
    cv_path, fw_path = write_artifacts(
        out, changes, walk, model=model, engine=engine,
        usage_changes=usage_changes, usage_walk=usage_walk,
        applied=len(edits), paragraphs=walk_paras, para_ids=para_ids,
        merge=bool(para_ids) or (out / "change_verify.json").exists())

    print(f"\nchange verifier: {len(problems)} problem(s) of {len(edits)} "
          f"applied edit(s); finished-text walk: {len(residuals)} residual(s) "
          f"({usage.api_calls} model call(s), ${cost:.4f}).")
    from collections import Counter
    if problems:
        counts = Counter(p.verdict for p in problems)
        print("  change problems: "
              + ", ".join(f"{v}={n}" for v, n in counts.most_common()))
        for p in problems[:10]:
            print(f"    [{p.verdict}] {p.para_id}: {p.original_text!r} -> "
                  f"{p.corrected_text!r} — {p.detail[:70]}")
    highs = [r for r in residuals if r.severity == "high"]
    lower = [r for r in residuals if r.severity != "high"]
    for r in highs[:10]:
        print(f"    ({r.severity}) {r.para_id}: {r.quote!r} — {r.problem[:70]}")
    if lower:
        print(f"  note: {len(lower)} low/medium residual(s) — candidates for "
              f"a next wave, not a delivery block:")
        for r in lower[:10]:
            print(f"    ({r.severity}) {r.para_id}: {r.quote!r} — "
                  f"{r.problem[:70]}")
    print(f"\n  {cv_path}\n  {fw_path}")

    if args.json:
        payload_changes = json.loads(cv_path.read_text(encoding="utf-8"))
        payload_walk = json.loads(fw_path.read_text(encoding="utf-8"))
        print(json.dumps(_envelope(findings=(), usage=usage, model=model,
                                   extra={"change_verify": payload_changes,
                                          "finished_walk": payload_walk}),
                         ensure_ascii=False))
    # A requested gate that did not run must fail.
    if (run_changes and not changes.ran_changes) or \
            (run_walk and not walk.ran_walk):
        print(f"error: {changes.reason or walk.reason or 'a requested gate '
              'did not run'}", file=sys.stderr)
        return 2
    # The verify command fails on damaged edits or high-severity residuals.
    # Certification separately requires every residual to be settled.
    return 1 if (problems or highs) else 0


def _galley_letter(args) -> int:
    from galley.casefile import CaseFile
    from galley.letter import render_all

    target = Path(args.casefile)
    cf_path = target / "casefile.json" if target.is_dir() else target
    synth_dir = target if target.is_dir() else target.parent
    if not cf_path.exists() and not (synth_dir / "findings.json").exists():
        print(f"error: {cf_path} not found and no findings.json in {synth_dir} "
              f"to build one from — pass a casefile.json, or a run directory "
              f"holding one or a findings.json", file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else (
        cf_path.parent if cf_path.exists() else synth_dir)
    setup_logging(out)
    if cf_path.exists():
        try:
            cf = CaseFile.load(cf_path)
        except (OSError, ValueError) as e:
            print(f"error: could not read {cf_path}: {e}", file=sys.stderr)
            return 2
    else:
        from galley.casefile_synth import casefile_from_run
        try:
            cf = casefile_from_run(synth_dir)
        except (OSError, ValueError) as e:
            print(f"error: could not build a case file from {synth_dir}: {e}",
                  file=sys.stderr)
            return 2
        print(f"note: no casefile.json — built the letter from "
              f"{synth_dir / 'findings.json'}", file=sys.stderr)

    ms = None
    if args.source:
        from galley.ingest import manuscript_from_source
        cfg = load_config(args.config)
        try:
            ms = manuscript_from_source(args.source, cfg)
        except (IngestError, FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    if getattr(args, "workspace", None):
        from galley.casefile_synth import workspace_waves
        waves = workspace_waves(args.workspace)
        if waves:
            cf.waves = list(waves)
            cf.budget.charges = []
            for w in waves:
                cf.budget.charge(f"run {w.index}", w.spend_usd, wave=w.index)
        else:
            print(f"note: --workspace {args.workspace}: no runs/*/findings."
                  f"json found; the letter keeps the run's own spend",
                  file=sys.stderr)
    # Run evidence supplies the letter and verification report. It is absent
    # for a bare casefile without a sibling run directory.
    from galley.letter import render_verification_report, run_evidence
    evidence = run_evidence(synth_dir, getattr(args, "workspace", None)) \
        if (synth_dir / "findings.json").exists() else None
    letter_path, style_path = render_all(cf, out, ms=ms, evidence=evidence)
    report_path = render_verification_report(evidence, out, cf=cf) \
        if evidence is not None else None
    open_queries = sum(1 for v in cf.verdicts if v.ruling == "query")
    print(f"\nEditorial letter for {cf.book or '(untitled)'}: "
          f"{len(cf.findings)} finding(s), {len(cf.waves)} wave(s), "
          f"{open_queries} open query/-ies, {_money(cf.budget.spent_usd)} spent.")
    print(f"  {letter_path}\n  {style_path}"
          + (f"\n  {report_path}" if report_path else ""))
    if args.json:
        # Rendering uses recorded decisions. Keep Galley findings out of the
        # DocProof findings array.
        print(json.dumps(_envelope(findings=(), usage=Usage(),
                                   model=cfg.api.model if args.source else "",
                                   extra={
                                       "book": cf.book,
                                       "letter": str(letter_path),
                                       "style_sheet": str(style_path),
                                       "verification": str(report_path)
                                       if report_path else "",
                                       "findings": len(cf.findings),
                                       "waves": len(cf.waves),
                                       "open_queries": open_queries,
                                       "spent_usd": round(cf.budget.spent_usd, 4),
                                   }), ensure_ascii=False))
    return 0


def _galley_seed(args) -> int:
    from galley.ingest import manuscript_from_source, write_manuscript_docx
    from galley.seeding import seed_copy

    cfg = load_config(args.config)
    out = Path(args.out)
    setup_logging(out)
    try:
        ms = manuscript_from_source(args.source, cfg)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    seeded, key = seed_copy(ms, args.count, rng_seed=args.seed)

    out.mkdir(parents=True, exist_ok=True)
    seeded_path = out / "seeded_manuscript.json"
    key_path = out / "answer_key.json"
    seeded_path.write_text(
        json.dumps(seeded.to_json(), indent=2, ensure_ascii=False),
        encoding="utf-8")
    key_path.write_text(
        json.dumps(key.to_json(), indent=2, ensure_ascii=False),
        encoding="utf-8")

    # Materialize seeded DOCX input for review; other formats currently
    # export JSON only.
    seeded_docx: Path | None = None
    docx_error: str | None = None
    if Path(args.source).suffix.lower() == ".docx":
        try:
            seeded_docx = write_manuscript_docx(
                args.source, cfg, seeded, out / "seeded_manuscript.docx")
        except (IngestError, FileNotFoundError, ValueError) as e:
            docx_error = str(e)
    else:
        docx_error = (f"{Path(args.source).suffix or '(no extension)'} source: "
                      f"only .docx can be materialized into a reviewable "
                      f"document today")

    by_type: dict[str, int] = {}
    for pe in key.planted:
        by_type[pe.error_type] = by_type.get(pe.error_type, 0) + 1
    print(f"\nPlanted {len(key.planted)} of {key.requested} requested error(s) "
          f"across chapter(s) {', '.join(map(str, key.seeded_chapters)) or '—'} "
          f"(seed {key.rng_seed}).")
    for et in sorted(by_type):
        print(f"  {et:<22} {by_type[et]}")
    if len(key.planted) < key.requested:
        print("  (fewer than requested — the sampled chapters ran out of "
              "mutable sites)")
    print(f"\n  {seeded_path}\n  {key_path}")
    if seeded_docx is not None:
        print(f"  {seeded_docx}")
        print(f"  Next: `docproof review {seeded_docx} --out RUN`, then "
              f"`docproof galley score RUN/findings.json --answer-key "
              f"{key_path}` (a review's findings.json converts on its own).")
    else:
        print(f"  (no reviewable document written: {docx_error})")
    if args.json:
        print(json.dumps(_envelope(findings=(), usage=Usage(),
                                   model=cfg.api.model, extra={
                                       "seeded_manuscript": str(seeded_path),
                                       "seeded_docx": (str(seeded_docx)
                                                       if seeded_docx else None),
                                       "answer_key": str(key_path),
                                       "planted": len(key.planted),
                                       "requested": key.requested,
                                       "seeded_chapters": list(key.seeded_chapters),
                                       "rng_seed": key.rng_seed,
                                   }), ensure_ascii=False))
    return 0


def _galley_score(args) -> int:
    from galley.contracts import GFinding
    from galley.seeding import AnswerKey, score_catches

    findings_path = Path(args.findings)
    key_path = Path(args.answer_key)
    try:
        raw = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not read {findings_path}: {e}", file=sys.stderr)
        return 2
    rows = raw.get("findings", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        print("error: findings must be a JSON array (or a {'findings': [...]} "
              "envelope)", file=sys.stderr)
        return 2
    if _review_shaped(rows):
        from galley.adapters.docproof_ladder import gfindings_from_json
        if isinstance(raw, dict):
            findings, dropped = gfindings_from_json(findings_path, wave=1,
                                                    model="")
        else:
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".json",
                                             encoding="utf-8",
                                             delete=False) as tf:
                json.dump({"findings": rows}, tf)
                tmp = Path(tf.name)
            try:
                findings, dropped = gfindings_from_json(tmp, wave=1, model="")
            finally:
                tmp.unlink(missing_ok=True)
        print(f"  {len(findings)} review finding(s) converted "
              f"({dropped} non-validated/unanchored row(s) left out)")
    else:
        findings = [GFinding.from_json(r) for r in rows if isinstance(r, dict)]

    try:
        key = AnswerKey.from_json(json.loads(key_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not read {key_path}: {e}", file=sys.stderr)
        return 2

    est = score_catches(findings, key)
    out = Path(args.out) if args.out else findings_path.parent
    out.mkdir(parents=True, exist_ok=True)
    recall_path = out / "recall.json"
    payload = {
        "planted": est.planted,
        "caught": est.caught,
        "rate": round(est.rate, 4),
        "by_type": {k: list(v) for k, v in est.by_type.items()},
        "caveat": est.caveat,
    }
    recall_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                           encoding="utf-8")

    print(f"\n{est.summary()}")
    for et in sorted(est.by_type):
        caught, planted = est.by_type[et]
        print(f"  {et:<22} {caught}/{planted}")
    print(f"\n  {est.caveat}")
    print(f"\n  {recall_path}")
    if args.json:
        print(json.dumps(_envelope(findings=(), usage=Usage(), model="",
                                   extra=payload), ensure_ascii=False))
    return 0


def _galley_calibrate(args) -> int:
    from galley.calibration import (
        DEFAULT_CALIBRATION_FILENAME,
        calibrate_free,
        record_run,
        record_recall,
    )
    from galley.casefile import CaseFile
    from galley.ingest import manuscript_from_source

    if not args.source:
        print("error: source is required (the manuscript to seed, or — with "
              "--from-run — the manuscript that run reviewed)", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    calibration_path = (
        Path(args.calibration) if args.calibration
        else Path(args.config).parent / DEFAULT_CALIBRATION_FILENAME
    )
    calibration_path.parent.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else Path.cwd()
    out.mkdir(parents=True, exist_ok=True)
    setup_logging(out)

    try:
        ms = manuscript_from_source(args.source, cfg)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    book = args.book or Path(args.source).stem

    if args.from_run:
        results = Path(args.from_run)
        cf_path = results / "casefile.json" if results.is_dir() else results
        synth_dir = results if results.is_dir() else results.parent
        if not cf_path.exists() and not (synth_dir / "findings.json").exists():
            print(f"error: {cf_path} not found and no findings.json in "
                  f"{synth_dir} to build one from — pass a casefile.json, or "
                  f"a run directory holding one or a findings.json",
                  file=sys.stderr)
            return 2
        if cf_path.exists():
            try:
                cf = CaseFile.load(cf_path)
            except (OSError, ValueError) as e:
                print(f"error: could not read {cf_path}: {e}", file=sys.stderr)
                return 2
        else:
            from galley.casefile_synth import casefile_from_run
            try:
                cf = casefile_from_run(synth_dir, book=book)
            except (OSError, ValueError) as e:
                print(f"error: could not build a case file from {synth_dir}: "
                      f"{e}", file=sys.stderr)
                return 2
            cf_path = synth_dir / "findings.json"
            print(f"note: no casefile.json — recording cost from {cf_path}",
                  file=sys.stderr)

        calibration = record_run(cf, ms, calibration_path)
        entries = sorted(calibration.cost.values(),
                         key=lambda e: (e.adapter, e.model))
        print(f"\nRecorded cost calibration from {cf_path} "
              f"({len(cf.waves)} wave(s)) into {calibration_path}:")
        for e in entries:
            print(f"  {e.adapter:<14} {e.model or '(no model)':<24} "
                  f"${e.usd_per_kword:.4f}/kword  ({e.samples} sample(s), "
                  f"{e.kwords_total:.1f}k word(s) total)")
        if args.json:
            print(json.dumps({
                "calibration": str(calibration_path),
                "cost": {k: e.to_json() for k, e in calibration.cost.items()},
            }, ensure_ascii=False))
        return 0

    if args.model:
        print("error: --model is not yet wired into the seeded closed loop — "
              "the paid adapters re-read the source from disk, not the "
              "in-memory seeded copy, so recall can't be scored for a paid "
              "pass yet. Calibrate a real run's cost instead with --from-run",
              file=sys.stderr)
        return 2

    result = calibrate_free(ms, args.count, rng_seed=args.seed, book=book)
    est = result.estimate

    seeded_path = out / "seeded_manuscript.json"
    key_path = out / "answer_key.json"
    seeded_path.write_text(
        json.dumps(result.seeded.to_json(), indent=2, ensure_ascii=False),
        encoding="utf-8")
    key_path.write_text(
        json.dumps(result.answer_key.to_json(), indent=2, ensure_ascii=False),
        encoding="utf-8")

    record_run(result.casefile, result.seeded, calibration_path)
    record_recall(est, calibration_path, book=book)

    print(f"\n{est.summary()}")
    for et in sorted(est.by_type):
        caught, planted = est.by_type[et]
        print(f"  {et:<22} {caught}/{planted}")
    print(f"\n  {est.caveat}")
    print(f"\n  {seeded_path}\n  {key_path}\n  {calibration_path}")
    if args.json:
        print(json.dumps({
            "book": book,
            "calibration": str(calibration_path),
            "seeded_manuscript": str(seeded_path),
            "answer_key": str(key_path),
            "planted": est.planted,
            "caught": est.caught,
            "rate": round(est.rate, 4),
            "by_type": {k: list(v) for k, v in est.by_type.items()},
        }, ensure_ascii=False))
    return 0


def _flights_provider_of(cfg):
    """A `model id -> Provider` resolver for the flights matrix, which can mix
    vendors (`--models gpt-5.6-luna,claude-sonnet-5`). `build_provider(cfg)`
    picks one vendor from `cfg.api`; this instead resolves each model id's own
    vendor (`providers.provider_for`, falling back to `cfg.api.provider` for a
    model the catalog has never heard of) and caches one provider per vendor,
    so a mixed matrix still costs at most three constructions, not one per
    flight."""
    from .providers import ProviderError, provider_for

    cache: dict[str, object] = {}

    def get(model_id: str):
        name = provider_for(model_id, cfg.api.provider)
        if name not in cache:
            kwargs = {"max_retries": cfg.api.max_retries,
                      "prompt_caching": cfg.api.prompt_caching,
                      "effort": cfg.api.effort}
            if name == "anthropic":
                from .providers.anthropic_provider import AnthropicProvider
                cache[name] = AnthropicProvider(**kwargs)
            elif name == "openai":
                from .providers.openai_provider import OpenAIProvider
                cache[name] = OpenAIProvider(**kwargs)
            elif name == "gemini":
                from .providers.gemini_provider import GeminiProvider
                cache[name] = GeminiProvider(**kwargs)
            elif name == "deepinfra":
                from .providers.deepinfra_provider import DeepInfraProvider
                cache[name] = DeepInfraProvider(**kwargs)
            else:
                raise ProviderError(
                    f"Unknown provider {name!r} for model {model_id!r}. Set "
                    f"api.provider, or use a model id the catalog knows.")
        return cache[name]

    return get


def _galley_flights(args) -> int:
    """The copy-edit lane: propose -> site/filter -> cluster -> judge -> real
    edit-channel findings tagged lane="copyedit". Three modes share this one
    entry point (see the parser help): the full pipeline, --propose-only
    (stop after clustering — no judge spend), and --judge-only (skip straight
    to judging an existing clusters.json, no manuscript needed)."""
    from . import flights as fl
    from .providers import ProviderError

    cfg = load_config(args.config)
    if getattr(args, "variant", None):
        cfg.variant = args.variant
    if args.posture is None:
        args.posture = cfg.flights.posture
    # Default to the configured flights judge, then the detector model.
    # Subscription judging uses the external packet workflow.
    if args.judge_model is None:
        args.judge_model = (getattr(cfg.flights, "judge_model", None)
                            or DEFAULT_FLIGHTS_JUDGE)
    out = Path(args.out) if args.out else Path(cfg.output_dir)
    usage = Usage()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    lenses = [l.strip() for l in args.lenses.split(",") if l.strip()]
    flight_specs = fl.flight_matrix(models, lenses)

    if args.judge_only:
        setup_logging(out)
        clusters_path = Path(args.judge_only)
        try:
            raw = json.loads(clusters_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: could not read {clusters_path}: {e}", file=sys.stderr)
            return 2
        rows = raw.get("clusters", raw) if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            print("error: clusters must be a JSON array (or a "
                  "{'clusters': [...]} envelope)", file=sys.stderr)
            return 2
        clusters = [fl.Cluster.from_json(r) for r in rows if isinstance(r, dict)]
        from .providers import estimate_cost
        guard = _galley_spend_guard(
            args, cfg, [args.judge_model],
            projected_usd=estimate_cost(args.judge_model,
                                        input_tokens=len(clusters) * 320,
                                        output_tokens=len(clusters) * 90))
        if guard is not None:
            return guard
        return _flights_judge_and_write(args, cfg, out, clusters, usage,
                                        source=raw.get("source")
                                        if isinstance(raw, dict) else None)

    if not args.input:
        print("error: an input manuscript is required unless --judge-only is "
              "given", file=sys.stderr)
        return 2

    error_dir = _resolve_error_dir(args.config)
    try:
        # Disable paid preparation passes for a dry run.
        prepared = prepare(cfg, args.input, error_dir,
                           dry_run=bool(args.dry_run))
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.dry_run:
        words = sum(len(p.text.split())
                   for p in fl.usable_paragraphs(prepared.doc.paragraphs))
        proj = fl.project_cost(words, flight_specs, args.judge_model,
                               propose_max_tokens=args.propose_max_tokens,
                               judge_max_tokens=args.judge_max_tokens)
        print(f"\nDry run — {words} reviewable word(s), {len(flight_specs)} "
              f"flight(s): {', '.join(s.key for s in flight_specs)}")
        for row in proj["flights"]:
            print(f"  propose {row['flight']:<32} ~${row['est_usd']:.3f}  "
                  f"(~{row['est_candidates']} candidate(s))")
        print(f"  judge   {args.judge_model:<32} ~${proj['judge_usd']:.3f}  "
              f"(~{proj['est_clusters']} cluster(s), from an est. "
              f"{proj['est_total_candidates']} candidate(s))")
        print(f"\n  PROJECTED total: ~${proj['total_usd']:.2f}")
        if args.json:
            print(json.dumps(proj, ensure_ascii=False))
        return 0

    if getattr(args, "approval", None) or getattr(args, "budget", None) is not None:
        words = sum(len(p.text.split())
                    for p in fl.usable_paragraphs(prepared.doc.paragraphs))
        proj = fl.project_cost(words, flight_specs, args.judge_model,
                               propose_max_tokens=args.propose_max_tokens,
                               judge_max_tokens=args.judge_max_tokens)
        projected = (sum(r["est_usd"] for r in proj["flights"])
                     if args.propose_only else proj["total_usd"])
        guard = _galley_spend_guard(
            args, cfg, models + ([] if args.propose_only else [args.judge_model]),
            projected_usd=projected)
        if guard is not None:
            return guard

    setup_logging(out)
    try:
        provider_of = _flights_provider_of(cfg)
        # Resolve all providers before spending on any flight.
        for spec in flight_specs:
            provider_of(spec.model)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    paragraphs = prepared.doc.paragraphs
    closing_quotes = (prepared.variant.closing_quotes if prepared.variant
                      else "”\"")
    text_of = {p.para_id: p.text for p in fl.usable_paragraphs(paragraphs)}

    by_flight = fl.propose_flights(
        paragraphs, provider_of, flight_specs,
        max_tokens=args.propose_max_tokens, usage=usage,
        closing_quotes=closing_quotes, concurrency=args.concurrency)

    if args.external_proposals:
        ext_path = Path(args.external_proposals)
        try:
            ext_raw = json.loads(ext_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: could not read {ext_path}: {e}", file=sys.stderr)
            return 2
        ext_rows = (ext_raw.get("proposals", ext_raw)
                   if isinstance(ext_raw, dict) else ext_raw)
        if not isinstance(ext_rows, list):
            print("error: --external-proposals must be a JSON array (or a "
                  "{'proposals': [...]} envelope)", file=sys.stderr)
            return 2
        ext_cands, ext_dropped = fl.load_external_proposals(
            ext_rows, text_of, closing_quotes=closing_quotes)
        for c in ext_cands:
            by_flight.setdefault(c.flight, []).append(c)
        print(f"  external proposals: {len(ext_cands)} sited, {ext_dropped} "
              f"dropped by the deterministic filters")

    clusters = fl.cluster_proposals(by_flight, text_of)
    agree2 = sum(1 for c in clusters if c.agreement >= 2)
    print(f"\nPropose: {sum(len(v) for v in by_flight.values())} candidate(s) "
          f"across {len(by_flight)} flight(s).")
    print(f"Union: {len(clusters)} cluster(s) ({agree2} by >=2 flights, "
          f"{len(clusters) - agree2} by one).")

    if args.propose_only:
        out.mkdir(parents=True, exist_ok=True)
        clusters_path = out / "clusters.json"
        clusters_path.write_text(json.dumps(
            {"clusters": [c.to_json() for c in clusters], "source": args.input,
             "models": models, "lenses": lenses},
            indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  propose-only: judge skipped, {len(clusters)} un-judged "
              f"cluster(s) written.\n  {clusters_path}")
        if args.json:
            print(json.dumps({"clusters": str(clusters_path),
                              "count": len(clusters)}, ensure_ascii=False))
        return 0

    return _flights_judge_and_write(args, cfg, out, clusters, usage,
                                    source=args.input)


def _flights_judge_and_write(args, cfg, out, clusters, usage, *,
                             source: str | None) -> int:
    """The shared tail of every `flights` mode that reaches the judge: rule on
    every cluster once, accept at the confidence floor, and write the
    findings envelope. `source` is carried through only for the report — a
    cluster is judged from what it carries, not from re-reading the
    manuscript."""
    from . import flights as fl
    from .providers import ProviderError, cost_of_usage

    try:
        provider = _flights_provider_of(cfg)(args.judge_model)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    verdicts = fl.judge_clusters(clusters, provider, model=args.judge_model,
                                 posture=args.posture, usage=usage,
                                 max_tokens=args.judge_max_tokens,
                                 concurrency=args.concurrency)
    accepted, counts = fl.accept(clusters, verdicts,
                                 min_confidence=args.min_confidence)
    ids = itertools.count(1)
    findings = fl.findings_from_accepted(accepted, ids)

    cost = cost_of_usage(usage, fallback_model=args.judge_model) or 0.0
    _galley_over_budget(args, cost)
    envelope = {
        "findings": [fl.finding_to_json(f) for f in findings],
        "cost": {"total_usd": round(cost, 4), "by_model": usage.by_model},
        "ledger": {"api_calls": usage.api_calls,
                   "input_tokens": usage.input_tokens,
                   "output_tokens": usage.output_tokens,
                   "cache_read_input_tokens": usage.cache_read_input_tokens,
                   "cache_creation_input_tokens": usage.cache_creation_input_tokens},
        "checkpoint": None,
        "lane": fl.LANE,
        "posture": args.posture,
        "judge_model": args.judge_model,
        "min_confidence": args.min_confidence,
        "clusters": len(clusters),
        "judge_counts": counts.to_json(),
        "source": source,
    }
    out.mkdir(parents=True, exist_ok=True)
    findings_path = out / "flights_findings.json"
    findings_path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    print(f"\nJudge ({args.posture}, floor={args.min_confidence}): "
          f"{counts.accepted} accepted, {counts.kept} kept-original, "
          f"{counts.below_floor} below floor, {counts.unjudged} unjudged.")
    print(f"  {len(findings)} finding(s), ${cost:.2f}, {usage.api_calls} "
          f"model call(s).")
    print(f"\n  {findings_path}")
    if args.json:
        print(json.dumps(envelope, ensure_ascii=False))
    return 0


# --- helpers ------------------------------------------------------------------

def _money(value: float) -> str:
    return f"${value:,.2f}"


# The house default judge for the copy-edit flights lane when the config names
# none: the detector Luna, billed per cluster, never a Claude model (which
# bills through the API here — $0 Claude is the session-subagent route).
DEFAULT_FLIGHTS_JUDGE = "gpt-5.6-luna"


def _cost_line(usage: Usage, model: str) -> str:
    """The run's dollar line for stdout: model spend summed per model plus the
    Sapling share, to four decimals so a sub-cent mock or replay never reads as
    the silently-didn't-run "$0.00"."""
    from .providers import cost_of_usage
    cost = (cost_of_usage(usage, fallback_model=model) or 0.0) \
        + (getattr(usage, "sapling_cost", 0.0) or 0.0)
    return f"${cost:.4f} spent ({usage.api_calls} model call(s))"


def _now_iso() -> str:
    """UTC now, in the same ISO-8601 shape findings.json stamps itself with —
    the two are compared as strings when certify checks a verify artifact for
    staleness (galley.manifest._is_stale)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _cost_field(usage: Usage, model: str) -> dict:
    """The `cost: {total_usd, by_model}` object every cost-bearing galley
    artifact carries — the same shape a findings.json envelope records, so
    certify can sum a run's artifacts uniformly."""
    from .contract import build_envelope
    return build_envelope(findings=(), usage=usage, fallback_model=model)["cost"]


def _fold_usage(into: Usage, other: Usage) -> None:
    """Add every counter of `other` into `into`, per-model buckets included."""
    for f in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "api_calls", "sapling_chars"):
        setattr(into, f, getattr(into, f) + getattr(other, f))
    into.sapling_cost += other.sapling_cost
    for model, bucket in other.by_model.items():
        dst = into.by_model.setdefault(model, {"api_calls": 0})
        for k, v in bucket.items():
            dst[k] = dst.get(k, 0) + v


def _review_shaped(rows: list) -> bool:
    """True when a findings array is `docproof review` output (para_id +
    original_text/corrected_text, anchored by the validator) rather than
    galley GFindings (a `span` object + find/replace)."""
    dicts = [r for r in rows if isinstance(r, dict)]
    return bool(dicts) and all(
        "span" not in r and ("original_text" in r or "anchor" in r)
        for r in dicts)


def _result_line(outputs) -> str:
    """What a finished review produced, both halves of it — no full stop, so a
    caller can add its own tail.

    A review hands back tracked changes and margin questions, and printing only
    the first tells someone who ran it on the command line that a book with
    forty questions waiting in it needed nothing looked at."""
    line = f"{outputs.applied} tracked change(s) applied"
    if outputs.queried:
        line += f", {outputs.queried} question(s) raised"
    return line


def _plain_state(job) -> str:
    return {"submitted": "waiting on the provider",
            "in_progress": "being reviewed",
            "ready": "ready to collect",
            "done": "finished",
            "failed": "needs attention"}.get(job.state, job.state)


def _envelope(*, findings, usage: Usage, model: str, coverage=None,
             checkpoint=None, extra: dict | None = None) -> dict:
    """Thin call-site wrapper over docproof.contract.build_envelope, so every
    verb's --json code reads the same way. See docproof/contract.py for the
    envelope's shape and the reasoning behind it."""
    from .contract import build_envelope
    return build_envelope(findings=findings, usage=usage, fallback_model=model,
                          coverage=coverage, checkpoint=checkpoint, extra=extra)


def _usage_from_payload(payload: dict) -> Usage:
    """Reconstruct a Usage from a findings.json's "usage" object — the same
    shape `dataclasses.asdict(usage)` produced it in, so this is the read side
    of that write. Used where a caller has no live Usage to hand the envelope
    builder (multi-round review keeps its own driver's totals off this
    module's beaten path); malformed input degrades to an all-zero Usage
    rather than failing the whole --json print over a cost figure."""
    try:
        return Usage(**(payload.get("usage") or {}))
    except TypeError:
        return Usage()


def _load_mocks(path: str):
    try:
        canned = json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: --mock-findings {path}: {e}", file=sys.stderr)
        return None
    if not isinstance(canned, list):
        print("error: --mock-findings must be a JSON array of findings.",
              file=sys.stderr)
        return None
    return canned


def _run_mock(cfg, prepared, canned):
    ids = itertools.count(1)
    usage = Usage()
    findings = []
    for prun in prepared.effective_pass_plan:
        analyzer = MockAnalyzer(list(prun.types), canned, ids)
        for chunk in prun.chunks:
            found, _ = analyzer.analyze_chunk(chunk, usage)
            findings.extend(found)
    # Mock mode makes no candidate judge calls, but deterministic candidate
    # errors still exercise the same production bridge and downstream guards.
    if (prepared.candidate_screening is not None
            and cfg.candidate_screening.mode == "apply"):
        findings.extend(prepared.candidate_screening.production_findings())
    return findings, usage


def cmd_galley_profile(args) -> int:
    from .config import load_config
    from .genre_profile import build_profile, confirm_with_model

    cfg = load_config(args.config)
    try:
        profile = build_profile(args.input, cfg)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    usage = Usage()
    if args.model:
        from .formats import get_format
        fmt = get_format(args.input)
        pkg = fmt.preflight(args.input, cfg.tracked_changes_policy)
        doc = fmt.build_document_model(pkg, cfg)
        profile = confirm_with_model(profile, doc.paragraphs,
                                     model=args.model, cfg=cfg, usage=usage)
        # Report cost on stderr to keep --json clean. Zero with --model means
        # the call failed and the profile remains deterministic.
        print(f"model confirmation on {args.model}: "
              f"{_cost_line(usage, args.model)}", file=sys.stderr)

    if args.out:
        Path(args.out).write_text(profile.model_dump_json(indent=2) + "\n",
                                  encoding="utf-8")
        print(f"wrote {args.out}")

    if args.json:
        print(profile.model_dump_json(indent=2))
        return 0

    print(f"{profile.word_count:,} words, {profile.paragraph_count} "
         f"paragraph(s), {len(profile.chapters)} chapter(s)")
    print(f"dialogue density: {profile.dialogue_density:.1%}")
    if profile.proper_nouns:
        top = ", ".join(f"{p.name} ({p.count})"
                        for p in profile.proper_nouns[:10])
        print(f"proper-noun candidates ({len(profile.proper_nouns)}): {top}"
             + (" ..." if len(profile.proper_nouns) > 10 else ""))
    for tic in profile.tics:
        print(f"tic: {tic.label} x{tic.count}")
    if profile.bespoke_sweep_candidates:
        print(f"{len(profile.bespoke_sweep_candidates)} bespoke-sweep "
             f"candidate(s) — see --json for patterns")
    if profile.chapter_label_rows:
        print(f"{len(profile.chapter_label_rows)} chapter/part label(s) out of "
              f"sequence or style of {len(profile.chapter_labels)} — "
              f"renumber rows ready (--chapter-rows FILE, then "
              f"import-findings):")
        for row in profile.chapter_label_rows[:8]:
            print(f"  {row['para_id']}: {row['original_text']!r} -> "
                  f"{row['corrected_text']!r}")
    if getattr(args, "chapter_rows", None):
        Path(args.chapter_rows).write_text(
            json.dumps(profile.chapter_label_rows, indent=2,
                       ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {len(profile.chapter_label_rows)} renumber row(s) to "
              f"{args.chapter_rows}")
    if profile.field_errors:
        print(f"WARNING: {len(profile.field_errors)} unresolved field "
              f"result(s) (\"Error! Bookmark not defined.\" etc.) — the "
              f"designer regenerates the TOC; no lane edits them:")
        for line in profile.field_errors[:6]:
            print(f"  {line}")
    rl = profile.reading_level
    if rl.ari is not None:
        print(f"reading level: ARI {rl.ari:.1f}"
             + (f", mean word rarity (zipf) {rl.mean_zipf:.2f}"
                if rl.mean_zipf is not None else ""))
    guesses = ", ".join(f"{g.genre} ({g.score:.0%})"
                        for g in profile.genre_guesses)
    print(f"genre guess: {guesses}")
    print(f"recommended posture preset: {profile.recommended_preset}"
         + (" (model-confirmed)" if profile.model_confirmed else ""))
    if profile.model_notes:
        print(f"model notes: {profile.model_notes}")
    return 0


def _galley_state(args) -> int:
    from galley.manifest import config_hash, sha256_file
    from galley.state_machine import (RunStateMachine, StateError,
                                      hash_artifact)

    run = Path(args.run)
    state_path = run / "state.json"
    machine = RunStateMachine.load(state_path) if state_path.is_file() \
        else RunStateMachine()

    src_hash = sha256_file(args.source) if getattr(args, "source", None) \
        and Path(args.source).is_file() else ""
    cfg_hash = ""
    if getattr(args, "config", None):
        try:
            cfg_hash = config_hash(_effective_cfg(args))
        except (FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    if getattr(args, "verify_resume", False):
        mismatches = machine.verify_resume(
            source_sha256=src_hash, config_sha256=cfg_hash,
            artifact_hasher=hash_artifact)
        if mismatches:
            print(f"UNSAFE to resume from {machine.current!r}:", file=sys.stderr)
            for m in mismatches:
                print(f"  - {m}", file=sys.stderr)
            return 6
        print(f"safe to resume from {machine.current!r}")
        return 0

    if getattr(args, "advance", None):
        if args.advance in ("settled", "certified", "delivered") \
                and getattr(args, "results", None):
            from galley.settle import open_items, terminal_state
            results = Path(args.results)
            problems: list[str] = []
            try:
                env = json.loads((results / "findings.json").read_text("utf-8"))
            except (OSError, ValueError) as e:
                print(f"error: --results {results}: no readable findings.json "
                      f"({e})", file=sys.stderr)
                return 2
            for row in env.get("findings") or []:
                if isinstance(row, dict):
                    st, _r = terminal_state(row)
                    if st not in ("applied", "dropped", "query"):
                        problems.append(f"{row.get('finding_id', '?')} is "
                                        f"{st}")
            for item in open_items(results):
                problems.append(f"{item.kind} {item.id} in {item.para_id} has "
                                f"no settlement record")
            if problems:
                print(f"REFUSED: cannot advance to {args.advance!r} — "
                      f"{len(problems)} open item(s):", file=sys.stderr)
                for line in problems[:10]:
                    print(f"  - {line}", file=sys.stderr)
                print("  run `docproof galley settle` first.", file=sys.stderr)
                return 7
        try:
            rec = machine.advance(args.advance, at=args.at, by=args.by,
                                  source_sha256=src_hash, config_sha256=cfg_hash)
        except StateError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        run.mkdir(parents=True, exist_ok=True)
        machine.save(state_path)
        print(f"advanced to {rec.state!r} (from {len(machine.history)} "
              f"recorded state(s))")
    else:
        print(f"run state: {machine.current or '(none recorded)'}")
        for rec in machine.history:
            print(f"  {rec.state:20} {rec.at or ''} {rec.by or ''}")
    if args.json:
        print(machine.model_dump_json())
    return 0


def _galley_ledger(args) -> int:
    from galley.lifecycle import reconstruct_from_findings

    run = Path(args.run)
    findings_path = run / "findings.json" if run.is_dir() else run
    if not findings_path.is_file():
        findings_path = run / "flights_findings.json"
    try:
        envelope = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not read findings from {args.run}: {e}",
              file=sys.stderr)
        return 2

    led = reconstruct_from_findings(envelope)
    states = led.by_state()
    dups = led.duplicates()
    print(f"Lifecycle ledger — {len(led)} finding(s) from {findings_path}:")
    for state in ("detected", "verified", "held", "promoted", "merged",
                  "queried", "rejected", "dropped", "delivered"):
        if states.get(state):
            print(f"  {state:10} {states[state]}")
    if dups:
        n = sum(len(v) for v in dups.values())
        print(f"\n  {len(dups)} duplicate group(s) ({n} finding(s) share a "
              f"content key):")
        for key, ids in list(dups.items())[:20]:
            print(f"    {key}: {', '.join(ids)}")
    else:
        print("\n  no content-duplicate findings")

    if getattr(args, "out", None):
        led.save(args.out)
        print(f"\n  wrote ledger: {args.out}")
    if args.json:
        print(json.dumps(led.to_json(), ensure_ascii=False))
    return 0


def _galley_intent_zones(args) -> int:
    from .config import load_config
    from .formats import get_format
    from .intent_zones import load_intent_zones, resolve

    try:
        cfg = load_config(args.config)
        zones = load_intent_zones(args.zones)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        fmt = get_format(args.input)
        pkg = fmt.preflight(args.input, cfg.tracked_changes_policy)
        doc = fmt.build_document_model(pkg, cfg)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    closing = doc.variant.closing_quotes if getattr(doc, "variant", None) \
        else "”\""
    resolved = resolve(zones, list(doc.paragraphs), closing_quotes=closing)
    span_map = resolved.as_dict()
    total = sum(len(v) for v in span_map.values())
    print(f"Resolved {len(zones.zones)} intent zone(s) → {total} protected "
          f"span(s) across {len(span_map)} paragraph(s):")
    for z in zones.zones:
        sel = ("para_ids" if z.para_ids else "para_range" if z.para_range
               else "terms" if z.terms else "regex" if z.regex
               else "quotes" if z.quotes else "?")
        print(f"  [{z.permission}] {z.label or z.category or '(zone)'} "
              f"— by {sel}")

    if getattr(args, "out", None):
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            {pid: [[s, e] for s, e in spans]
             for pid, spans in span_map.items()},
            indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  wrote resolved span map: {out}")
    if args.json:
        print(json.dumps({pid: [[s, e] for s, e in spans]
                          for pid, spans in span_map.items()},
                         ensure_ascii=False))
    return 0


def _galley_triage_nouns(args) -> int:
    from .genre_profile import Profile
    from .profile_corrections import triage_proper_nouns

    if str(args.profile).lower().endswith((".docx", ".idml")):
        print(f"error: {args.profile}: triage-nouns takes the profile JSON, "
              "not the manuscript — run `docproof galley profile IN --json > "
              "profile.json` first, then pass profile.json here.",
              file=sys.stderr)
        return 2
    try:
        profile = Profile.model_validate_json(
            Path(args.profile).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"error: {args.profile}: {e}", file=sys.stderr)
        return 2
    entries = triage_proper_nouns(profile)

    buckets: dict[str, list] = {}
    for e in entries:
        buckets.setdefault(e.suggested_class, []).append(e)
    print(f"Proper-noun triage for {profile.source} "
          f"({len(entries)} candidate(s)):")
    for cls in ("protect", "enforce", "reject", "suspect"):
        rows = buckets.get(cls, [])
        if not rows:
            continue
        print(f"\n  {cls.upper()} ({len(rows)}):")
        for e in rows:
            print(f"    {e.name:20} {e.count:>4}x   {e.reason}")

    if getattr(args, "out", None):
        starter = {
            "source": profile.source,
            "recommended_preset": None,
            "proper_noun_classes": {e.name: e.suggested_class for e in entries},
            "note": "Triage suggestions — edit before use. Only protect/"
                    "enforce names seed the config; reject/suspect are held.",
        }
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(starter, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\n  wrote correction-overlay starter: {out}")
    if args.json:
        print(json.dumps([e.model_dump() for e in entries], ensure_ascii=False))
    return 0


def cmd_galley_genre_pack(args) -> int:
    from .genre import write_genre_pack
    from .genre_profile import Profile

    profile = None
    if args.profile:
        try:
            profile = Profile.model_validate_json(
                Path(args.profile).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"error: --profile {args.profile}: {e}", file=sys.stderr)
            return 2

    corrections = None
    if getattr(args, "corrections", None):
        from .profile_corrections import ProfileCorrections
        try:
            corrections = ProfileCorrections.model_validate_json(
                Path(args.corrections).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"error: --corrections {args.corrections}: {e}",
                  file=sys.stderr)
            return 2
        if profile is None:
            print("error: --corrections needs --profile (it overlays one)",
                  file=sys.stderr)
            return 2

    try:
        summary = write_genre_pack(
            args.base, args.genre, args.out, profile=profile,
            corrections=corrections, stage=getattr(args, "stage", None),
            era=getattr(args, "era", None))
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"wrote {summary['out_path']} — genre '{summary['genre']}' over "
         f"{summary['base_config']}")
    if "stage" in summary:
        print(f"  stage: {summary['stage']} (lanes + locks)")
    if summary.get("stage_lock_violations"):
        print(f"  stage lock WON over genre for: "
              f"{', '.join(summary['stage_lock_violations'])}")
    if summary["overlay_applied"]:
        print(f"  posture: {', '.join(summary['overlay_applied'])}")
    if summary["pending"]:
        for key, value in summary["pending"].items():
            print(f"  NOT applied (no matching config section yet): "
                 f"{key} = {value!r}")
    if "continuity_prompt" in summary:
        print(f"  continuity prompt: {summary['continuity_prompt']}")
    if "genre_scans_applied" in summary:
        print(f"  genre scans: {summary['genre_scans_applied']}")
    if summary.get("corrections_applied"):
        print("  correction overlay applied: reject/suspect names held back "
              "from seeding (raw profile untouched)")
    if summary["seeded_names_count"]:
        print(f"  seeded {summary['seeded_names_count']} proper noun(s) from "
             f"the profile into consistency.seeded_names / "
             f"spellcheck.allowlist")
    elif args.profile:
        print("  profile had no proper-noun candidates to seed")
    if "reading_level_target_ari" in summary:
        print(f"  reading-level target ARI set to "
             f"{summary['reading_level_target_ari']:.1f} from the profile")
    if "anachronism_era" in summary:
        print(f"  anachronism scan era set to {summary['anachronism_era']} "
              f"(the scan is a no-op without it)")
    return 0


def _effective_cfg(args):
    """Build the effective Config from --config plus any --stage/--genre, with
    the same stage>genre precedence _configure uses. For the read-only galley
    verbs (routes/approve/certify) that need the config a run WOULD use."""
    cfg = load_config(args.config)
    _stage, _genre = _resolve_stage_genre(
        args.config, getattr(args, "stage", None), getattr(args, "genre", None))
    stage_locks = {}
    if _stage:
        from .stages import apply_stage
        cfg, stage_locks = apply_stage(cfg, _stage)
    if _genre:
        from .genre import apply_genre
        cfg, _ = apply_genre(cfg, _genre)
    if stage_locks:
        from .stages import enforce_locks
        enforce_locks(cfg, stage_locks)
    return cfg


def _galley_routes(args) -> int:
    from .genre import available_genres  # noqa: F401 (parser choices)
    from galley.manifest import model_routes

    try:
        cfg = _effective_cfg(args)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    routes = model_routes(cfg)
    active = [r for r in routes if r.active]
    print(f"Effective model routes for {args.config}"
          f"{f' +stage {args.stage}' if getattr(args, 'stage', None) else ''}"
          f"{f' +genre {args.genre}' if getattr(args, 'genre', None) else ''}:")
    for r in routes:
        mark = " " if r.active else "·"
        print(f"  {mark} {r.role:<34} {r.model:<20} {r.provider}"
              f"{'' if r.active else '   (lane off)'}")
    reachable = sorted({r.provider for r in active})
    print(f"\n  active providers: {', '.join(reachable)}")

    denied = sorted(set(getattr(args, "deny", []) or []))
    violations = [r for r in active if r.provider in denied]
    if args.json:
        print(json.dumps({"routes": [r.to_json() for r in routes],
                          "active_providers": reachable,
                          "denied": denied,
                          "violations": [r.to_json() for r in violations]},
                         ensure_ascii=False))
    if violations:
        print(f"\n  DENIED provider reachable: "
              + "; ".join(f"{r.role}->{r.provider}" for r in violations),
              file=sys.stderr)
        return 3
    return 0


def _sniff_pack_provenance(config_path: str | Path) -> dict[str, str]:
    """Read the `# galley: genre=… stage=…` header write_genre_pack stamps on a
    materialized config. Empty dict when there is none (a hand-written config)."""
    try:
        with open(config_path, encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return {}
    m = re.match(r"#\s*galley:\s*(.+)", first)
    if not m:
        return {}
    return dict(pair.split("=", 1) for pair in m.group(1).split()
                if "=" in pair)


def _galley_approve(args) -> int:
    from galley.manifest import build_manifest

    prov = _sniff_pack_provenance(args.config)
    try:
        cfg = _effective_cfg(args)
        manifest = build_manifest(
            source=args.input, config_path=args.config, cfg=cfg,
            max_spend_usd=args.budget,
            stage=getattr(args, "stage", None) or prov.get("stage"),
            genre=getattr(args, "genre", None) or prov.get("genre"),
            mechanical_only=bool(getattr(args, "mechanical_only", False)),
            comment_budget=getattr(args, "comment_budget", None),
            note=args.note)
    except (FileNotFoundError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"wrote {out} — approval for {args.input}")
    print(f"  source {manifest['source_sha256'][:12]}…  config "
          f"{manifest['config_sha256'][:12]}…")
    print(f"  budget ${manifest['max_spend_usd']:.2f}  stage "
          f"{manifest['stage']}  lanes {manifest['enabled_lanes']}"
          + ("  MECHANICAL ONLY" if manifest.get("mechanical_only") else "")
          + (f"  comments <= {manifest['comment_budget']}"
             if manifest.get("comment_budget") else ""))
    print(f"  allowed providers: {', '.join(manifest['allowed_providers'])}")
    print(f"  allowed models: {', '.join(manifest['allowed_models'])}")
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False))
    return 0


def _galley_certify(args) -> int:
    from galley.manifest import certify_run

    manifest = None
    if getattr(args, "approval", None):
        try:
            manifest = json.loads(Path(args.approval).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: could not read {args.approval}: {e}", file=sys.stderr)
            return 2
    cfg = None
    if getattr(args, "config", None):
        try:
            cfg = _effective_cfg(args)
        except (FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    try:
        cert = certify_run(args.run, manifest=manifest, cfg=cfg,
                           source=getattr(args, "source", None))
    except (FileNotFoundError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"Certificate for {args.run}:")
    for c in cert.checks:
        glyph = {"pass": "PASS", "fail": "FAIL", "skip": "skip",
                 "warn": "WARN"}.get(c.status, c.status)
        if c.required and c.status != "pass":
            glyph = "MISSING" if c.status != "fail" else "FAIL"
        print(f"  [{glyph}] {c.name}{f' — {c.detail}' if c.detail else ''}")
    missing = [c.name for c in cert.missing if c.status != "fail"]
    print(f"\n  {'PASSED' if cert.passed else 'FAILED'} "
          f"({len(cert.failed)} failing check(s)"
          + (f", {len(missing)} required check(s) without evidence: "
             + ", ".join(missing) if missing else "") + ")")
    if args.json:
        print(json.dumps(cert.to_json(), ensure_ascii=False))
    return 0 if cert.passed else 4


def _galley_export_judgments(args) -> int:
    """clusters.json (from flights --propose-only) -> a canonical judgment
    packet for an external judge. $0, no model."""
    from . import flights as fl
    from galley.packet import export_packet

    try:
        raw = json.loads(Path(args.clusters).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not read {args.clusters}: {e}", file=sys.stderr)
        return 2
    rows = raw.get("clusters", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        print("error: clusters must be a JSON array (or a "
              "{'clusters': [...]} envelope)", file=sys.stderr)
        return 2
    clusters = [fl.Cluster.from_json(r) for r in rows if isinstance(r, dict)]

    zones: dict = {}
    if getattr(args, "intent_zones", None):
        try:
            zraw = json.loads(Path(args.intent_zones).read_text(encoding="utf-8"))
            zones = {str(pid): [(int(a), int(b)) for a, b in ranges]
                     for pid, ranges in zraw.items()}
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            print(f"error: --intent-zones {args.intent_zones}: {e}",
                  file=sys.stderr)
            return 2

    source = raw.get("source") if isinstance(raw, dict) else None
    packet = export_packet(clusters, source=source, intent_zones=zones)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(packet, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    flagged = sum(1 for c in packet["clusters"] if c["intent_zone"])
    print(f"wrote {out} — {len(packet['clusters'])} cluster(s) to judge"
          f"{f', {flagged} in an intent zone (query/reject only)' if flagged else ''}")
    print("  fill each cluster's `decision` (accept|replace|query|reject), "
          "then `docproof galley import-judgments`")
    if args.json:
        print(json.dumps({"out": str(out), "clusters": len(packet["clusters"]),
                          "intent_zone_flagged": flagged}, ensure_ascii=False))
    return 0


def _galley_import_judgments(args) -> int:
    """A filled judgment packet -> the findings envelope, validated, no model
    call. The model-free counterpart to flights --judge-only."""
    from . import flights as fl
    from galley.packet import PacketError, import_decisions

    try:
        packet = json.loads(Path(args.packet).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not read {args.packet}: {e}", file=sys.stderr)
        return 2
    try:
        result = import_decisions(packet)
    except PacketError as e:
        print(f"error: judgment packet rejected — {e}", file=sys.stderr)
        return 2

    out = Path(args.out) if getattr(args, "out", None) else Path(args.packet).parent
    out.mkdir(parents=True, exist_ok=True)
    envelope = {
        "findings": [fl.finding_to_json(f) for f in result.findings],
        "cost": {"total_usd": 0.0, "by_model": {}},
        "ledger": {"api_calls": 0, "input_tokens": 0, "output_tokens": 0,
                   "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0},
        "checkpoint": None,
        "lane": fl.LANE,
        "posture": "external",
        "judge_model": "external:import-judgments",
        "min_confidence": None,
        "clusters": len(packet.get("clusters", []) or []),
        "judge_counts": result.counts,
        "source": result.source,
    }
    findings_path = out / "flights_findings.json"
    findings_path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    c = result.counts
    print(f"\nImported (model-free): {c.get('accept', 0)} accepted, "
          f"{c.get('replace', 0)} replaced, {c.get('query', 0)} queried, "
          f"{c.get('reject', 0)} rejected, {c.get('unruled', 0)} unruled.")
    print(f"  {len(result.findings)} finding(s), $0.00, 0 model call(s).")
    print(f"\n  {findings_path}")
    if args.json:
        print(json.dumps(envelope, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
