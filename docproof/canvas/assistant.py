"""The AI box's brain: the §15 art director, resident in the canvas.

This is the server side of docs/cover_canvas_spec.md §6. A person types "move
the title down and left, off her face" and something has to (a) know the
doctrine well enough to agree that it should come off her face, (b) see the
cover, and (c) make the change through the SAME op vocabulary a drag produces,
so the edit lands in the undo log like any other. That is the whole design:
the assistant is not a second edit path, it is a second hand on the first one.

Three decisions this module is built around:

- **Claude Code as the driver, on the subscription.** The brain is the Agent
  SDK spawning the CLI, exactly the Galley pattern — which means the child
  process must never see an API key, or a $0 assistant turn quietly becomes a
  billed one. `_child_env` is that guard and it is the most load-bearing
  fifteen lines in the file.
- **Stateless between calls.** The UI owns the transcript; every call flattens
  it into one prompt. A resident session object would be a second source of
  truth for a conversation the browser already has, and it would not survive a
  server restart the way the canvas document does.
- **The tools ARE the ops.** `apply_ops` is a thin skin over
  docproof.canvas.ops.apply_many, so an assistant edit is validated, recorded
  in `history`, and undone by exactly the machinery a click uses. When an op
  is refused, the refusal SENTENCE goes back to the model as the tool result —
  ops.py writes its errors for a reader, and the reader here is the thing that
  can fix its own mistake and try again.

Four tools are the exception to that last rule, because no op can express
them: `reroll`, `finalize` and `ground_figure` write new PIXELS onto an art
layer and spend real money doing it, and `rebalance` measures a plate before
it nudges one. All four are thin skins over docproof.canvas.regen — the same
verbs §5's button shelf fires — so the AI box and the buttons cannot drift
apart, and all four are imported LAZILY, so a canvas with no regeneration
lane is still a working canvas with a working assistant.

Plan mode is enforced by ABSENCE, not by instruction: the mutating tools are
simply not registered, so a plan-mode turn cannot edit the document even if
the model decides it should.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import importlib
import io
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import ops as canvas_ops
from .model import CanvasDoc

# Opus 5 is the owner's pick for the driver (a cover edit is a judgment call,
# and the turn is subscription-covered either way), but a default is not a
# pin: the env var exists so a slower/cheaper model can be tried on a whole
# session without a code change.
DEFAULT_MODEL = "claude-opus-5"
MODEL_ENV = "DOCPROOF_CANVAS_ASSISTANT_MODEL"

# Bounded so a confused turn cannot spin. Roughly: inspect, look, two or three
# apply_ops rounds with a self-correction each, and a summary — with headroom.
MAX_TURNS = 25

SERVER_NAME = "canvas"

# How wide `look` renders the cover for the model's eye. Big enough to judge
# type weight and a face against a background, small enough that a turn can
# afford to look several times — which is the whole point of a tool that can
# now be called after every edit.
LOOK_WIDTH = 900

# What a look may weigh, and the ladder it climbs down to get there.
#
# A tool result travels back through the CLI transport as ONE line of JSON,
# and the SDK reads those lines into a 1MB buffer — a frame over that size is
# not truncated, it is a CLIJSONDecodeError that kills the whole turn. A
# 900px PNG of a real cover is several megabytes of base64 and blew up every
# turn that looked. So: JPEG, like the browser's own snapshot already learned
# to be (engine.js snapshotBase64: "a cover photograph is roughly a tenth the
# size as JPEG"), with a budget checked against the ENCODED bytes rather than
# assumed from the dimensions — a busy plate and a flat one differ by an
# order of magnitude at the same size.
#
# The budget is the binary size; base64 is a third larger again, and the
# frame carries the rest of the message around it. 480KB encodes to ~640KB
# and leaves the megabyte comfortable.
LOOK_MAX_BYTES = 480_000

# Quality first, then size: a slightly softer picture is a better answer than
# a smaller one when what is being judged is composition. Every rung is
# tried in order and the first that fits is sent.
LOOK_LADDER: tuple[tuple[int, int], ...] = (
    (LOOK_WIDTH, 80), (LOOK_WIDTH, 65), (700, 65), (560, 60), (420, 55),
)

# The measuring grid `look(grid=true)` draws over that render: a line every
# tenth of the canvas, labelled in the document's OWN units (fractions), so
# a position read off the picture is already the number an op takes. Thirds
# are drawn heavier because the doctrine talks in thirds, and the centre
# cross heavier still.
GRID_STEP = 0.1
GRID_THIRDS = (1 / 3, 2 / 3)

_INSTALL_HINT = (
    "The canvas assistant needs the Claude Agent SDK, which is not installed "
    "in this environment — install it with `pip install claude-agent-sdk` "
    "(and the Claude Code CLI it drives) and reopen the canvas.")

_LOGIN_HINT = (
    "The canvas assistant runs on your Claude subscription and this machine "
    "is not logged in — run `claude setup-token` in a terminal and set "
    "CLAUDE_CODE_OAUTH_TOKEN, or run `claude` once to sign in, then reopen "
    "the canvas.")

_CLI_HINT = (
    "The canvas assistant could not find the Claude Code CLI it drives — "
    "install it (`npm install -g @anthropic-ai/claude-code`) and reopen the "
    "canvas.")


class AssistantUnavailable(RuntimeError):
    """The AI box cannot run here, with a sentence saying what to do.

    Always a human sentence naming the missing piece and the command that
    fixes it: this text is rendered straight into the AI box, where "no
    module named claude_agent_sdk" would be a dead end for the person the
    canvas was built for."""


@dataclass
class ChatResult:
    """One assistant turn's outcome.

    `doc` is a COPY — the caller persists it (or throws it away, if the turn
    is abandoned), which is what keeps a failed turn from half-editing the
    document on disk. `ops_applied` is the op log the UI folds into undo;
    `cost_usd` is the assistant turn only, and is normally 0.0 because the
    subscription covers it. Image spend from `reroll`, `finalize` and
    `ground_figure` lands on `doc.cost_usd` instead, where the canvas's
    running total already lives — it is never added here as well, or one
    plate would be billed to the person twice on one screen."""

    reply: str
    doc: CanvasDoc
    ops_applied: list[dict]
    cost_usd: float


# -- the doctrine -------------------------------------------------------------

# docs/cover_designer_spec.md §15.18-15.23, compressed to what a person editing
# a finished cover can act on. Distilled rather than referenced: the spec is
# 1,500 lines of provenance and build lists, and the model needs the rules, in
# the room, every turn.
DOCTRINE = """\
1.  Focal dominance: the subject is the loudest thing on the cover. If type or
    a prop out-shouts it, fix the subject's scale or value — do not quiet the
    title to compensate.
2.  CARDINAL: a standing figure must LOOK like it is standing on something. A
    reader adjudicates this instantly and without vocabulary, and no palette,
    type or mark survives behind a floating figure.
3.  When the plate has no floor, the answer is a new near-band plate, not
    wedging the figure onto the nearest horizontal-ish surface. Generate the
    ground; do not cram them onto some other platform.
4.  Grounding a cutout is a STACK and all of it is required: a receiving
    surface at the feet; a cast shadow on a plane lifted light enough to show
    it; a hard, unblurred weld at the contact (2-3px); and no rim light in the
    bottom 5% of the figure. A lit edge at the contact reads as hovering and
    outvotes correct shadows.
5.  A cutout implies its integration work at the moment you ask for it —
    enumerate the grounding when you plan the plate, not when someone
    complains it floats.
6.  Depth bands must differ in VALUE, not only in z-order: blend the far band
    toward the sky behind it. Correct layer order makes depth stop being
    thought about.
7.  Ground contacts are shown-and-seated or hidden entirely, never mixed. One
    visible termination against sky or canopy floats the whole scene.
8.  Type has homes: eyebrow above the title, author line on a painted stable
    ground. A scrim fighting busy texture at escalated strength reads as a
    bezel, not as protection.
9.  Clipped art — art visible only through letterforms — must be value-
    OPPOSITE to its field and uniform edge to edge. A title that still needs a
    scrim is a failed value direction, not a protected title.
10. When the real object is the wrong value, change the reproduction medium,
    not the palette: microfilm negative, blueprint, photostat, X-ray.
11. Type and palette are claims about the world, not decoration over it. A
    face or a hue from a different story reads "off" without the reader being
    able to name why.
12. Dead space is the enemy: a flat band over roughly a fifth of the height is
    a failure, not breathing room.
13. Measure before you move. `inspect` and `look` are the measurements; an
    estimate off a preview carries ~10% error. Never assert a clearance, a
    contact or a containment you have not actually read.
14. A number proves legality, not quality. Look at the cover before making any
    claim about it, and use each number only for the question it answers.
15. Every element needs a reason on the page. A prop with no story job dies in
    review.
16. If a fix does not move the read, change the KIND of move rather than its
    parameters again. Two failed refinements of one approach is the signal to
    swap approaches.
17. Fewest ops that achieve the ask. You are editing one thing a person named,
    not re-litigating their cover."""

GEOMETRY = """\
Everything in the document is a FRACTION — nothing is in pixels. `canvas.w/h`
is only the reference resolution the fractions are read against.

- `frame.x` / `frame.y` are the CENTER of the layer's box, not its top-left.
  0.5/0.5 is dead center; smaller x is further left, smaller y is further UP.
- `frame.w` is a fraction of canvas WIDTH, `frame.h` a fraction of canvas
  HEIGHT. Both must be > 0 (hide a layer with `visible`, never with w=0).
- `frame.rotation` is degrees clockwise about that center; `flip_h`/`flip_v`
  mirror in place.
- A text layer's `size` is a fraction of canvas HEIGHT. `tracking` is an em
  fraction. Colors are literal "#rrggbb". There is NO auto-wrap: "\\n" inside
  `text` is the only line break.
- `layers` is listed BOTTOM to TOP — index 0 is behind everything."""

OPS = """\
set_frame       {"op":"set_frame","layer_id":"ly_ab12","x":0.42,"y":0.63}
set_frame       {"op":"set_frame","layer_id":"ly_ab12","corners":[[0.08,0.12],[0.94,0.05],[0.97,0.88],[0.11,0.95]]}
nudge           {"op":"nudge","layer_id":"ly_ab12","dx":-0.2,"dy":0.05}
set_text        {"op":"set_text","layer_id":"ly_cd34","size":0.09,"color":"#f5f1e8"}
set_layer       {"op":"set_layer","layer_id":"ly_ab12","visible":false,"locked":false}
set_art         {"op":"set_art","layer_id":"ly_ab12","fit":"contain","source":"assets/canvas_ly_ab12_1.png"}
set_scrim       {"op":"set_scrim","layer_id":"ly_ef56","color":"#0b0f14","gradient":{"angle":90,"stops":[{"at":0.0,"alpha":0.0},{"at":1.0,"alpha":0.65}]}}
set_frame_style {"op":"set_frame_style","layer_id":"ly_gh78","preset":"double_rule","stroke":"#c9a227","stroke_w":0.004,"inset":0.03}
set_shape       {"op":"set_shape","layer_id":"ly_ij90","shape":"rect","fill":"#101820","radius":0.02}
set_effects     {"op":"set_effects","layer_id":"ly_ab12","effects":[{"type":"drop_shadow","params":{"dx":0.01,"dy":0.02,"blur":0.01}}]}
add_layer       {"op":"add_layer","layer":{"id":"ly_new001","kind":"scrim","frame":{"x":0.5,"y":0.8,"w":1.0,"h":0.3},"color":"#000000","gradient":{"angle":90,"stops":[{"at":0.0,"alpha":0.0},{"at":1.0,"alpha":0.7}]}},"index":2}
remove_layer    {"op":"remove_layer","layer_id":"ly_ab12"}
reorder_layer   {"op":"reorder_layer","layer_id":"ly_ab12","index":4}
set_wrap        {"op":"set_wrap","spine_in":0.75}

- A locked layer refuses every op but `set_layer`, which is where unlocking
  lives — set `locked: false` first, in its own op.
- `corners` is the corner pin: four canvas-fraction [x,y] points in TL, TR,
  BR, BL order that skew how the pixels sit INSIDE the box. x/y/w/h/rotation
  still own where the box is. `"corners": null` unpins the layer.
- `set_art`'s `source` only accepts a plate this layer already has — the one
  it shows, or one from its history strip. It is the swap-back, not a file
  setter; new pixels come from `reroll`. The strip is never consumed by a
  swap, so you can hop back and forth.
- `set_scrim`, `set_frame_style` and `set_shape` edit those layers' own
  parameters IN PLACE. Never remove-and-re-add a layer to change a colour: it
  loses the id, the stacking position and the undo the person expects.
- `set_effects` states the WHOLE stack; it replaces, never appends.
- `add_layer` brings its own id ("ly_" + 6 hex characters, unique in the
  document) and its own `kind`; `index` is the position in the finished
  bottom-to-top list, and omitting it puts the layer on top.
- Every typed op needs at least one field and is kind-checked: `set_shape` at
  a title is refused, not ignored.
- A batch is all-or-nothing: if op 4 is refused, ops 1-3 did not happen
  either. Read the refusal, fix the batch, send it again.
- `set_wrap` takes no `layer_id` — it re-measures the print wrap itself
  (`spine_in`, `bleed_in`, `dpi`) on a document that already has one, and
  every layer keeps its place on its panel. It cannot change trim: a new
  trim size is a different book, not a wider spine."""

VERBS = """\
Four tools reach past the op vocabulary at an art layer's PIXELS. Three of
them spend real money and none of them is destructive: the plate each one
replaces stays on that layer's history strip, and `set_art` swaps it back.

- `reroll` — roll the prompt again, or a tweaked one, for a NEW picture.
  There is no seed, so it comes back genuinely different and every clearance
  the type was arranged against is gone. Costs money.
- `finalize` — keep this plate, then make it crisp. Re-renders the CURRENT
  plate at full quality with the composition anchored to the draft itself,
  so the layout survives the render. Reach for it when the person says the
  plate is right; never as a way to ask for a different picture. Costs money.
- `ground_figure` — rules 2-4 as one button. Regenerates a band across the
  bottom of the plate into real ground with a contact shadow, so a standing
  figure stands on something; add the scene's own specifics ("wet
  cobblestones") as the instruction. Costs money.
- `rebalance` — measure, then nudge. Reads one plate's exposure, contrast
  spread, mirror symmetry and centre of mass, and lands a bounded levels
  correction as an ordinary effect that undoes like any other edit.
  Costs NOTHING, and it reports every number it read — so it is the cheap
  answer to rule 13, and the thing to try before a re-roll."""

_MODE_CONDUCT = """\
ANSWER THE THING THAT WAS ASKED — in both modes, and before everything below.
A greeting is a greeting: say hello, say in one line what you can see on the
canvas, and stop. A question gets an answer. Only a request for a critique or
a change gets a critique or a change. Sweeping the whole cover for defects
because somebody typed "hi" hands them a wall of work they did not ask for,
and it buries the one thing that actually matters.

When you DO critique, lead with the single worst problem and give at most
three items unless you are asked for everything. Each one names the layer,
says what to do, and is something the person could accept or refuse on the
spot. If there is nothing wrong, say so — an empty list is a real answer.

PLAN MODE — you have `inspect` and `look` and nothing else; the mutating tools
are not loaded, so you cannot change the cover even by accident. When a
diagnosis IS what was asked for: diagnose what is actually there, cite the
doctrine rule a defect breaks, and answer with a NUMBERED plan whose steps
each name the layer and the op that would do it. Do not describe a change as
if you had made it — offer instead to make it in Act mode.

ACT MODE — carry out the direction with the fewest ops that achieve it,
preferring one `apply_ops` batch over several. `inspect` before your first
edit of a conversation: the layer ids and the current frames are the
measurement, and a batch aimed at a remembered id is a batch that gets
refused. When an op comes back refused, read the sentence — it says exactly
what was wrong — fix it and retry rather than reporting failure. Finish with
one or two sentences saying what changed, in the person's own terms ("the
title moved off her face and up 4%"), never a list of op dicts."""

MEASURE = """\
Measure before you judge, and look again after you act. You have three
instruments and they are all cheap:

- `inspect` — the numbers the document actually holds: every layer's id, kind
  and frame. Positions come from here, never from memory of an earlier turn.
- `look(grid=true)` — the cover re-rendered with a labelled grid over it,
  tenths of the canvas in the same fractions the ops take. This is how a
  judgement becomes an edit: not "the title is too high" but "the title's
  centre reads at y≈0.31, the plate's horizon is at 0.46, put it at 0.24".
  Say the numbers you read; a number you did not read off the grid is a
  guess, and a guess spent on a plate is money.
- `rebalance` — measures a plate's exposure, contrast spread, symmetry and
  centre of mass and reports every number, for nothing. It is the FIRST
  answer to a flat or muddy plate, not the last: try it before proposing a
  re-roll that costs money and returns a different picture.

The loop, in Act mode: measure, change ONE thing, `look` at what you did,
say whether it worked. `look` re-renders the live document, so after an edit
it shows the edit — a claim about a change you did not look at is a claim you
have not checked. Two or three passes is a working session; a fourth without
improvement means the plate is wrong, not the layout, and you should say so
instead of spending again. Every plate verb costs real money, so state what
you are about to buy and why before you buy it, and never roll twice in a row
without looking in between."""

SYSTEM_PROMPT = f"""\
You are the resident art director for Cover Canvas, a layered editor for book
covers. Someone is looking at a cover right now and telling you what is wrong
with it. You know the house doctrine, you can see the cover, and you edit it
through the same ops their own mouse produces — so anything you do, they can
undo.

Speak like a designer, not a tool: short, concrete, opinionated. Say when a
requested change will break a rule below, do the change anyway if they insist,
and never lecture twice about the same thing.

## The doctrine

{DOCTRINE}

## The document

{GEOMETRY}

## The ops

{OPS}

## The plate verbs

{VERBS}

## Measuring

{MEASURE}

## Conduct

{_MODE_CONDUCT}"""


# -- the working session ------------------------------------------------------

@dataclass
class _ToolSpec:
    """One tool, described without touching the SDK.

    The registry is built as plain data so "which tools does plan mode get"
    is answerable — and testable — without spawning a CLI or importing the
    agent SDK at all. `_sdk_tools` is the only place that turns these into
    SDK objects."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]


def _fake_active() -> bool:
    """Whether regen's $0 stand-in lane is on (DOCPROOF_CANVAS_FAKE_IMAGING).

    Asked through the lazy import the plate tools already use, and answered
    False on a build with no regeneration module at all. The lane is a
    property of the MACHINE, and the three places this file consults it —
    the registry gate, the refusal the spending verbs share, and the client
    factory — must never disagree with each other or with regen's own
    answer: a tool that is registered and then refuses itself is worse than
    one that was never offered."""
    try:
        from .regen import fake_active
    except ImportError:
        return False
    return bool(fake_active())


@dataclass
class _Session:
    """One turn's mutable state: the working document and what happened to it.

    Everything the tools touch lives here rather than in closures, so the
    handlers are ordinary methods a test can call directly with a dict."""

    job_dir: Path
    doc: CanvasDoc
    mode: str
    snapshot_png: bytes | None = None
    image_client: Callable[[], Any] | None = None
    ops_applied: list[dict] = field(default_factory=list)
    # Where doc.history stood when the turn began — the mark `_edited` reads
    # to know whether the client's snapshot is still the truth. Set in
    # __post_init__ rather than defaulted to 0, because a document that has
    # been edited before today arrives with a history already in it.
    history_at_start: int = -1

    def __post_init__(self) -> None:
        if self.history_at_start < 0:
            self.history_at_start = len(self.doc.history)

    # -- tools --

    async def inspect(self, args: dict[str, Any]) -> dict[str, Any]:
        """The document as compact JSON, bottom to top.

        Deliberately small enough to re-call after every batch: the model
        needs current ids and frames more often than it needs any one field,
        and a description it is afraid to refresh goes stale silently. Long
        prompts are clipped for the same reason — the prompt's SUBJECT is
        what identifies a plate, not its style block."""
        return _text(json.dumps(_summarize(self.doc), separators=(",", ":")))

    async def apply_ops(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run a batch of ops against the working copy.

        A refusal comes back as ops.py's own sentence rather than as an
        exception, because the model is the one that can fix it: "layer
        'ly_a91f' is locked, so nudge was refused — unlock it first with
        set_layer (locked=false)" is a repair instruction, and a stack trace
        is not."""
        raw = args.get("ops")
        if not isinstance(raw, list):
            return _text(
                "apply_ops takes an `ops` list of op dicts, like "
                "[{\"op\": \"nudge\", \"layer_id\": \"ly_ab12\", \"dx\": -0.2}]",
                is_error=True)
        try:
            canvas_ops.apply_many(self.doc, raw)
        except canvas_ops.OpError as e:
            return _text(str(e), is_error=True)
        self.ops_applied.extend(copy.deepcopy(op) for op in raw)
        names = ", ".join(str(op.get("op")) for op in raw)
        return _text(
            f"applied {len(raw)} op{'' if len(raw) == 1 else 's'} ({names}); "
            f"layers bottom to top: "
            f"{', '.join(l.id for l in self.doc.layers) or 'none'}")

    # -- the plate verbs --
    #
    # Four tools that are not ops (see the module docstring). The three that
    # spend money share a shape, deliberately and visibly: lazy import, a
    # `layer_id` that has to be a real string, the image-lane guard, the call
    # off the event loop, and every failure returned as a tool RESULT rather
    # than raised — the model is the thing that can pick a different layer or
    # a different verb, and it cannot read a traceback the turn died on.

    def _no_image_lane(self, phrase: str) -> dict[str, Any] | None:
        """The image-key refusal the three spending verbs share, or None to
        go ahead.

        `phrase` is the verb in the person's own words ("re-rolling",
        "finalizing") so a refusal reads as that button's refusal and not as
        a generic capability error."""
        if self.image_client is None and not _fake_active():
            return _text(f"{phrase} is not available in this session — no "
                         f"image key is configured", is_error=True)
        return None

    def _vendor_client(self) -> Any:
        """The image client for one call, or None in the $0 stand-in lane —
        where no client is built at all, because constructing one is exactly
        what fails on a machine with no key. Called INSIDE each handler's
        try, so a key that turns out to be broken is a sentence too."""
        return None if _fake_active() else self.image_client()

    async def reroll(self, args: dict[str, Any]) -> dict[str, Any]:
        """Regenerate one art layer's plate, optionally from a new prompt.

        The import is lazy and its failure is a tool result, not a crash: the
        regeneration lane is a separate module with a separate dependency
        (an image client), and a canvas whose assistant cannot re-roll is
        still a working canvas."""
        try:
            from .regen import reroll as _reroll
        except ImportError:                                 # pragma: no cover
            return _text(
                "re-rolling is not available in this build — the "
                "regeneration module is missing", is_error=True)
        layer_id = args.get("layer_id")
        if not isinstance(layer_id, str) or not layer_id:
            return _text("reroll needs the `layer_id` of the art layer to "
                         "regenerate", is_error=True)
        prompt = args.get("prompt") or None
        refusal = self._no_image_lane("re-rolling")
        if refusal is not None:
            return refusal
        try:
            # Off the loop: a generation takes tens of seconds, and the SDK's
            # control protocol (this very tool call's transport) is running on
            # the same event loop — a blocking call here stalls the agent that
            # is waiting for the answer.
            cost = float(await asyncio.to_thread(
                _reroll, self.job_dir, self.doc, layer_id,
                client=self._vendor_client(), prompt=prompt))
        except Exception as e:
            return _text(f"the re-roll failed: {e}", is_error=True)
        # Image spend already landed on doc.cost_usd inside regen (§8's
        # running total); `cost` here is only for the sentence back to the
        # model. Adding it again would double-charge the session display.
        return _text(
            f"layer {layer_id} now shows {self._source(layer_id)} — that "
            f"re-roll cost ${cost:.2f}, and the previous plate is kept in "
            f"the layer's history.")

    async def finalize(self, args: dict[str, Any]) -> dict[str, Any]:
        """Re-render this art layer's CURRENT plate at full quality.

        The top of §5's quality ladder, and the reason drafts are worth
        rolling cheap: a re-roll asks for the same WORDS again and the image
        model has no seed, so it answers with a different picture and every
        clearance the type was arranged against is gone. This feeds the kept
        plate back through the edit endpoint instead, so the draft itself
        anchors the composition and only the craft changes."""
        try:
            from .regen import finalize as _finalize
        except ImportError:                                 # pragma: no cover
            return _text(
                "finalizing is not available in this build — the "
                "regeneration module is missing", is_error=True)
        layer_id = args.get("layer_id")
        if not isinstance(layer_id, str) or not layer_id:
            return _text("finalize needs the `layer_id` of the art layer to "
                         "re-render at full quality", is_error=True)
        # Emphasis appended to the fidelity instruction, never a replacement
        # for it — regen owns that wording, and a prompt that arrived here as
        # a whole new description would quietly turn a finalize into a roll.
        prompt = args.get("prompt") or None
        refusal = self._no_image_lane("finalizing")
        if refusal is not None:
            return refusal
        try:
            cost = float(await asyncio.to_thread(
                _finalize, self.job_dir, self.doc, layer_id,
                client=self._vendor_client(), prompt=prompt))
        except Exception as e:
            return _text(f"the finalize failed: {e}", is_error=True)
        # Charged on the document, not on the turn — same rule as reroll.
        return _text(
            f"layer {layer_id} is now a full-quality render of the same "
            f"plate, {self._source(layer_id)} — that cost ${cost:.2f}, and "
            f"the draft it was rendered from is kept in the layer's history. "
            f"Anything measured off the draft's exact pixels should be "
            f"measured again.")

    async def ground_figure(self, args: dict[str, Any]) -> dict[str, Any]:
        """Give the figure on this plate something to stand on (§15.23).

        The cardinal rule as one call. The mask is the SERVER's — a band
        across the bottom of the plate — which is what makes this a button
        rather than a drawing exercise: §15.23's answer is always a floor at
        the bottom of the frame, so nobody has to trace the same rectangle
        every time."""
        try:
            from .regen import ground_figure as _ground
        except ImportError:                                 # pragma: no cover
            return _text(
                "grounding a figure is not available in this build — the "
                "regeneration module is missing", is_error=True)
        layer_id = args.get("layer_id")
        if not isinstance(layer_id, str) or not layer_id:
            return _text("ground_figure needs the `layer_id` of the art "
                         "layer whose figure is floating", is_error=True)
        instruction = args.get("instruction") or None
        refusal = self._no_image_lane("grounding a figure")
        if refusal is not None:
            return refusal
        try:
            cost = float(await asyncio.to_thread(
                _ground, self.job_dir, self.doc, layer_id,
                client=self._vendor_client(), instruction=instruction))
        except Exception as e:
            return _text(f"the grounding failed: {e}", is_error=True)
        return _text(
            f"layer {layer_id} now has generated ground and a contact shadow "
            f"under the figure, {self._source(layer_id)} — that cost "
            f"${cost:.2f}, and the previous plate is kept in the layer's "
            f"history. Look at the contact before you call it grounded.")

    async def rebalance(self, args: dict[str, Any]) -> dict[str, Any]:
        """Measure this art layer's plate and land a bounded exposure nudge.

        The only free verb on the shelf: no vendor call happens at all, so it
        is registered whenever the turn can edit, with or without an image
        key. Its whole answer is regen's measured SENTENCE, returned verbatim
        — every number that drove the correction, so "why did it do that" is
        answered without the model having to ask again."""
        try:
            from .regen import rebalance as _rebalance
        except ImportError:                                 # pragma: no cover
            return _text(
                "rebalancing is not available in this build — the "
                "regeneration module is missing", is_error=True)
        layer_id = args.get("layer_id")
        if not isinstance(layer_id, str) or not layer_id:
            return _text("rebalance needs the `layer_id` of the art layer to "
                         "measure", is_error=True)
        # regen.rebalance lands its correction through ops.apply on this
        # working document, which records it in doc.history — but NOT in this
        # session's ops_applied, where apply_ops is the only thing that ever
        # appends. The UI folds ops_applied into its undo stack, so the ops
        # regen appended are copied across from history's tail: without this
        # the one edit a person is most likely to want back is the one edit
        # they cannot undo. Read as a RANGE rather than as "the last op", so
        # a regen that ever applies two stays honest here.
        before = len(self.doc.history)
        try:
            sentence = await asyncio.to_thread(
                _rebalance, self.job_dir, self.doc, layer_id)
        except Exception as e:
            return _text(f"the rebalance failed: {e}", is_error=True)
        self.ops_applied.extend(
            copy.deepcopy(op) for op in self.doc.history[before:])
        return _text(str(sentence))

    def _source(self, layer_id: str) -> str:
        """The plate a layer is showing, for the sentence a plate verb
        reports back. Defensive because the sentence is not worth losing a
        landed regeneration over."""
        try:
            return str(self.doc.layer(layer_id).source)
        except (KeyError, AttributeError):                  # pragma: no cover
            return "a new plate"

    def _edited(self) -> bool:
        """Whether this turn has changed the document yet.

        Read off `doc.history` rather than tracked by each handler: every op
        records itself there (ops.apply) and so does every plate verb
        (regen appends its own entry), so one length comparison cannot fall
        out of step with a verb somebody adds later."""
        return len(self.doc.history) > self.history_at_start

    def _render_look(self, grid: bool) -> bytes:
        """The working document drawn from scratch, as JPEG bytes that fit.

        Server-side (docproof.canvas.render), on the DOCUMENT this turn has
        been editing — which is the whole point: the browser's snapshot was
        taken before the turn started, so a model that edited and then
        "looked" used to be shown the cover as it was BEFORE its own edit,
        and reported on a change it could not actually see.

        Flattened onto white because the render carries no paper (render.py's
        DIVERGENCES) and a cover judged on transparency is a cover judged
        wrong — which also means there is no alpha to keep, so JPEG costs
        nothing but weight (see LOOK_MAX_BYTES: a PNG here is what killed
        every turn that looked).

        Rendered ONCE at full width and resized down the ladder from that
        one render: re-rendering smaller would re-set the type and re-fit the
        plates, which is a different picture, and the grid would land on
        different pixels than the ones being measured."""
        from PIL import Image

        from . import render as canvas_render

        image = canvas_render.render(self.doc, self.job_dir, width=LOOK_WIDTH)
        paper = Image.new("RGBA", image.size, (255, 255, 255, 255))
        flat = Image.alpha_composite(paper, image).convert("RGB")
        if grid:
            flat = _draw_grid(flat, self.doc)

        smallest = b""
        for width, quality in LOOK_LADDER:
            frame = flat
            if width < flat.width:
                height = max(1, round(flat.height * width / flat.width))
                frame = flat.resize((width, height), Image.LANCZOS)
            buf = io.BytesIO()
            frame.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            smallest = data
            if len(data) <= LOOK_MAX_BYTES:
                return data
        # Every rung was still too heavy. The smallest is what there is; the
        # caller decides whether to send it, and _looked says so out loud
        # rather than letting the transport kill the turn.
        return smallest

    async def look(self, args: dict[str, Any]) -> dict[str, Any]:
        """The cover as it stands RIGHT NOW, as an image.

        Two sources, and which one is used is not a preference:

        - The client's snapshot, when the turn has changed nothing yet and no
          grid was asked for. It is the browser's own Konva render — the
          picture the person is actually looking at, fonts shaped by their
          browser — so it is the truest answer while it is still true.
        - A fresh server-side render, the moment either of those stops
          holding. After an edit the snapshot is stale by definition, and a
          measuring grid has to be drawn over pixels this process made.

        `grid` overlays the measuring grid (see _draw_grid): tenths of the
        canvas, labelled in the fractions the ops themselves take, so a
        position read off the picture needs no conversion to become a
        `set_frame`.

        A render that fails is not fatal to the turn: the snapshot is offered
        instead where there is one, with a sentence saying the picture is the
        one from before the edit."""
        grid = bool(args.get("grid"))
        stale = self._edited()
        if stale or grid:
            try:
                png = await asyncio.to_thread(self._render_look, grid)
            except Exception as e:                          # noqa: BLE001
                if not self.snapshot_png:
                    return _text(
                        f"The cover could not be rendered to look at ({e}) — "
                        f"work from `inspect` and say what you could not "
                        f"check.", is_error=True)
                return {"content": [
                    {"type": "text", "text": (
                        f"The live render failed ({e}), so this is the "
                        f"snapshot from before this turn's edits — do not "
                        f"judge those edits from it.")},
                    {"type": "image",
                     "data": base64.b64encode(self.snapshot_png).decode("ascii"),
                     "mimeType": _image_mime(self.snapshot_png)},
                ]}
            if len(png) > LOOK_MAX_BYTES:
                # Nothing on the ladder fit. Sending it anyway is not an
                # option: an oversized frame is not a truncated picture, it
                # is a CLIJSONDecodeError that ends the turn.
                return _text(
                    "This cover will not fit into a picture small enough to "
                    "send. Work from `inspect` and say what you could not "
                    "check.", is_error=True)
            note = ("The cover as it stands after this turn's edits"
                    if stale else "The cover as it stands")
            if grid:
                note += (", with the measuring grid over it: every line is a "
                         "tenth of the canvas and the labels ARE the "
                         "fractions ops take (x across, y down, both from the "
                         "top-left corner; a layer's frame.x/y is its CENTRE)")
            return {"content": [
                {"type": "text", "text": f"{note}."},
                {"type": "image",
                 "data": base64.b64encode(png).decode("ascii"),
                 "mimeType": _image_mime(png)},
            ]}
        if not self.snapshot_png:
            # Nothing edited and nothing sent: render anyway rather than
            # refuse. The document is on disk; being unable to see a cover
            # that this process can draw would be a self-inflicted blindness.
            try:
                png = await asyncio.to_thread(self._render_look, False)
            except Exception as e:                          # noqa: BLE001
                return _text(
                    f"No snapshot came with this message and the cover could "
                    f"not be rendered either ({e}) — work from `inspect` and "
                    f"say what you could not check.", is_error=True)
            if len(png) > LOOK_MAX_BYTES:
                return _text(
                    "This cover will not fit into a picture small enough to "
                    "send. Work from `inspect` and say what you could not "
                    "check.", is_error=True)
            return {"content": [
                {"type": "text", "text": "The cover as it stands."},
                {"type": "image",
                 "data": base64.b64encode(png).decode("ascii"),
                 "mimeType": _image_mime(png)},
            ]}
        return {"content": [{
            "type": "image",
            "data": base64.b64encode(self.snapshot_png).decode("ascii"),
            "mimeType": _image_mime(self.snapshot_png),
        }]}

    # -- registry --

    def specs(self) -> list[_ToolSpec]:
        """The tools this turn actually gets.

        Plan mode's read-only promise is kept HERE, by omission, rather than
        by a sentence in the system prompt that a determined model could talk
        itself past (§6: "read-only tools only")."""
        specs = [
            _ToolSpec(
                "inspect",
                "The canvas document as compact JSON: canvas size and every "
                "layer bottom to top with its id, kind, frame and salient "
                "fields. Cheap — re-call it after any edit.",
                {"type": "object", "properties": {}},
                self.inspect),
            _ToolSpec(
                "look",
                "See the cover as it stands, as an image — RE-RENDERED from "
                "the live document, so calling it after an edit shows the "
                "edit. Free, and there is no limit: look after every change "
                "you make. Pass grid=true to get a measuring grid over it — "
                "lines every tenth of the canvas, labelled in the same "
                "fractions the ops take (x across, y down, from the "
                "top-left; a layer's frame.x/y is its CENTRE) — which is how "
                "you turn \"the title sits too high\" into a number.",
                {"type": "object",
                 "properties": {
                     "grid": {
                         "type": "boolean",
                         "description": (
                             "Overlay the labelled measuring grid (and, on a "
                             "print wrap, the fold lines). Use it whenever "
                             "you are about to state or change a position."),
                     },
                 }},
                self.look),
        ]
        if self.mode != "act":
            return specs
        specs.append(_ToolSpec(
            "apply_ops",
            "Apply a batch of canvas ops to the document: set_frame (the box, "
            "and `corners` for the corner pin), nudge, set_text, set_layer, "
            "set_art (fit, and a swap back to a plate on this layer's "
            "history), set_scrim, set_frame_style, set_shape, set_effects, "
            "add_layer, remove_layer, reorder_layer. All-or-nothing: a "
            "refused op means none of the batch landed, and the refusal "
            "sentence says what to fix.",
            {"type": "object",
             "properties": {"ops": {
                 "type": "array",
                 "items": {"type": "object"},
                 "description": "Op dicts in the canvas op vocabulary, "
                                "applied in order."}},
             "required": ["ops"]},
            self.apply_ops))
        # Free, so it is registered on every act turn: rebalance makes no
        # vendor call, and gating a measurement behind an image key would
        # mean the cheapest way to answer "is this plate too dark" is the
        # only one a keyless session cannot reach.
        specs.append(_ToolSpec(
            "rebalance",
            "Measure one art layer's plate — mean luminance, contrast "
            "spread, mirror symmetry, visual centre of mass — and land a "
            "bounded levels correction as an ordinary undoable effect. Free: "
            "no image call. Returns exactly what it measured, so use it "
            "before asserting anything about a plate's values.",
            {"type": "object",
             "properties": {
                 "layer_id": {"type": "string",
                              "description": "The art layer to measure."}},
             "required": ["layer_id"]},
            self.rebalance))
        # The spending verbs need a real image client — or the $0 stand-in
        # lane, which is the whole point of that lane: on a machine with no
        # key at all the entire loop, this registry included, still runs.
        if self.image_client is None and not _fake_active():
            return specs
        specs.append(_ToolSpec(
            "reroll",
            "Regenerate an art layer's plate — same prompt by default, "
            "or a revised one. Costs real money and is never "
            "destructive: the old plate is kept in the layer's history. A "
            "re-roll is a NEW picture, so anything positioned against the "
            "old one has to be checked again.",
            {"type": "object",
             "properties": {
                 "layer_id": {"type": "string",
                              "description": "The art layer to reroll."},
                 "prompt": {"type": "string",
                            "description": "A revised prompt. Omit to "
                                           "re-roll the same one."}},
             "required": ["layer_id"]},
            self.reroll))
        specs.append(_ToolSpec(
            "finalize",
            "The top of the quality ladder: re-render this art layer's "
            "CURRENT plate at full quality with the composition anchored to "
            "it, so the layout the type was arranged against survives. The "
            "plate is the request — the layer needs no prompt of its own. "
            "Use it when the person says the plate is right and wants it "
            "crisp, never to ask for a different picture. Costs real money; "
            "the draft is kept in the layer's history.",
            {"type": "object",
             "properties": {
                 "layer_id": {"type": "string",
                              "description": "The art layer to re-render."},
                 "prompt": {"type": "string",
                            "description": "Emphasis appended to the "
                                           "fidelity instruction "
                                           "(\"especially the water\"), not "
                                           "a new description."}},
             "required": ["layer_id"]},
            self.finalize))
        specs.append(_ToolSpec(
            "ground_figure",
            "The cardinal rule's button: generate credible ground and a "
            "contact shadow under a standing figure by regenerating a band "
            "across the bottom of the plate. The server draws the mask, so "
            "no region has to be traced. Costs real money; the previous "
            "plate is kept in the layer's history.",
            {"type": "object",
             "properties": {
                 "layer_id": {"type": "string",
                              "description": "The art layer whose figure is "
                                             "floating."},
                 "instruction": {"type": "string",
                                 "description": "The scene's own specifics "
                                                "for the ground (\"wet "
                                                "cobblestones\"), appended "
                                                "to the recipe."}},
             "required": ["layer_id"]},
            self.ground_figure))
        return specs


def _text(body: str, *, is_error: bool = False) -> dict[str, Any]:
    """One text tool result in the SDK's handler shape."""
    return {"content": [{"type": "text", "text": body}], "is_error": is_error}


# The two magic numbers a canvas export can actually start with: PNG's
# signature and JPEG's SOI marker.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8", "image/jpeg"),
)


def _draw_grid(image: Any, doc: CanvasDoc) -> Any:
    """The measuring grid, drawn over a rendered cover.

    A vision model can see that a title is too high; it cannot see that the
    title's centre is at y=0.31 and should be at 0.24. This is the ruler that
    closes that gap — and it is drawn in the DOCUMENT's units, tenths of the
    canvas labelled 0.1 to 0.9, so a number read off the picture is already
    the number `set_frame` takes. No conversion step means no conversion
    mistake.

    Drawn on a TRANSLUCENT overlay, not straight onto the pixels: the cover
    still has to be judgeable through it — a ruler that hides the art defeats
    the purpose of looking. Four weights, faint to strong: tenths, then the
    thirds (drawn at 0.333/0.667, their own lines, because the doctrine talks
    in thirds and thirds are not tenths), then the centre cross, then — on a
    print wrap — the two folds in amber, because on a full sheet x=0.5 is the
    spine and almost never what anybody means by "the middle".

    Labels sit in the top margin and down the left edge, out of the way of a
    face, with a dark stroke so they hold on a white sky."""
    from PIL import Image, ImageDraw, ImageFont

    base = image.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = base.size
    try:
        font = ImageFont.load_default(13)
    except TypeError:                                       # Pillow < 9.2
        font = ImageFont.load_default()

    def rule(at: float, vertical: bool, width: int, ink: tuple[int, int, int],
             alpha: int) -> None:
        """One line across the whole frame, with a dark companion beside it
        so it reads on light art as well as dark."""
        pos = round(at * (w if vertical else h))
        for offset, colour in ((1, (0, 0, 0, alpha // 2)),
                               (0, ink + (alpha,))):
            a, b = ((pos + offset, 0), (pos + offset, h)) if vertical else \
                   ((0, pos + offset), (w, pos + offset))
            draw.line([a, b], fill=colour, width=width)

    def label(text: str, xy: tuple[int, int],
              ink: tuple[int, int, int] = (255, 255, 255)) -> None:
        draw.text(xy, text, font=font, fill=ink + (255,),
                  stroke_width=2, stroke_fill=(0, 0, 0, 255))

    white = (255, 255, 255)
    # Which edge is which, said on the picture as well as in the answer.
    label("x", (4, 3))
    label("y", (4, 20))
    for n in range(1, int(round(1 / GRID_STEP))):
        at = round(GRID_STEP * n, 2)
        rule(at, True, 1, white, 105)
        rule(at, False, 1, white, 105)
        label(f"{at:g}", (round(at * w) + 4, 3))
        label(f"{at:g}", (4, round(at * h) + 3))

    # Thirds are not tenths, so they get their own lines AND their own
    # labels — on the opposite edges, where they cannot be mistaken for the
    # tenth label they sit next to.
    for third in GRID_THIRDS:
        rule(third, True, 2, white, 165)
        rule(third, False, 2, white, 165)
        label(f"{third:.3f}", (round(third * w) + 4, h - 20))
        label(f"{third:.3f}", (w - 44, round(third * h) + 3))

    # The centre, which is what "dead centre" means in every op that takes a
    # frame — worth its own weight so it is never counted to.
    rule(0.5, True, 3, white, 225)
    rule(0.5, False, 3, white, 225)

    wrap = getattr(doc, "wrap", None)
    if wrap is not None:
        try:
            from .wrap import panels as _panels
            geometry = _panels(wrap)
        except Exception:                                   # noqa: BLE001
            geometry = None
        if geometry:
            # The two folds ARE the spine's edges, from the same geometry the
            # browser draws its guides from (docproof.canvas.wrap.panels), so
            # "move it onto the front panel" is a number and not a guess.
            amber = (255, 205, 80)
            for edge in ("x0", "x1"):
                at = (geometry.get("spine") or {}).get(edge)
                if at is None:
                    continue
                rule(float(at), True, 2, amber, 235)
                label(f"fold {float(at):.3f}",
                      (round(float(at) * w) + 4, h - 22), amber)
            for name in ("back", "spine", "front"):
                panel = geometry.get(name) or {}
                if "x0" in panel and "x1" in panel:
                    mid = (float(panel["x0"]) + float(panel["x1"])) / 2
                    label(name.upper(), (round(mid * w) - 16, h - 42), amber)
    return Image.alpha_composite(base, overlay).convert("RGB")


def _image_mime(data: bytes) -> str:
    """The snapshot's real content type, read off its first bytes.

    The declared type has to match the pixels. The client sends whatever its
    canvas exported — PNG today, and JPEG the moment the front end wants a
    snapshot roughly a tenth the size — and a hard-coded "image/png" on JPEG
    bytes is a lie told to a vision decoder, which is the kind of failure that
    comes back as "the model could not see the cover" rather than as an error.
    Sniffing costs two comparisons and means the front end can switch encodings
    without touching this file.

    Anything that matches neither is called PNG: the canvas has produced PNG
    for its whole life, and a byte string that is neither is a bug upstream —
    guessing a third format here would only make it harder to see."""
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    return "image/png"


def _summarize(doc: CanvasDoc) -> dict[str, Any]:
    """The document, small enough to re-read every turn.

    Not `model_dump`: a full dump carries plate histories, the frozen source
    spec, and the whole op history, which together dwarf the thing the model
    is trying to see. This keeps what an edit is aimed at — where each layer
    is, and the one or two fields that say which layer it is."""
    layers = []
    for index, layer in enumerate(doc.layers):
        entry: dict[str, Any] = {
            "index": index,
            "id": layer.id,
            "kind": layer.kind,
            "name": layer.name,
            "visible": layer.visible,
            "locked": layer.locked,
            "frame": _frame(layer.frame),
        }
        if layer.opacity != 1.0:
            entry["opacity"] = _round(layer.opacity)
        if layer.effects:
            entry["effects"] = [e.type for e in layer.effects]
        entry.update(_salient(layer))
        layers.append(entry)
    return {"canvas": {"w": doc.canvas.w, "h": doc.canvas.h}, "layers": layers}


def _frame(frame: Any) -> dict[str, Any]:
    """One layer's box, with the corner pin present only when it IS one.

    `corners` is None on every unpinned layer, which is nearly all of them,
    and a `"corners":null` on every frame of every inspect is a field the
    model reads on each refresh and learns nothing from. When the pin is
    real it stays, rounded like everything else — the four points are read
    the same way x/y are, and trailing float noise is noise either way."""
    out = {k: _round(v) for k, v in frame.model_dump().items()}
    if out.get("corners") is None:
        out.pop("corners", None)
    else:
        out["corners"] = [[_round(v) for v in point]
                          for point in out["corners"]]
    return out


def _salient(layer: Any) -> dict[str, Any]:
    """The fields that say which layer this is, per kind."""
    if layer.kind == "text":
        out = {"text": layer.text, "family": layer.family,
               "style": layer.style, "size": _round(layer.size),
               "color": layer.color, "align": layer.align}
        if layer.tracking:
            out["tracking"] = _round(layer.tracking)
        if layer.warp.kind != "none":
            out["warp"] = f"{layer.warp.kind} {_round(layer.warp.amount)}"
        return out
    if layer.kind == "art":
        out = {"source": layer.source, "fit": layer.fit,
               "transparent": layer.transparent}
        if layer.prompt:
            out["prompt"] = _clip(layer.prompt)
        if layer.plate_history:
            out["previous_plates"] = len(layer.plate_history)
        return out
    if layer.kind == "scrim":
        return {"color": layer.color,
                "gradient": {
                    "angle": _round(layer.gradient.angle),
                    "stops": [[_round(s.at), _round(s.alpha)]
                              for s in layer.gradient.stops]}}
    if layer.kind == "frame":
        return {"preset": layer.preset, "stroke": layer.stroke,
                "stroke_w": _round(layer.stroke_w),
                "inset": _round(layer.inset), "fill": layer.fill}
    if layer.kind == "shape":
        return {"shape": layer.shape, "fill": layer.fill,
                "stroke": layer.stroke, "stroke_w": _round(layer.stroke_w),
                "radius": _round(layer.radius)}
    return {}                                               # pragma: no cover


def _clip(value: str, limit: int = 120) -> str:
    """A prompt's opening, which is where its subject is."""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _round(value: Any) -> Any:
    """Four decimals is a tenth of a pixel on a 2K plate — past that the
    digits are noise the model has to read on every inspect."""
    return round(value, 4) if isinstance(value, float) else value


# -- the SDK seam -------------------------------------------------------------

def _sdk() -> Any:
    """The agent SDK, or the sentence that says how to get it.

    Imported through importlib at CALL time, never at module import: the
    canvas server must start, and its routes must import, on a machine that
    has no assistant installed — the AI box is the only thing that should
    fail there, and it should fail into the chat panel."""
    try:
        return importlib.import_module("claude_agent_sdk")
    except ImportError as e:
        raise AssistantUnavailable(_INSTALL_HINT) from e


def _require_login() -> None:
    """Refuse before spawning anything if this machine has no Claude login.

    Checked here rather than left to the CLI because the CLI's own failure
    for this is a subprocess exit nobody in the AI box can act on. The three
    places a login can live are the token env var (the Galley pattern), the
    credentials file, and the CLI's own config — any one of them is enough,
    and the check is deliberately generous: a false "you are logged in" costs
    one clear error from the CLI, a false "you are not" costs the feature."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    if (base / ".credentials.json").exists():
        return
    legacy = Path(config_dir) / ".claude.json" if config_dir \
        else Path.home() / ".claude.json"
    if legacy.exists():
        return
    raise AssistantUnavailable(_LOGIN_HINT)


def _child_env() -> dict[str, str]:
    """The billing guard: the spawned CLI must run on the subscription.

    ClaudeAgentOptions.env is MERGED over the parent's environment by the
    transport (it cannot delete a key), and the CLI reads ANTHROPIC_API_KEY
    with JavaScript truthiness — so blanking it to "" is how you actually make
    it absent, and the SDK's own auth probing treats the empty string as
    unset. This matters more than it looks: DocProof holds image-generation
    keys in the same process, and a key that leaks into this child turns a
    $0 subscription turn into a metered API bill without any visible symptom.

    CLAUDE_CODE_OAUTH_TOKEN is deliberately left alone — it is the credential
    this is supposed to run on."""
    return {"ANTHROPIC_API_KEY": ""}


def _sdk_tools(sdk: Any, specs: list[_ToolSpec]) -> list[Any]:
    """The specs, wrapped as SDK tool objects."""
    return [sdk.tool(s.name, s.description, s.schema)(s.handler) for s in specs]


def _options(sdk: Any, session: _Session, model: str) -> Any:
    """The agent's whole world for one turn.

    Everything here is a fence. `tools=[]` removes Claude Code's built-ins
    (no Bash, no Read, no web) so the only reachable capability is the canvas
    server; `strict_mcp_config` keeps this repo's own .mcp.json out;
    `setting_sources=[]` keeps its CLAUDE.md and settings out, so the art
    director is not handed a coding agent's instructions; and `cwd` is the job
    directory rather than the repo, so even a hypothetical file tool would be
    looking at the cover, not at the source."""
    specs = session.specs()
    server = sdk.create_sdk_mcp_server(
        name=SERVER_NAME, tools=_sdk_tools(sdk, specs))
    return sdk.ClaudeAgentOptions(
        model=model,
        system_prompt=f"{SYSTEM_PROMPT}\n\nYou are in "
                      f"{session.mode.upper()} MODE.",
        mcp_servers={SERVER_NAME: server},
        allowed_tools=[f"mcp__{SERVER_NAME}__{s.name}" for s in specs],
        tools=[],
        strict_mcp_config=True,
        setting_sources=[],
        # The tool surface is already closed to the functions above, so
        # there is nothing a permission prompt could protect — and a prompt
        # would block forever anyway: nobody is watching this subprocess's
        # stdin, the person is watching a chat panel in a browser.
        permission_mode="bypassPermissions",
        max_turns=MAX_TURNS,
        cwd=str(session.job_dir),
        env=_child_env(),
    )


# -- the prompt ---------------------------------------------------------------

def _prompt_text(messages: list[dict]) -> str:
    """The rolling transcript flattened into one prompt.

    The assistant is stateless between calls (see the module docstring), so
    prior turns arrive as CONTEXT and the last user message arrives as the
    ask. Marked off explicitly: an unlabelled transcript reads as instructions
    the model should still be carrying out, and it would redo yesterday's
    edits."""
    if not messages:
        raise ValueError("chat() needs at least one message, the user's")
    *earlier, latest = messages
    if latest.get("role") != "user":
        raise ValueError(
            f"the last message must be the user's, got {latest.get('role')!r}")
    ask = str(latest.get("content", "")).strip()
    if not earlier:
        return ask
    lines = [f"{m.get('role', 'user')}: {str(m.get('content', '')).strip()}"
             for m in earlier]
    return (
        "Earlier in this conversation, for context only — these turns are "
        "already done, do not repeat them:\n\n<transcript>\n"
        + "\n\n".join(lines)
        + "\n</transcript>\n\nThe person now says:\n\n" + ask)


def _user_message(text: str, snapshot_png: bytes | None) -> dict[str, Any]:
    """One streamed user message, in the shape the CLI's stdin expects.

    The snapshot rides ON the message rather than waiting behind the `look`
    tool: an art director who has to ask to see the cover will sometimes not
    ask, and then critiques a document description instead of a picture. `look`
    stays for the second look, after an edit.

    The media type is SNIFFED, never assumed — see `_image_mime`; both places a
    snapshot is attached read the same magic bytes, so the two never disagree
    about what the client sent."""
    content: Any = text
    if snapshot_png:
        content = [
            {"type": "text", "text": text},
            {"type": "image",
             "source": {"type": "base64",
                        "media_type": _image_mime(snapshot_png),
                        "data": base64.b64encode(snapshot_png).decode("ascii")}},
        ]
    return {"type": "user", "session_id": "",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None}


# -- the turn -----------------------------------------------------------------

def resolve_model(model: str | None = None) -> str:
    """Explicit argument, then the env override, then Opus 5."""
    if model:
        return model
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


async def chat(job_dir: Path, doc: CanvasDoc, messages: list[dict], mode: str,
               *, snapshot_png: bytes | None = None, model: str | None = None,
               image_client: Callable[[], Any] | None = None) -> ChatResult:
    """One turn of the AI box.

    Works on a deep copy of `doc` throughout and hands the copy back: the
    caller decides whether the turn is kept. A turn that raises leaves the
    on-disk document exactly as it was, which is the only reason it is safe
    to let a language model hold the pen at all.

    `mode` is "plan" (read-only) or "act" (full tool set). `image_client` is a
    zero-argument factory rather than a client because opening one costs a key
    lookup that a plan-mode turn should never pay."""
    if mode not in ("plan", "act"):
        raise ValueError(f"mode must be 'plan' or 'act', got {mode!r}")
    sdk = _sdk()
    _require_login()

    session = _Session(
        job_dir=Path(job_dir), doc=doc.model_copy(deep=True), mode=mode,
        snapshot_png=snapshot_png,
        image_client=image_client if mode == "act" else None)
    options = _options(sdk, session, resolve_model(model))
    message = _user_message(_prompt_text(messages), snapshot_png)

    async def prompt():
        yield message

    reply = ""
    last_text = ""
    cost = 0.0
    try:
        async for msg in sdk.query(prompt=prompt(), options=options):
            if isinstance(msg, sdk.AssistantMessage):
                # Subagent output would arrive with a parent id; there are no
                # subagents here, but the guard keeps the reply the model's
                # own words if that ever changes.
                if getattr(msg, "parent_tool_use_id", None):
                    continue
                spoken = "\n".join(
                    b.text for b in msg.content
                    if isinstance(b, sdk.TextBlock) and b.text.strip())
                if spoken.strip():
                    last_text = spoken.strip()
            elif isinstance(msg, sdk.ResultMessage):
                # Subscription runs report 0.0 (or nothing at all); whatever
                # it says is passed through rather than estimated, because the
                # canvas's money display must never invent a number.
                cost = float(msg.total_cost_usd or 0.0)
                if msg.result and msg.result.strip():
                    reply = msg.result.strip()
    except AssistantUnavailable:                            # pragma: no cover
        raise
    except sdk.CLINotFoundError as e:
        raise AssistantUnavailable(_CLI_HINT) from e
    except (sdk.ProcessError, sdk.ResultError) as e:
        # A CLI that exists but cannot hold a session — most commonly
        # "Not logged in · Please run /login" arriving as a ResultError
        # (the login pre-check passes because ~/.claude.json exists even
        # when the CLI is signed out). Same remedy as no CLI at all, so it
        # becomes the same readable 501 instead of escaping as a raw 500.
        raise AssistantUnavailable(
            f"The art director could not start a Claude session ({e}). "
            "Sign this machine in once with `claude setup-token` or "
            "`claude /login`, then try again.") from e

    return ChatResult(
        reply=reply or last_text or
        "The art director finished without saying anything — try asking again.",
        doc=session.doc,
        ops_applied=session.ops_applied,
        cost_usd=cost)


__all__ = [
    "AssistantUnavailable", "ChatResult", "chat", "resolve_model",
    "DEFAULT_MODEL", "LOOK_WIDTH", "MEASURE", "MODEL_ENV", "MAX_TURNS",
    "SERVER_NAME", "SYSTEM_PROMPT",
]
