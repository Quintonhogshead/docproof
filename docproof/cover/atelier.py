"""The atelier: one agent per concept, building its assigned cover until it
judges the cover finished.

This replaces the fixed critique-and-revise loop that used to follow painting
(a vision judge, a Sonnet reviser, up to four rounds, all of it choreographed
by docproof.cover.pipeline). The loop's ceiling was structural: it could only
ever answer the question it was asked in the order it was asked, so it
reported problems it had no move for and spent its rounds on wall-clock
pressure rather than on the cover being right.

An agent has the moves. It reads the archetype before it fights it, argues
itself into an art prompt before it buys one, looks at what came back,
measures the render report, and edits the spec for free as many times as it
takes. That is what produced the six Longsword covers by hand; this module is
that loop, in-product, N-wide.

Four things this module is built around:

- **The tools are the studio's own verbs.** `paint` is imaging.generate,
  `render` is compose + save_renders, `edit_spec` is the same JSON-path patch
  vocabulary revise_spec has always used, with the same guarded paths. There
  is no second rendering path for an agent to drift onto.
- **Money is metered here, not trusted to the agent.** `Budget` is checked
  before every generation and the refusal goes back as the tool result, so an
  agent that wants a thirteenth image is told no in a sentence it can act on
  rather than quietly spending. The agent may choose the tier (§7.2's draft
  and full rungs) — escalating a keeper to 2K is exactly the judgment worth
  delegating — but every roll is priced from the SAME tier it rolled at.
- **Finishing is a decision, not a timeout.** `finish` is a tool. An agent
  that stops calling tools without it has run out of turns, and that is
  recorded as a different outcome from a cover its builder called done.
- **A failed concept is one concept.** Everything raises inward: the caller
  gets a ConceptOutcome either way, and an atelier that dies leaves its
  siblings running.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import agent_lane
from . import doctrine
from .archetypes import ARCHETYPES, Archetype
from .compose import compose
from .direction import SpecEdit, _apply_edit
from .imaging import IMAGE_COST, generate, has_real_alpha
from .model import ArtSlot, Brief, CoverSpec, RenderReport

log = logging.getLogger("docproof.cover.atelier")

SERVER_NAME = "atelier"

# Opus 5 executes. A cover is a judgment call end to end — which trap this
# archetype sets, whether the plate that came back is the plate that was
# asked for, whether the thing is finished — and the turn is subscription
# covered either way. A default is not a pin; the env var exists so a whole
# job can be tried on another model.
DEFAULT_MODEL = "claude-opus-5"
MODEL_ENV = "DOCPROOF_ATELIER_MODEL"

# One concept's ceiling. Both bind, whichever comes first: the count stops an
# agent that has lost the thread on cheap rolls, the dollars stop one that
# escalated everything to 2K. Sized from the six hand-built covers, which
# spent 1-2 generations each and never wanted more than about six.
MAX_GENERATIONS = 12
MAX_ART_USD = 0.50

# Turns, not rounds: a turn is one model reply, so this bounds the whole
# session rather than any particular loop inside it. Generous, because the
# planning half of a good cover is several turns before a cent is spent.
MAX_TURNS = 60

# What the agent's eyes get. compose renders at 1600x2560; a vision call does
# not need that and pays for every pixel, so the look is downscaled — and to
# roughly the width a browser shows, which is the size the agent is being
# asked to judge the cover at anyway.
LOOK_WIDTH = 600

_SUBJECT = "The cover atelier"
_REMEDY = "start the job again"
_INSTALL_HINT = agent_lane.install_hint(_SUBJECT, _REMEDY)
_LOGIN_HINT = agent_lane.login_hint(_SUBJECT, _REMEDY)
_CLI_HINT = agent_lane.cli_hint(_SUBJECT, _REMEDY)

AtelierUnavailable = agent_lane.AgentLaneUnavailable

ASSETS_DIR = "assets"
RENDERS_DIR = "renders"


@dataclass
class Budget:
    """One concept's art allowance, and what it has actually spent."""
    max_generations: int = MAX_GENERATIONS
    max_usd: float = MAX_ART_USD
    generations: int = 0
    usd: float = 0.0

    def refusal(self, tier: str) -> str | None:
        """Why this generation may not happen, or None."""
        if self.generations >= self.max_generations:
            return (f"Budget spent: {self.generations} of "
                    f"{self.max_generations} generations used. Finish with "
                    f"what you have — spec edits and `render` are free.")
        price = IMAGE_COST.get(tier, IMAGE_COST["2K"])
        if self.usd + price > self.max_usd + 1e-9:
            return (f"Budget spent: ${self.usd:.2f} of ${self.max_usd:.2f} "
                    f"used and a {tier} roll costs ${price:.2f}. Drop to a "
                    f"cheaper tier or finish with what you have.")
        return None

    def charge(self, tier: str) -> float:
        price = IMAGE_COST.get(tier, IMAGE_COST["2K"])
        self.generations += 1
        self.usd += price
        return price

    def describe(self) -> str:
        return (f"{self.generations}/{self.max_generations} generations, "
                f"${self.usd:.2f}/${self.max_usd:.2f} spent")


@dataclass
class ConceptOutcome:
    """What one atelier session produced. Always returned, never raised."""
    spec: CoverSpec
    report: RenderReport | None = None
    renders: list[str] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)
    summary: str = ""
    finished: bool = False
    error: str | None = None


@dataclass
class _ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]


def _text(body: str, *, is_error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": body}]}
    if is_error:
        out["is_error"] = True
    return out


def resolve_model(model: str | None = None) -> str:
    if model and model.strip():
        return model.strip()
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def _describe_report(report: RenderReport) -> str:
    """The render report as the sentences an agent should act on.

    `adjustments` and `warnings` first and named as such, because those are
    the engine saying what it had to do to save the cover — the single most
    actionable thing in the whole record, and the thing a raw JSON dump
    buries between two dictionaries of floats."""
    lines = [
        f"contrast: {json.dumps(report.contrast)}",
        f"scrim_final: {json.dumps({str(k): v for k, v in report.scrim_final.items()})}",
        f"fitted_sizes: {json.dumps(report.fitted_sizes)}",
        f"occlusion: {json.dumps(report.occlusion)}",
        f"dead_band: {report.dead_band_frac:.3f}",
    ]
    if report.adjustments:
        lines.append("AUTOPILOT (the engine changed your design to keep the "
                     "type legible — treat each as a defect to fix at the "
                     "source): " + "; ".join(report.adjustments))
    if report.warnings:
        lines.append("WARNINGS: " + "; ".join(report.warnings))
    if not report.adjustments and not report.warnings:
        lines.append("No warnings and no autopilot intervention.")
    return "\n".join(lines)


class _Session:
    """One concept's mutable state. Handlers are ordinary methods so a test
    can call them with a dict and no SDK in sight."""

    def __init__(self, *, job_dir: Path, index: int, brief: Brief,
                 spec: CoverSpec, archetype: Archetype, image_client: Any,
                 assemble_prompt: Callable[[ArtSlot, Archetype], str],
                 budget: Budget, sem: asyncio.Semaphore | None,
                 save_renders: Callable[..., list[str]]):
        self.job_dir = job_dir
        self.index = index
        self.brief = brief
        self.spec = spec
        self.archetype = archetype
        self.image_client = image_client
        self.assemble_prompt = assemble_prompt
        self.budget = budget
        self.sem = sem
        self._save_renders = save_renders
        self.report: RenderReport | None = None
        self.renders: list[str] = []
        self.ledger: list[dict] = []
        self.finished = False
        self.summary = ""
        self.composed = False

    # -- tools ---------------------------------------------------------------

    async def read_spec(self, args: dict[str, Any]) -> dict[str, Any]:
        return _text(self.spec.model_dump_json(indent=2))

    async def archetype_info(self, args: dict[str, Any]) -> dict[str, Any]:
        """What the director is told about the template it is filling.

        The slot's `role` and `prompt_frame` are shown, not just its id and
        flags. A director that sees only "cluster_left: generatable=True" is
        picking a noun blind, and a blind pick falls back on whatever the
        GENRE suggests — which is how a novel of iron thorns and bone came
        back as a rose garden. The frame is the geometry the noun has to sit
        in ("fine and busy, on thin whipping lengths"; "one very large
        single, on a short severed stalk"), and the slot id is often a
        historical name for it rather than a description — `bloom_front` is
        the big lower-left feature, not a flower. Showing both is what lets
        the director choose FROM THE BOOK and still land in the composition.
        """
        a = self.archetype
        slots = []
        for s in a.art:
            head = (f"- {s.id}: generatable={s.generatable}, "
                    f"transparent={getattr(s, 'transparent', False)}"
                    + (", archetype-authored mask" if s.mask is not None
                       else ""))
            if s.role:
                head += f", role={s.role}"
            slots.append(head)
            frame = " ".join(s.prompt_frame.split())
            if frame:
                slots.append(f"    fills: {frame}")
            if s.cut_edge:
                slots.append(f"    severed end sits on: {s.cut_edge}")
        text = [f"archetype: {a.name}", a.describe.strip(), "",
                "ART SLOTS", *slots, "", "TEXT SLOTS"]
        text += [f"- {t.id}: zone {t.zone.model_dump()}" for t in a.text]
        return _text("\n".join(text))

    async def budget_left(self, args: dict[str, Any]) -> dict[str, Any]:
        return _text(self.budget.describe())

    async def paint(self, args: dict[str, Any]) -> dict[str, Any]:
        slot_id = str(args.get("slot", "")).strip()
        prompt = str(args.get("prompt", "")).strip()
        tier = str(args.get("resolution") or "1K").strip()
        if tier not in IMAGE_COST:
            return _text(f"resolution must be one of "
                         f"{', '.join(sorted(IMAGE_COST))}.", is_error=True)
        slot = next((s for s in self.spec.art if s.id == slot_id), None)
        if slot is None:
            return _text(
                f"No art slot {slot_id!r} on this cover. Slots: "
                f"{', '.join(s.id for s in self.spec.art)}.", is_error=True)
        if not prompt:
            return _text("paint needs a prompt.", is_error=True)
        refusal = self.budget.refusal(tier)
        if refusal:
            return _text(refusal, is_error=True)

        slot = slot.model_copy(update={"prompt": prompt})
        assembled = self.assemble_prompt(slot, self.archetype)
        try:
            if self.sem is not None:
                async with self.sem:
                    png = await asyncio.to_thread(
                        generate, self.image_client, assembled,
                        transparent=slot.transparent, resolution=tier)
            else:
                png = await asyncio.to_thread(
                    generate, self.image_client, assembled,
                    transparent=slot.transparent, resolution=tier)
        except Exception as e:                              # noqa: BLE001
            return _text(f"The generation failed: {e}", is_error=True)

        price = self.budget.charge(tier)
        rel = f"{ASSETS_DIR}/c{self.index}_{slot_id}.png"
        (self.job_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.job_dir / rel).write_bytes(png)
        for s in self.spec.art:
            if s.id == slot_id:
                s.prompt, s.asset = prompt, rel
        self.ledger.append({
            "kind": "image", "concept": self.index,
            "detail": f"concept {self.index} {slot_id} ({tier}, atelier)",
            "usd": price})

        note = f"Painted {slot_id} at {tier}. {self.budget.describe()}."
        if slot.transparent and not await asyncio.to_thread(has_real_alpha, png):
            note += ("\nNOTE: a cutout was requested and the plate came back "
                     "OPAQUE — compose will fall back to the §5.2.3 layer "
                     "order (title drawn on top of the focal). Look at the "
                     "composite before deciding whether to repaint.")
        return _text(note + "\nCall `render` to see it composed.")

    async def render(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            report, renders = await self._compose()
        except Exception as e:                              # noqa: BLE001
            return _text(f"The cover could not be composed: {e}",
                         is_error=True)
        return _text(_describe_report(report)
                     + f"\nrender: {renders[0] if renders else '(none)'}")

    async def look(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.composed:
            try:
                await self._compose()
            except Exception as e:                          # noqa: BLE001
                return _text(f"The cover could not be composed to look at "
                             f"({e}).", is_error=True)
        try:
            png = await asyncio.to_thread(self._look_png)
        except Exception as e:                              # noqa: BLE001
            return _text(f"The render could not be read back ({e}).",
                         is_error=True)
        return {"content": [
            {"type": "text", "text":
                "The cover as it stands. Judge it at this size — this is "
                "roughly what a browser sees."},
            {"type": "image",
             "data": base64.b64encode(png).decode("ascii"),
             "mimeType": "image/png"}]}

    async def edit_spec(self, args: dict[str, Any]) -> dict[str, Any]:
        raw = args.get("edits") or []
        if not isinstance(raw, list) or not raw:
            return _text("edit_spec needs a non-empty `edits` list.",
                         is_error=True)
        root = self.spec.model_dump(mode="json")
        skipped: list[str] = []
        for entry in raw:
            try:
                edit = SpecEdit(path=str(entry.get("path", "")),
                                value=str(entry.get("value", "")))
            except Exception as e:                          # noqa: BLE001
                skipped.append(f"{entry!r}: {e}")
                continue
            why = _apply_edit(root, edit)
            if why:
                skipped.append(why)
        try:
            candidate = CoverSpec.model_validate(root)
        except Exception as e:                              # noqa: BLE001
            return _text(
                f"Those edits do not add up to a valid cover spec, so none "
                f"were kept: {e}", is_error=True)

        self.spec = candidate
        try:
            report, renders = await self._compose()
        except Exception as e:                              # noqa: BLE001
            return _text(f"The edits applied but the cover could not be "
                         f"composed: {e}", is_error=True)
        head = "Edits applied and recomposed."
        if skipped:
            head += " Skipped: " + "; ".join(skipped)
        return _text(head + "\n" + _describe_report(report)
                     + f"\nrender: {renders[0] if renders else '(none)'}")

    async def finish(self, args: dict[str, Any]) -> dict[str, Any]:
        summary = str(args.get("summary", "")).strip()
        if not summary:
            return _text("finish needs a summary: what you shipped, what you "
                         "changed and why, and where this cover is weak.",
                         is_error=True)
        if not self.composed:
            return _text("Nothing has been composed yet — call `render` and "
                         "look at the cover before finishing.", is_error=True)
        self.finished = True
        self.summary = summary
        return _text("Recorded. This concept is done.")

    # -- machinery -----------------------------------------------------------

    async def _compose(self) -> tuple[RenderReport, list[str]]:
        image, report = await asyncio.to_thread(compose, self.spec,
                                                self.job_dir)
        renders = await asyncio.to_thread(
            self._save_renders, image, self.job_dir, self.spec.version,
            self.index)
        self.report = report
        self.renders = renders[:1]
        self.composed = True
        return report, self.renders

    def _look_png(self) -> bytes:
        from PIL import Image

        path = self.job_dir / self.renders[0]
        with Image.open(path) as im:
            im = im.convert("RGB")
            h = round(im.height * LOOK_WIDTH / im.width)
            im = im.resize((LOOK_WIDTH, h), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
        return buf.getvalue()

    def specs(self) -> list[_ToolSpec]:
        return [
            _ToolSpec("read_spec", "The current CoverSpec as JSON — every "
                      "zone, colour, font, effect and layer.", {},
                      self.read_spec),
            _ToolSpec("archetype_info", "This archetype's structure: its art "
                      "slot ids, which are generatable, which carry an "
                      "archetype-authored mask, and the text zones.", {},
                      self.archetype_info),
            _ToolSpec("budget", "Generations and dollars left.", {},
                      self.budget_left),
            _ToolSpec(
                "paint",
                "Generate the art for one slot and attach it. COSTS MONEY. "
                "The prompt is a scene with a medium, a light direction and "
                "a camera — never text, lettering or logos. `resolution` is "
                "1K (draft, ~$0.03) or 2K (~$0.05); draft first, escalate "
                "only a keeper.",
                {"type": "object",
                 "properties": {
                     "slot": {"type": "string"},
                     "prompt": {"type": "string"},
                     "resolution": {"type": "string",
                                    "enum": sorted(IMAGE_COST)}},
                 "required": ["slot", "prompt"]},
                self.paint),
            _ToolSpec("render", "Compose the cover and return the render "
                      "report. Free, unlimited.", {}, self.render),
            _ToolSpec("look", "See the composed cover, downscaled to about "
                      "what a browser thumbnail shows.", {}, self.look),
            _ToolSpec(
                "edit_spec",
                "Patch the spec by JSON path and recompose. Free. Paths are "
                "dotted with [n] indices, values are JSON-encoded strings — "
                'e.g. {"path": "palette.accent", "value": "\\"#a83250\\""} '
                'or {"path": "text[0].zone.y", "value": "0.12"}. version, '
                "notes_log and art asset paths are code's alone to write.",
                {"type": "object",
                 "properties": {"edits": {
                     "type": "array",
                     "items": {"type": "object",
                               "properties": {"path": {"type": "string"},
                                              "value": {"type": "string"}},
                               "required": ["path", "value"]}}},
                 "required": ["edits"]},
                self.edit_spec),
            _ToolSpec(
                "finish",
                "Declare this cover done and stop. Say what you shipped, "
                "what you changed between passes and why, and where it is "
                "weak.",
                {"type": "object",
                 "properties": {"summary": {"type": "string"}},
                 "required": ["summary"]},
                self.finish),
        ]


def _system_prompt() -> str:
    return f"""You are a book-cover designer executing ONE assigned concept \
in Cover Studio. A director has read the whole manuscript and given you the \
design, the traps it sets, and what finished looks like. Build it.

HOW THIS WORKS. `paint` buys art layers and costs real money. Everything \
else is free: `render` composes and returns the engine's report, `look` shows \
you the result, `edit_spec` patches the spec by JSON path and recomposes. The \
composer sets ALL type itself with embedded fonts — never ask an art prompt \
for text, lettering, signage copy, numbers or logos.

PLAN BEFORE YOU SPEND. Read the spec and the archetype first. Work out which \
trap this design sets and which clause of your prompt defends against it. \
Your first generation should be one you have already argued yourself into. \
Most good covers need one or two.

ITERATE FOR FREE. A spec edit and a recompose cost nothing. Repaint only when \
the PIXELS are wrong — when no edit to zones, colour, type, scrims, masks or \
effects could fix it — and say so when you finish.

READ THE REPORT. An AUTOPILOT line means the engine changed your design to \
keep the type legible: your art fought your type, and the fix belongs at the \
source, not in the scrim. Warnings, dead_band, occlusion and contrast are all \
the engine telling you what it had to do.

JUDGE AT THUMBNAIL SIZE. `look` shows you roughly what a browser sees. A cover \
that only works at full size does not work.

FINISH DELIBERATELY. Call `finish` when the cover is right, with an honest \
summary including where it is weak. Do not stop by going quiet.

{doctrine.render("atelier")}"""


def _user_prompt(brief: Brief, assignment: Any, index: int) -> str:
    d = assignment.direction
    prompts = "\n".join(f"  - {p.slot}: {p.prompt}" for p in d.art_prompts) \
        or "  (none — this concept is procedural)"
    return f"""YOUR ASSIGNMENT — concept {index}: {d.concept_name}

THE BOOK
Title: {brief.title}
Author: {brief.author}
Genre: {brief.genre}
{("Mood: " + brief.mood) if brief.mood else ""}
{("Avoid: " + brief.avoid) if brief.avoid else ""}

WHY THIS COVER
{d.rationale}

THE DESIGN
archetype: {d.archetype}
palette: {d.palette.model_dump()}
title font: {d.title_font} · author font: {d.author_font}
recipe: {d.recipe or "(none)"} · type move: {d.type_move or "(none)"}
art prompts the director drafted:
{prompts}

WHAT WILL GO WRONG IF YOU ARE CARELESS
{assignment.execution_notes}

DONE WHEN
{assignment.done_when}

The spec is already built from this design — read it, check it against the \
archetype, improve the art prompts before you spend, then build and iterate."""


def _options(sdk: Any, session: _Session, model: str) -> Any:
    """The agent's whole world. Every line is a fence, for the reasons
    docproof.canvas.assistant._options gives: no built-in tools, no MCP
    config from this repo, no CLAUDE.md, and a cwd that is the job directory
    rather than the source tree."""
    specs = session.specs()
    server = sdk.create_sdk_mcp_server(
        name=SERVER_NAME, tools=agent_lane.sdk_tools(sdk, specs))
    return sdk.ClaudeAgentOptions(
        model=model,
        system_prompt=_system_prompt(),
        mcp_servers={SERVER_NAME: server},
        allowed_tools=[f"mcp__{SERVER_NAME}__{s.name}" for s in specs],
        tools=[],
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="bypassPermissions",
        max_turns=MAX_TURNS,
        cwd=str(session.job_dir),
        env=agent_lane.child_env(),
    )


def _sdk() -> Any:
    return agent_lane.sdk(_INSTALL_HINT)


def _require_login() -> None:
    agent_lane.require_login(_LOGIN_HINT)


async def build_concept(*, job_dir: Path, index: int, brief: Brief,
                        assignment: Any, spec: CoverSpec, image_client: Any,
                        assemble_prompt: Callable[[ArtSlot, Archetype], str],
                        save_renders: Callable[..., list[str]],
                        sem: asyncio.Semaphore | None = None,
                        budget: Budget | None = None,
                        model: str | None = None) -> ConceptOutcome:
    """Run one concept's agent to completion and report what it produced.

    Never raises for anything that is one concept's problem: an SDK failure,
    a dead session, an agent that runs out of turns all come back as a
    ConceptOutcome carrying `error` and whatever was composed before the
    trouble, so a sibling concept is unaffected. The two exceptions are the
    lane's own refusals (no SDK, no login), which are the whole JOB's problem
    and are raised for the caller to fail the job on."""
    sdk = _sdk()
    _require_login()

    session = _Session(
        job_dir=Path(job_dir), index=index, brief=brief, spec=spec,
        archetype=ARCHETYPES[spec.archetype], image_client=image_client,
        assemble_prompt=assemble_prompt, budget=budget or Budget(), sem=sem,
        save_renders=save_renders)
    options = _options(sdk, session, resolve_model(model))
    message = _user_prompt(brief, assignment, index)

    async def prompt():
        yield {"type": "user",
               "message": {"role": "user", "content": message}}

    error: str | None = None
    try:
        async for msg in sdk.query(prompt=prompt(), options=options):
            if isinstance(msg, sdk.ResultMessage) and msg.total_cost_usd:
                # Subscription turns report 0.0; whatever it says is passed
                # through rather than estimated, never invented.
                session.ledger.append({
                    "kind": "atelier", "concept": index,
                    "detail": f"concept {index} agent session",
                    "usd": float(msg.total_cost_usd)})
    except agent_lane.AgentLaneUnavailable:
        raise
    except sdk.CLINotFoundError as e:
        raise agent_lane.AgentLaneUnavailable(_CLI_HINT) from e
    except (sdk.ProcessError, sdk.ResultError) as e:
        raise agent_lane.AgentLaneUnavailable(
            f"{_SUBJECT} could not start a Claude session ({e}). Sign this "
            f"machine in once with `claude setup-token` or `claude /login`, "
            f"then try again.") from e
    except Exception as e:                                  # noqa: BLE001
        log.exception("Atelier concept %d failed", index)
        error = f"This concept's designer stopped early: {e}"

    if not session.finished and error is None and session.composed:
        error = ("This concept's designer ran out of turns before calling "
                 "finish; the last composed version was kept.")
    elif not session.composed and error is None:
        error = "This concept's designer never composed a cover."

    return ConceptOutcome(
        spec=session.spec, report=session.report, renders=session.renders,
        ledger=session.ledger, summary=session.summary,
        finished=session.finished, error=error)


__all__ = ["ASSETS_DIR", "AtelierUnavailable", "Budget", "ConceptOutcome",
           "DEFAULT_MODEL", "LOOK_WIDTH", "MAX_ART_USD", "MAX_GENERATIONS",
           "MAX_TURNS", "MODEL_ENV", "RENDERS_DIR", "SERVER_NAME",
           "build_concept", "resolve_model"]
