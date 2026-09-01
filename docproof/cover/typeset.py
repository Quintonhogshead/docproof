"""Cover Studio's type-setter: turns one resolved TextSlot into pixels.

Three jobs, in order, matching docs/cover_designer_spec.md §7.3 points 2-6:

1. `fit_text` — apply case, then binary-search the largest font size in
   [size_min, size_max] whose best balanced line-breaking fits the zone.
   Never fails: a title that cannot fit even at size_min still gets a
   FitResult — via the no-overflow escalation (extra lines at the floor,
   then shrink below it; see fit_text's docstring — this supersedes §7.3's
   original "rendered anyway at the floor" sentence, 2026-08-30 addendum)
   plus a human-readable `.warning`, because a cover has to render for
   every brief and cropped glyphs are never acceptable.
2. `draw_text` — paint the chosen lines onto a Pillow layer: tracked runs are
   drawn glyph-by-glyph (the em/1000 tracking unit only makes sense applied
   between individual glyphs); untracked runs are drawn as one string so
   raqm, when present, can shape them. Stroke, shadow, and align/valign all
   live here.

What this module deliberately does NOT own: which color or shadow a slot
ends up drawn with. That is the legibility autopilot's call
(docproof.cover.compose), made by sampling the composite this text is about
to sit on — a decision that needs the current canvas, which this module
never sees. `draw_text` takes the autopilot's answer (`color`, `shadow`) as
plain arguments instead.
"""
from __future__ import annotations

import functools
import itertools
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

from .archetypes import zone_px
from .fonts import font_path
from .model import Shadow, TextSlot

log = logging.getLogger("docproof.cover.typeset")

# The manylinux/macOS Pillow wheels bundle libraqm (shaped text, real
# kerning); a from-source or minimal build may not have it. Covers still
# render without it — basic layout just kerns slightly worse on untracked
# runs — so this is a one-time warning, never a hard requirement (see
# docs/cover_designer_spec.md §7.3's opening paragraph).
try:
    import PIL.features
    RAQM_AVAILABLE = bool(PIL.features.check("raqm"))
except Exception:  # noqa: BLE001 - feature probing must never break import
    RAQM_AVAILABLE = False

if not RAQM_AVAILABLE:
    log.warning(
        "Pillow was built without raqm (libraqm) support; Cover Studio's "
        "text renders with Pillow's basic layout engine instead of shaped "
        "text — kerning on untracked runs will be slightly worse, but "
        "covers still render.")

# Multiplier from font size to the vertical stride between stacked lines —
# a touch looser than 1.0 so ascenders/descenders on adjacent lines never
# touch, applied uniformly to every display slot (spec §7.3 point 4).
LINE_HEIGHT = 1.08

# Binary-search steps for the fit search. 12 steps over a size range that is
# at most a few hundred px halves the gap ~4000x — far finer than a font's
# own hinting grid, so more steps would not change the rendered result.
FIT_ITERATIONS = 12

# Above this many words, the true balanced-break search (which brute-forces
# every word-boundary split for every line count) is skipped in favor of a
# plain greedy wrap. Titles are short; the combinatorial search is bounded by
# C(words-1, max_lines-1), which stays cheap for realistic word counts, but a
# hand-edited spec could pair a long title with a large max_lines and make
# that blow up. 12 keeps the worst case (max_lines up to 12) under a few
# thousand candidates while covering every realistic cover title.
MAX_BRUTE_WORDS = 12

# §15.12 justify_stack: the largest line may render at most this many times
# the smallest line's size — "cap the ratio, not the drama": "THE" alone on
# a line rendering huge is correct, 5x huge is a ransom note.
STACK_RATIO_CAP = 2.8

# §15.12 emphasis: how much bigger a `larger`-styled word renders than the
# rest of its line (baseline-aligned, same fitted layout).
EMPHASIS_LARGER_SCALE = 1.25

# The floor the below-size_min escalation (the no-cropped-glyphs rule; see
# fit_text) can shrink to. resolve_at already clamps the rasterized font to
# 1px, so searching below this would change nothing.
_ABSOLUTE_SIZE_FLOOR_PX = 1.0


@functools.lru_cache(maxsize=256)
def _font(file: str, size_px: float) -> ImageFont.FreeTypeFont:
    """One rasterizer handle per (file, size) — the fit searches load the
    same face at the same candidate sizes over and over (12 binary-search
    steps × every text slot × every replay), and FreeType setup is the
    expensive part. Cached handles are only ever read (measure/draw), never
    mutated, and rasterization is a pure function of (file, size), so the
    cache cannot change a single rendered byte."""
    return ImageFont.truetype(file, size_px)


def _load_font(file: Path | str, size_px: float) -> ImageFont.FreeTypeFont:
    return _font(str(file), max(1.0, size_px))


def measure(text: str, font: ImageFont.FreeTypeFont, tracking_px: float) -> float:
    """The rendered width of one line, in px: the font's own glyph advances
    plus `tracking_px` inserted at every inter-glyph gap (never at the ends —
    an N-character line has N-1 gaps). The same formula the fit search uses
    to test candidate breaks and `draw_text` uses to position tracked glyphs,
    so measurement and rendering can never disagree about a line's width."""
    if not text:
        return 0.0
    return font.getlength(text) + tracking_px * (len(text) - 1)


# -- emphasis (§15.12): word-granular style runs ------------------------------

@dataclass(frozen=True)
class _EmphasisPlan:
    """One slot's resolved emphasis, computed once per fit/draw: WHICH words
    (post-case whitespace-split indices), and what "emphasized" means as
    concrete rendering inputs — the face file the styled words draw in and
    the size multiplier. Color is NOT here: accent ink is the caller's to
    supply (draw_text's emphasis_color), the same door the autopilot's
    color already comes through, because typeset never sees a palette."""
    indices: frozenset[int]
    font_file: str        # face for the emphasized words
    scale: float          # 1.0 except emphasis_style="larger"
    accent: bool          # emphasis_style == "accent_color"


def _emphasis_plan(slot: TextSlot) -> _EmphasisPlan | None:
    """None when the slot has no emphasis — every legacy path branches on
    exactly that. The model validator already guaranteed the italic
    companion / swap face exists, so font_path here cannot raise for a slot
    that passed spec validation."""
    if not slot.emphasis:
        return None
    style = slot.emphasis_style
    if style == "italic":
        file = font_path(slot.font_family, "italic")
    elif style == "swap_face":
        file = font_path(slot.emphasis_font)
    else:                       # accent_color / larger keep the slot's face
        file = font_path(slot.font_family)
    return _EmphasisPlan(
        indices=frozenset(i for i in slot.emphasis if i >= 0),
        font_file=str(file),
        scale=EMPHASIS_LARGER_SCALE if style == "larger" else 1.0,
        accent=style == "accent_color")


def _styled_runs(line: str, start_word: int,
                 plan: _EmphasisPlan) -> tuple[tuple[str, bool], ...]:
    """The line as maximal same-style runs of (text, emphasized), covering
    every character including the single separator spaces _lines_from_splits
    joins words with. A separator carries the style of the word BEFORE it —
    one fixed, documented rule so measurement and drawing can never disagree
    about whose (possibly `larger`-scaled) space a gap is."""
    words = line.split(" ")
    runs: list[tuple[str, bool]] = []
    for i, word in enumerate(words):
        emphasized = (start_word + i) in plan.indices
        piece = word if i == len(words) - 1 else word + " "
        if runs and runs[-1][1] == emphasized:
            runs[-1] = (runs[-1][0] + piece, emphasized)
        else:
            runs.append((piece, emphasized))
    return tuple(runs)


def _measure_styled(line: str, start_word: int, plan: _EmphasisPlan,
                    base_font: ImageFont.FreeTypeFont,
                    emph_font: ImageFont.FreeTypeFont,
                    tracking_px: float) -> float:
    """measure()'s emphasis-aware twin: the width the styled render will
    actually occupy. Two regimes, matching the two drawing regimes exactly
    (same advances, same gap count — see _render_line's expressive branch):
    untracked lines draw run-by-run (raqm shapes within a run), so width is
    the sum of run getlengths; tracked lines draw glyph-by-glyph, so width
    is per-glyph advances plus the base-size tracking gap at every
    inter-glyph boundary (never the ends), the same N-1 rule measure()
    documents. The tracking unit stays the LINE's base size for every gap —
    a `larger` word widens its glyphs, not its gaps."""
    if not line:
        return 0.0
    runs = _styled_runs(line, start_word, plan)
    if not tracking_px:
        return sum((emph_font if emphasized else base_font).getlength(text)
                  for text, emphasized in runs)
    total = 0.0
    for text, emphasized in runs:
        font = emph_font if emphasized else base_font
        total += sum(font.getlength(ch) for ch in text)
    return total + tracking_px * (len(line) - 1)


def _line_word_starts(lines: tuple[str, ...]) -> tuple[int, ...]:
    """Each line's first word's GLOBAL index in the slot's post-case split —
    lines are contiguous word ranges by construction (_lines_from_splits),
    so this is a running count. Emphasis indices are global; per-line
    styling needs this offset."""
    starts: list[int] = []
    count = 0
    for line in lines:
        starts.append(count)
        count += len(line.split())
    return tuple(starts)


# -- balanced line breaking --------------------------------------------------

@dataclass(frozen=True)
class _Break:
    lines: tuple[str, ...]
    fits: bool              # every line measures within the zone width
    variance: float         # px^2 variance of line widths — lower is more balanced


def _variance(widths: list[float]) -> float:
    if len(widths) <= 1:
        return 0.0
    mean = sum(widths) / len(widths)
    return sum((w - mean) ** 2 for w in widths) / len(widths)


def _overflow(widths: list[float], zone_w_px: float) -> float:
    return max(0.0, max(widths) - zone_w_px) if widths else 0.0


def _partitions(n_words: int, n_lines: int):
    """Every way to cut `n_words` words into exactly `n_lines` contiguous,
    non-empty groups at word boundaries only (never hyphenate), as 0-based
    split points. n_lines - 1 splits chosen from the n_words - 1 gaps between
    words — brute force, per docs/cover_designer_spec.md §7.3 point 4."""
    if n_lines == 1:
        yield ()
        return
    if n_lines > n_words:
        return
    yield from itertools.combinations(range(1, n_words), n_lines - 1)


def _lines_from_splits(words: list[str], splits: tuple[int, ...]) -> tuple[str, ...]:
    bounds = (0, *splits, len(words))
    return tuple(" ".join(words[bounds[i]:bounds[i + 1]])
                for i in range(len(bounds) - 1))


def _forced_split(words: list[str], breaks: list[int]) -> tuple[int, ...]:
    """`slot.line_breaks` as a split tuple in the same 0-based shape
    _partitions yields, clipped to the words actually present. TextSlot's
    own validator has already refused a malformed list; the clip here is
    for the one case it cannot see — a spec hand-edited to a shorter title
    after the breaks were authored — where dropping the out-of-range tail
    beats raising inside the renderer."""
    return tuple(i for i in breaks if 0 < i < len(words))


def _forced_break(words: list[str], breaks: list[int], measure_fn,
                  zone_w_px: float) -> _Break:
    """The designed break as a _Break, bypassing every scoring rule. `fits`
    still reports honestly — a forced line that overflows the zone is the
    author's problem to SEE, and fit_text's own escalation ladder then
    handles it exactly as it handles a searched break that will not fit
    (shrink below the floor rather than crop a glyph). What the escalation
    must never do here is re-break the line: extra lines beyond max_lines
    are not offered when the breaks are designed, because inventing a line
    the author did not ask for silently discards the whole instruction."""
    splits = _forced_split(words, breaks)
    lines = _lines_from_splits(words, splits)
    starts = (0, *splits)
    widths = [measure_fn(line, start) for line, start in zip(lines, starts)]
    return _Break(lines=lines, fits=all(w <= zone_w_px for w in widths),
                  variance=_variance(widths))


def _best_break_brute(words: list[str], measure_fn, zone_w_px: float,
                      max_lines: int) -> _Break:
    """Try 1, then 2, ... lines; the first line count with any width-fitting
    split wins (fewest lines that permits the largest font), broken by
    lowest width variance among that count's fitting splits. If nothing ever
    fits, fall back to whichever candidate — at any line count — overflows
    the zone width the least (the "least-bad" breaking the below-floor
    escalation in fit_text shrinks until it genuinely fits).

    `measure_fn(line, start_word)` takes the line's first word's global
    index too — emphasis (§15.12) styles words by global position, so a
    line's width depends on WHICH words it holds, not just their text."""
    best_bad: _Break | None = None
    best_bad_overflow = float("inf")
    for n_lines in range(1, max_lines + 1):
        fitting: list[_Break] = []
        for splits in _partitions(len(words), n_lines):
            lines = _lines_from_splits(words, splits)
            starts = (0, *splits)
            widths = [measure_fn(line, start)
                     for line, start in zip(lines, starts)]
            fits = all(w <= zone_w_px for w in widths)
            variance = _variance(widths)
            cand = _Break(lines=lines, fits=fits, variance=variance)
            if fits:
                fitting.append(cand)
            else:
                overflow = _overflow(widths, zone_w_px)
                if (overflow < best_bad_overflow - 1e-9
                        or (abs(overflow - best_bad_overflow) <= 1e-9
                            and (best_bad is None or variance < best_bad.variance))):
                    best_bad, best_bad_overflow = cand, overflow
        if fitting:
            return min(fitting, key=lambda c: c.variance)
    return best_bad if best_bad is not None else _Break(
        lines=(" ".join(words),), fits=False, variance=0.0)


def _best_break_greedy(words: list[str], measure_fn, zone_w_px: float,
                       max_lines: int) -> _Break:
    """The >MAX_BRUTE_WORDS fallback: a plain greedy word-wrap (pack a line
    until the next word would overflow, then start a new one), with any
    overflow past `max_lines` folded onto the final line. Deterministic and
    linear in word count, unlike the brute-force search above. Same
    (line, start_word) measure contract as the brute search."""
    lines: list[str] = []
    current: list[str] = []
    start = 0
    for word in words:
        trial = current + [word]
        if current and measure_fn(" ".join(trial), start) > zone_w_px:
            lines.append(" ".join(current))
            start += len(current)
            current = [word]
        else:
            current = trial
    if current:
        lines.append(" ".join(current))
    if len(lines) > max_lines:
        head = lines[:max_lines - 1]
        tail = " ".join(lines[max_lines - 1:])
        lines = [*head, tail]
    starts = _line_word_starts(tuple(lines))
    widths = [measure_fn(line, start) for line, start in zip(lines, starts)]
    fits = len(lines) <= max_lines and all(w <= zone_w_px for w in widths)
    return _Break(lines=tuple(lines), fits=fits, variance=_variance(widths))


def _best_break(words: list[str], measure_fn, zone_w_px: float,
                max_lines: int) -> _Break:
    if not words:
        return _Break(lines=(), fits=True, variance=0.0)
    if len(words) <= MAX_BRUTE_WORDS:
        return _best_break_brute(words, measure_fn, zone_w_px, max_lines)
    return _best_break_greedy(words, measure_fn, zone_w_px, max_lines)


# -- fit search ---------------------------------------------------------------

@dataclass(frozen=True)
class FitResult:
    """One text slot's resolved typography: the chosen size, its line
    breaks, and the tracking (already converted to px at that size) needed
    to redraw those exact lines. `fits=False` means the slot's own stated
    constraints (size_min / max_lines) could not be honored — the text
    still renders, inside its zone, via the escalation fit_text documents —
    with a `warning` for RenderReport saying what was actually done.

    `line_sizes_px` is justify_stack's (§15.12) per-line answer: one size
    per line, each solved independently to fill the zone width. EMPTY —
    every uniform fit — means every line renders at `size_px`, the launch
    behavior, and every consumer of per-line geometry (draw_text,
    text_mask, line_boxes, line_ink_boxes) branches on exactly that
    emptiness. When non-empty, `size_px`/`size_frac` report the LARGEST
    line's size (the display size a human would name), and `tracking_px`
    matches it; per-line tracking re-derives from each line's own size
    (the em/1000 unit scales with the glyphs it spaces)."""
    size_px: float
    size_frac: float          # size_px / canvas height — RenderReport.fitted_sizes
    lines: tuple[str, ...]
    tracking_px: float
    fits: bool
    warning: str | None
    line_sizes_px: tuple[float, ...] = ()


def _apply_case(text: str, case: str) -> str:
    if case == "upper":
        return text.upper()
    if case == "title":
        return text.title()
    return text


def fit_text(slot: TextSlot, canvas_size: tuple[int, int], *,
            iters: int = FIT_ITERATIONS) -> FitResult:
    """Binary-search the largest font size in [size_min, size_max] (fractions
    of canvas height) whose best balanced line-breaking fits `slot.zone`, at
    `slot.font_family`/`slot.tracking`. `slot.content` is cased first
    (§7.3 point 2) — everything downstream (measuring, breaking, and later
    `draw_text`) works on that cased text, never the raw brief string.

    A size "fits" when its best breaking's widths are all within the zone AND
    the resulting block (line count × size × LINE_HEIGHT) fits the zone's
    height — both checked, since fewer/wider lines and more/narrower lines
    trade one constraint for the other.

    When size_min itself cannot satisfy those two constraints within
    max_lines, the fit escalates rather than ever letting ink overflow the
    zone (cropped glyphs are never acceptable — this deliberately
    supersedes §7.3's original "rendered anyway at the floor" behavior):
    first EXTRA LINES beyond max_lines at the floor size, as many as the
    zone's height holds; then, still too wide (an unbreakable long word) or
    too tall, SHRINK below size_min until the widest line fits the zone
    width and the block fits the zone height. Both escalations return
    fits=False plus a warning saying exactly which was taken.

    fit_mode="justify_stack" (§15.12) dispatches to its own per-line fit —
    see _fit_justify_stack — which is overflow-immune by construction.
    slot.arc does not enter the fit at all: the search measures the arc's
    chord (§15.12), which IS the straight-line width measured here."""
    canvas_w, canvas_h = canvas_size
    text = _apply_case(slot.content, slot.case)
    words = text.split()
    zone_left, zone_top, zone_w_px, zone_h_px = zone_px(slot.zone, canvas_size)

    if not words:
        # Nothing to place — e.g. an optional slot the caller chose not to
        # skip, or a required slot left blank. Report the ceiling size so a
        # caller inspecting `fitted_sizes` sees "no constraint hit", not a
        # misleadingly tiny number; draw_text no-ops on empty `lines` anyway.
        return FitResult(size_px=slot.size_max * canvas_h,
                         size_frac=slot.size_max, lines=(),
                         tracking_px=0.0, fits=True, warning=None)

    size_min_px = slot.size_min * canvas_h
    size_max_px = slot.size_max * canvas_h
    font_file = font_path(slot.font_family)
    plan = _emphasis_plan(slot)

    def measure_at(size_px: float, tracking_px: float):
        """The (line, start_word) measure closure at one candidate size —
        the legacy measure() verbatim when the slot has no emphasis, its
        style-aware twin when it does (styled words change advances, so
        they change which breaks fit — §15.12: "width measurement must
        account for the styled words")."""
        font = _load_font(font_file, size_px)
        if plan is None:
            return lambda line, start: measure(line, font, tracking_px)
        emph_font = _load_font(plan.font_file, size_px * plan.scale)
        return lambda line, start: _measure_styled(
            line, start, plan, font, emph_font, tracking_px)

    if slot.fit_mode == "justify_stack":
        return _fit_justify_stack(slot, canvas_size, words, measure_at,
                                  zone_w_px, zone_h_px,
                                  size_min_px, size_max_px)

    forced = _forced_split(words, slot.line_breaks)

    def resolve_at(size_px: float, max_lines: int) -> tuple[_Break, float]:
        tracking_px = slot.tracking / 1000.0 * size_px
        if forced:
            # Designed breaks: the same partition at every candidate size,
            # so the binary search below solves purely for SIZE. max_lines
            # is deliberately ignored — the author's line count IS the
            # answer, and TextSlot's validator already refused a list that
            # asks for more lines than the slot allows.
            br = _forced_break(words, list(forced),
                               measure_at(size_px, tracking_px), zone_w_px)
        else:
            br = _best_break(words, measure_at(size_px, tracking_px),
                             zone_w_px, max_lines)
        return br, tracking_px

    def fits_zone(br: _Break, size_px: float, max_lines: int) -> bool:
        if not br.fits or len(br.lines) > max_lines:
            return False
        block_h = len(br.lines) * size_px * LINE_HEIGHT
        return block_h <= zone_h_px + 1e-6

    lo, hi = size_min_px, size_max_px
    best: tuple[float, _Break, float] | None = None
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        br, tracking_px = resolve_at(mid, slot.max_lines)
        if fits_zone(br, mid, slot.max_lines):
            best = (mid, br, tracking_px)
            lo = mid
        else:
            hi = mid

    if best is not None:
        size_px, br, tracking_px = best
        return FitResult(size_px=size_px, size_frac=size_px / canvas_h,
                         lines=br.lines, tracking_px=tracking_px,
                         fits=True, warning=None)

    # -- floor escalation: ink must never overflow the zone -------------------
    # Step 1: keep the floor size, allow as many lines as the zone's height
    # holds. Only worth trying when that is genuinely more than max_lines.
    height_allows = int(zone_h_px // (size_min_px * LINE_HEIGHT))
    if height_allows > slot.max_lines:
        br, tracking_px = resolve_at(size_min_px, height_allows)
        if fits_zone(br, size_min_px, height_allows):
            warning = (
                f"{slot.id}: even size_min ({slot.size_min:.3f}×canvas "
                f"height) does not fit its zone within {slot.max_lines} "
                f"line(s); exceeded max_lines ({len(br.lines)} lines at "
                f"the floor size) so no ink overflows the zone.")
            return FitResult(size_px=size_min_px,
                             size_frac=size_min_px / canvas_h,
                             lines=br.lines, tracking_px=tracking_px,
                             fits=False, warning=warning)

    # Step 2: shrink below size_min until the widest line fits the zone
    # width and the block fits the zone height. At each candidate size the
    # line budget is whatever the zone's height holds at that size (never
    # fewer than max_lines), so width and height are solved together. The
    # search floor is the 1px rasterization clamp — below it nothing
    # changes — and at 1px any realistic zone fits, so `found` is None only
    # for a zone too narrow for a single 1px glyph; that degenerate still
    # renders the least-bad floor break rather than nothing, matching the
    # never-fail contract, with the honest warning.
    def resolve_relaxed(size_px: float):
        allowed = max(slot.max_lines,
                     int(zone_h_px // (size_px * LINE_HEIGHT)), 1)
        br, tracking_px = resolve_at(size_px, allowed)
        if fits_zone(br, size_px, allowed):
            return br, tracking_px
        return None

    found: tuple[float, _Break, float] | None = None
    at_floor = resolve_relaxed(_ABSOLUTE_SIZE_FLOOR_PX)
    if at_floor is not None:
        br, tracking_px = at_floor
        found = (_ABSOLUTE_SIZE_FLOOR_PX, br, tracking_px)
        lo, hi = _ABSOLUTE_SIZE_FLOOR_PX, size_min_px
        for _ in range(iters + 6):     # +6: the range spans px, not tens of px
            mid = (lo + hi) / 2.0
            resolved = resolve_relaxed(mid)
            if resolved is not None:
                found = (mid, *resolved)
                lo = mid
            else:
                hi = mid

    if found is not None:
        size_px, br, tracking_px = found
        warning = (
            f"{slot.id}: even size_min ({slot.size_min:.3f}×canvas height) "
            f"does not fit its zone within {slot.max_lines} line(s); "
            f"shrunk below size_min to {size_px / canvas_h:.4f}×canvas "
            f"height ({len(br.lines)} line(s)) so no ink overflows the "
            f"zone.")
        return FitResult(size_px=size_px, size_frac=size_px / canvas_h,
                         lines=br.lines, tracking_px=tracking_px,
                         fits=False, warning=warning)

    br, tracking_px = resolve_at(size_min_px, slot.max_lines)
    warning = (f"{slot.id}: its zone is too small to fit even one 1px "
              f"glyph; rendered the least-bad line breaks at the floor "
              f"size — expect clipped ink.")
    return FitResult(size_px=size_min_px, size_frac=size_min_px / canvas_h,
                     lines=br.lines, tracking_px=tracking_px,
                     fits=False, warning=warning)


# A cheap-but-honest reference size for justify_stack's candidate scoring:
# glyph advances are near-linear in point size (hinting wobbles them ~1%),
# so one measurement here predicts every line's fill size well enough to
# RANK candidate breaks; only the winner gets the exact per-line solve.
_STACK_REF_SIZE_PX = 100.0


def _fit_justify_stack(slot: TextSlot, canvas_size: tuple[int, int],
                       words: list[str], measure_at, zone_w_px: float,
                       zone_h_px: float, size_min_px: float,
                       size_max_px: float) -> FitResult:
    """§15.12's poster stack: every line sized INDEPENDENTLY so its tracked
    width fills the zone width exactly, clamped to size_max above (a short
    line stops growing and underfills, aligned per slot.align) and ratio-
    capped at STACK_RATIO_CAP× the smallest line. Candidate line-breaks are
    scored by minimal wasted VERTICAL space — the stack that fills most of
    the zone's height wins, not the most even widths (uniform fit's variance
    scoring would defeat the whole point: "THE" alone on a line rendering
    huge is correct here).

    Overflow-immune by construction, on both axes: a line whose exact fill
    size would fall below size_min keeps that below-floor size rather than
    clamping up into a width overflow (warned, fits=False — the same
    no-cropped-glyphs doctrine as fit_text's uniform escalation), and a
    stack taller than the zone shrinks ALL lines proportionally — the ratio
    structure survives — to exactly the zone height, warned only when the
    shrink lands under the floor.

    The candidate sweep prices every break at a linear ESTIMATE from one
    reference measurement (deterministic, and accurate to about a percent);
    the chosen break alone gets the exact binary solve per line, so the
    shipped widths land within a pixel of the zone width regardless of any
    estimate wobble."""
    canvas_w, canvas_h = canvas_size

    def width_at(line: str, start: int, size_px: float) -> float:
        tracking_px = slot.tracking / 1000.0 * size_px
        return measure_at(size_px, tracking_px)(line, start)

    def fill_estimate(line: str, start: int) -> float:
        w_ref = width_at(line, start, _STACK_REF_SIZE_PX)
        if w_ref <= 0:
            return size_max_px
        return min(max(_STACK_REF_SIZE_PX * zone_w_px / w_ref,
                       _ABSOLUTE_SIZE_FLOOR_PX), size_max_px)

    def fill_exact(line: str, start: int) -> float:
        """Largest size in [1px, size_max] whose width stays within the
        zone — width is monotone in size, so a plain binary search; 24
        steps leave the answer within a fraction of a pixel of exactly
        filling. Returns the bound itself when even that bound underfills
        (size_max: a short line at cap) or overflows (1px: a zone too
        narrow for one glyph — width then still overflows, and the caller's
        warning owns that degenerate honestly)."""
        if width_at(line, start, size_max_px) <= zone_w_px:
            return size_max_px
        lo, hi = _ABSOLUTE_SIZE_FLOOR_PX, size_max_px
        if width_at(line, start, lo) > zone_w_px:
            return lo
        for _ in range(24):
            mid = (lo + hi) / 2.0
            if width_at(line, start, mid) <= zone_w_px:
                lo = mid
            else:
                hi = mid
        return lo

    def ratio_capped(sizes: list[float]) -> list[float]:
        cap = STACK_RATIO_CAP * min(sizes)
        return [min(s, cap) for s in sizes]

    # -- candidate sweep ------------------------------------------------------
    candidates: list[tuple[tuple[str, ...], tuple[int, ...]]] = []
    forced = _forced_split(words, slot.line_breaks)
    if forced:
        # Designed breaks: exactly one candidate, so the sweep's
        # least-wasted-height scoring never runs. This is the whole reason
        # the field exists — that scorer is what turns a four-line poster
        # stack into three lines whenever three happens to fill the zone
        # more completely, which is most of the time.
        candidates.append((_lines_from_splits(words, forced), (0, *forced)))
    elif len(words) <= MAX_BRUTE_WORDS:
        for n_lines in range(1, slot.max_lines + 1):
            for splits in _partitions(len(words), n_lines):
                lines = _lines_from_splits(words, splits)
                candidates.append((lines, (0, *splits)))
    else:
        # Long titles skip the combinatorial sweep exactly like the uniform
        # fit does: one greedy wrap at the floor size supplies the single
        # candidate, and the per-line solve below does the rest.
        tracking_px = slot.tracking / 1000.0 * size_min_px
        br = _best_break_greedy(words, measure_at(size_min_px, tracking_px),
                                zone_w_px, slot.max_lines)
        candidates.append((br.lines, _line_word_starts(br.lines)))

    best_fit: tuple[float, int, tuple[str, ...], tuple[int, ...]] | None = None
    best_tall: tuple[float, int, tuple[str, ...], tuple[int, ...]] | None = None
    for lines, starts in candidates:
        est = ratio_capped([fill_estimate(line, start)
                           for line, start in zip(lines, starts)])
        block_h = sum(s * LINE_HEIGHT for s in est)
        waste = zone_h_px - block_h
        if waste >= 0:
            key = (waste, len(lines))
            if best_fit is None or key < (best_fit[0], best_fit[1]):
                best_fit = (waste, len(lines), lines, starts)
        else:
            key = (-waste, len(lines))
            if best_tall is None or key < (best_tall[0], best_tall[1]):
                best_tall = (-waste, len(lines), lines, starts)

    chosen = best_fit if best_fit is not None else best_tall
    assert chosen is not None   # words is non-empty — fit_text guarded
    _, _, lines, starts = chosen

    # -- exact solve for the winner -------------------------------------------
    sizes = ratio_capped([fill_exact(line, start)
                         for line, start in zip(lines, starts)])
    block_h = sum(s * LINE_HEIGHT for s in sizes)
    if block_h > zone_h_px + 1e-6:
        scale = zone_h_px / block_h
        sizes = [s * scale for s in sizes]

    warning = None
    fits = True
    smallest = min(sizes)
    if smallest < size_min_px - 1e-6:
        fits = False
        warning = (
            f"{slot.id}: justify_stack sized line(s) below size_min "
            f"({slot.size_min:.3f}×canvas height) — smallest line is "
            f"{smallest / canvas_h:.3f}×canvas height — so every line "
            f"fits the zone.")

    size_px = max(sizes)
    return FitResult(size_px=size_px, size_frac=size_px / canvas_h,
                     lines=tuple(lines),
                     tracking_px=slot.tracking / 1000.0 * size_px,
                     fits=fits, warning=warning,
                     line_sizes_px=tuple(sizes))


# -- rendering ------------------------------------------------------------

def _render_line(line: str, font: ImageFont.FreeTypeFont, tracking_px: float,
                 color: tuple[int, int, int], zone_left: float, zone_w: float,
                 row_center_y: float, align: str, stroke_w_px: int,
                 stroke_rgb: tuple[int, int, int] | None,
                 canvas_size: tuple[int, int]) -> Image.Image:
    """One line, drawn onto its own canvas-sized transparent layer at its
    final (x, y) — vertically anchored to its row's mid-line via Pillow's
    "m" anchor rather than hand-rolled ascent/descent math, so untracked and
    tracked lines land on the identical baseline grid."""
    layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    if not line:
        return layer
    draw = ImageDraw.Draw(layer)
    fill = (*color, 255)
    stroke_fill = (*stroke_rgb, 255) if stroke_rgb else None
    if tracking_px:
        # Tracking only means something applied glyph-by-glyph — draw each
        # character with `font.getlength` advances plus the tracking gap,
        # starting from whichever x makes the WHOLE tracked run land at the
        # requested alignment (measure() gives that total width).
        total_w = measure(line, font, tracking_px)
        if align == "left":
            x = zone_left
        elif align == "right":
            x = zone_left + zone_w - total_w
        else:
            x = zone_left + (zone_w - total_w) / 2.0
        for i, ch in enumerate(line):
            draw.text((x, row_center_y), ch, font=font, fill=fill, anchor="lm",
                      stroke_width=stroke_w_px, stroke_fill=stroke_fill)
            x += font.getlength(ch)
            if i < len(line) - 1:
                x += tracking_px
    else:
        # No tracking: one draw call for the whole line, so raqm (when
        # present) shapes it — real kerning, ligatures, the works.
        anchor_h = {"left": "l", "center": "m", "right": "r"}[align]
        if align == "left":
            x = zone_left
        elif align == "right":
            x = zone_left + zone_w
        else:
            x = zone_left + zone_w / 2.0
        draw.text((x, row_center_y), line, font=font, fill=fill,
                 anchor=f"{anchor_h}m", stroke_width=stroke_w_px,
                 stroke_fill=stroke_fill)
    return layer


# -- expressive rendering (§15.12) --------------------------------------------
#
# The four type moves change WHERE and HOW glyph ink is laid, never what the
# downstream machinery consumes: draw_text/text_mask/line_ink_boxes below
# each branch to this path the moment a slot carries any move (or a
# justify_stack fit), and the legacy paths above stay byte-identical for
# everything else. Everything ink-based downstream — occlusion, contact,
# autopilot sampling, balance snap — reads the resulting alpha and needs no
# knowledge that a move happened.

@dataclass(frozen=True)
class _LineStyle:
    """Everything the expressive renderer needs to draw ONE line: the
    fonts (base + emphasis companion when the slot has one), the line's
    text as (text, emphasized) runs, and the per-line numbers. Colors are
    RGB triples — the caller already resolved hexes — and the emphasis
    color equals the base color unless the slot's style is accent_color
    AND the caller supplied an accent (typeset never sees a palette)."""
    font: ImageFont.FreeTypeFont
    emph_font: ImageFont.FreeTypeFont | None
    runs: tuple[tuple[str, bool], ...]
    tracking_px: float
    arc_sag_px: float          # signed bow at this line's chord: + arch
    color: tuple[int, int, int]
    emph_color: tuple[int, int, int]
    stroke_w_px: int
    stroke_rgb: tuple[int, int, int] | None


def _line_style(slot: TextSlot, line: str, start_word: int, size_px: float,
                tracking_px: float, plan: _EmphasisPlan | None,
                color_rgb: tuple[int, int, int],
                emph_rgb: tuple[int, int, int], sag_px: float,
                stroke_w_px: int,
                stroke_rgb: tuple[int, int, int] | None) -> _LineStyle:
    base_font = _load_font(font_path(slot.font_family), size_px)
    if plan is not None:
        emph_font = _load_font(plan.font_file, size_px * plan.scale)
        runs = _styled_runs(line, start_word, plan)
    else:
        emph_font = None
        runs = ((line, False),)
    return _LineStyle(font=base_font, emph_font=emph_font, runs=runs,
                      tracking_px=tracking_px, arc_sag_px=sag_px,
                      color=color_rgb, emph_color=emph_rgb,
                      stroke_w_px=stroke_w_px, stroke_rgb=stroke_rgb)


def _style_width(style: _LineStyle) -> float:
    """The width the styled line will actually be DRAWN at, per the same
    two regimes _measure_styled documents (run sums untracked, per-glyph
    advances + base tracking gaps otherwise) — alignment must center the
    real ink, so this is computed from the same fonts the renderer holds,
    never re-measured elsewhere."""
    def pick(emphasized: bool) -> ImageFont.FreeTypeFont:
        return (style.emph_font if emphasized and style.emph_font is not None
                else style.font)

    if not style.tracking_px and style.arc_sag_px == 0.0:
        return sum(pick(e).getlength(t) for t, e in style.runs)
    total = sum(pick(e).getlength(ch) for t, e in style.runs for ch in t)
    n = sum(len(t) for t, _ in style.runs)
    return total + style.tracking_px * (n - 1) if n else 0.0


def _composite_clipped(base: Image.Image, tile: Image.Image,
                       dest: tuple[int, int]) -> None:
    """alpha_composite that tolerates a dest partly (or wholly) outside
    `base` — Image.alpha_composite itself demands non-negative in-bounds
    coordinates, and both rotated slot layers and arc glyph tiles routinely
    poke past an edge. Crops the tile to the overlap and composites in
    place; a fully off-canvas tile is a no-op."""
    x, y = dest
    left, top = max(0, -x), max(0, -y)
    right = min(tile.width, base.width - x)
    bottom = min(tile.height, base.height - y)
    if right <= left or bottom <= top:
        return
    if (left, top, right, bottom) != (0, 0, tile.width, tile.height):
        tile = tile.crop((left, top, right, bottom))
    base.alpha_composite(tile, dest=(x + left, y + top))


def _arc_radius(chord_w: float, sag_px: float) -> float | None:
    """The circle through a chord's endpoints whose apex sits |sag| off the
    chord midpoint: R = (c²/4 + s²) / 2s — None when the bow is under half
    a pixel (draw straight) or the chord is degenerate."""
    if abs(sag_px) < 0.5 or chord_w <= 1.0:
        return None
    return (chord_w * chord_w / 4.0 + sag_px * sag_px) / (2.0 * abs(sag_px))


def _arc_at(dx: float, radius: float, sag_px: float) -> tuple[float, float]:
    """Baseline displacement and glyph rotation at signed chord offset
    `dx` (0 = chord midpoint). Derivation, y-down canvas coordinates: the
    circle's center sits R−|s| BELOW the chord for an arch (above for a
    valley), so a point at horizontal offset dx lies sqrt(R²−dx²) toward
    the text from the center — dy = −sign(s)·(sqrt(R²−dx²) − (R−|s|)),
    which is −s at the midpoint and exactly 0 at both endpoints (the
    endpoint identity R² − c²/4 = (R−|s|)² makes the ends land back on the
    straight baseline). The glyph rotates to the local tangent: the radial
    through the point makes asin(dx/R) with vertical, so the tangent makes
    the same angle with horizontal — positive (counter-clockwise, PIL's
    convention) on the left half of an arch, mirrored for a valley."""
    dxc = max(-radius, min(radius, dx))
    h = math.sqrt(max(radius * radius - dxc * dxc, 0.0))
    sign = 1.0 if sag_px > 0 else -1.0
    dy = -sign * (h - (radius - abs(sag_px)))
    rot = -sign * math.degrees(math.asin(dxc / radius))
    return dy, rot


def _render_line_expressive(style: _LineStyle, zone_left: float,
                            zone_w: float, row_center_y: float, align: str,
                            canvas_size: tuple[int, int]) -> Image.Image:
    """The expressive twin of _render_line: one line onto its own
    canvas-sized transparent layer, supporting word-granular emphasis runs
    and the arced baseline. Glyphs sit on the line's BASELINE (anchor "s")
    rather than the legacy mid-line anchor: mixed sizes (`larger`) must
    share a baseline, and the arc's bow displaces the baseline itself —
    the baseline y derives from the base font's own ascent/descent about
    the same row mid-line the legacy path anchors to, so straight
    expressive text lands on the legacy grid to within font-metric
    rounding.

    Three regimes, in order of preference: untracked flat emphasis draws
    run-by-run (raqm still shapes within each run); tracked or mixed-font
    flat text draws glyph-by-glyph exactly like the legacy tracked path
    (advance + gap); an arced line draws every glyph as its own tile,
    rotated to the local tangent about its baseline midpoint, then
    composited at the bowed position — forced glyph-by-glyph even at
    tracking 0, per §15.12."""
    layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    line_text = "".join(t for t, _ in style.runs)
    if not line_text:
        return layer
    draw = ImageDraw.Draw(layer)
    stroke_fill = (*style.stroke_rgb, 255) if style.stroke_rgb else None

    def pick(emphasized: bool) -> tuple[ImageFont.FreeTypeFont,
                                        tuple[int, int, int]]:
        if emphasized:
            font = (style.emph_font if style.emph_font is not None
                    else style.font)
            return font, style.emph_color
        return style.font, style.color

    total_w = _style_width(style)
    if align == "left":
        x = zone_left
    elif align == "right":
        x = zone_left + zone_w - total_w
    else:
        x = zone_left + (zone_w - total_w) / 2.0

    # Baseline from the base font's metrics: the legacy "m" anchor centers
    # the ascent..descent band on row_center_y, so that band's baseline
    # sits (ascent − descent)/2 below the row's mid-line.
    ascent, descent = style.font.getmetrics()
    y_base = row_center_y + (ascent - descent) / 2.0

    if not style.tracking_px and style.arc_sag_px == 0.0:
        for text, emphasized in style.runs:
            font, rgb = pick(emphasized)
            draw.text((x, y_base), text, font=font, fill=(*rgb, 255),
                      anchor="ls", stroke_width=style.stroke_w_px,
                      stroke_fill=stroke_fill)
            x += font.getlength(text)
        return layer

    radius = _arc_radius(total_w, style.arc_sag_px)
    chord_mid = x + total_w / 2.0
    n_chars = len(line_text)
    idx = 0
    for text, emphasized in style.runs:
        font, rgb = pick(emphasized)
        for ch in text:
            adv = font.getlength(ch)
            if radius is None:
                draw.text((x, y_base), ch, font=font, fill=(*rgb, 255),
                          anchor="ls", stroke_width=style.stroke_w_px,
                          stroke_fill=stroke_fill)
            else:
                glyph_cx = x + adv / 2.0
                dy, rot = _arc_at(glyph_cx - chord_mid, radius,
                                  style.arc_sag_px)
                # A square tile comfortably larger than any glyph extent
                # from its baseline midpoint (advance/2 sideways, ~ascent
                # up), rotated about its exact center so the anchor point
                # survives the rotation unmoved.
                half = int(math.ceil(1.8 * font.size + style.stroke_w_px + 2))
                tile = Image.new("RGBA", (2 * half, 2 * half), (*rgb, 0))
                ImageDraw.Draw(tile).text(
                    (half, half), ch, font=font, fill=(*rgb, 255),
                    anchor="ms", stroke_width=style.stroke_w_px,
                    stroke_fill=stroke_fill)
                if abs(rot) > 1e-3:
                    tile = tile.rotate(rot,
                                       resample=Image.Resampling.BICUBIC)
                _composite_clipped(
                    layer, tile,
                    (round(glyph_cx) - half, round(y_base + dy) - half))
            x += adv
            idx += 1
            if idx < n_chars:
                x += style.tracking_px
    return layer


def _rotate_layer(layer: Image.Image, angle_deg: float) -> Image.Image:
    """One straight-alpha RGBA layer rotated by `angle_deg` (PIL's
    convention: positive = counter-clockwise), expanded so nothing clips.
    The premultiplied round-trip matters: transparent pixels carry
    arbitrary RGB, and bicubic-resampling straight RGBA would smear that
    RGB into every antialiased glyph edge as a dark fringe. The alpha band
    itself resamples identically either way, which is what keeps
    draw_text's rotated ink and text_mask's rotated mask pixel-aligned."""
    return (layer.convert("RGBa")
            .rotate(angle_deg, resample=Image.Resampling.BICUBIC,
                    expand=True)
            .convert("RGBA"))


def _rotated_anchor(alpha_bbox: tuple[int, int, int, int], slot: TextSlot,
                    canvas_size: tuple[int, int]) -> tuple[int, int]:
    """Where the rotated slot layer's frame origin lands on the canvas:
    place the rotated GLYPH ink's bbox per align/valign inside the zone —
    the re-anchor §15.12 asks for, since rotation-with-expand grew the
    frame and the flat layout's position means nothing now — then clamp
    the bbox fully inside the canvas (top/left win when the rotated extent
    is larger than the canvas allows). Both draw_text and text_mask call
    this with the same glyph-alpha bbox, so ink and mask land together."""
    zone_left, zone_top, zone_w_px, zone_h_px = zone_px(slot.zone, canvas_size)
    left, top, right, bottom = alpha_bbox
    bw, bh = right - left, bottom - top
    if slot.align == "left":
        tx = float(zone_left)
    elif slot.align == "right":
        tx = zone_left + zone_w_px - bw
    else:
        tx = zone_left + (zone_w_px - bw) / 2.0
    if slot.valign == "top":
        ty = float(zone_top)
    elif slot.valign == "bottom":
        ty = zone_top + zone_h_px - bh
    else:
        ty = zone_top + (zone_h_px - bh) / 2.0
    cw, ch = canvas_size
    tx = min(max(tx, 0.0), float(max(cw - bw, 0)))
    ty = min(max(ty, 0.0), float(max(ch - bh, 0)))
    return round(tx) - left, round(ty) - top


def _expressive(slot: TextSlot, fit: FitResult) -> bool:
    """Whether this slot leaves the legacy render paths at all — the ONE
    gate every consumer below shares. False (every pre-wave spec: no
    emphasis, arc 0, rotate 0, uniform fit) keeps draw_text/text_mask/
    line_ink_boxes on their launch code byte-for-byte."""
    return bool(fit.line_sizes_px or slot.emphasis
                or slot.arc != 0.0 or slot.rotate != 0.0)


def _expressive_line_layers(slot: TextSlot, fit: FitResult,
                            canvas_size: tuple[int, int],
                            color_rgb: tuple[int, int, int],
                            emph_rgb: tuple[int, int, int],
                            stroke_w_px: int,
                            stroke_rgb: tuple[int, int, int] | None
                            ) -> list[Image.Image]:
    """Every fitted line rendered flat (pre-rotation) onto its own
    canvas-sized layer via the expressive renderer — the shared builder
    draw_text, text_mask, and line_ink_boxes all go through, so the three
    can never disagree about where expressive ink lies. The arc's sagitta
    is arc × ZONE height (§15.12), the same bow on every line's own row
    (parallel baselines)."""
    zone_left, _, zone_w_px, zone_h_px = zone_px(slot.zone, canvas_size)
    plan = _emphasis_plan(slot)
    sag_px = slot.arc * zone_h_px
    metrics = _line_metrics(slot, fit)
    spans = _row_spans(slot, fit, canvas_size)
    starts = _line_word_starts(fit.lines)
    layers: list[Image.Image] = []
    for i, line in enumerate(fit.lines):
        if not line:
            layers.append(Image.new("RGBA", canvas_size, (0, 0, 0, 0)))
            continue
        size_px, tracking_px = metrics[i]
        row_top, row_h = spans[i]
        style = _line_style(slot, line, starts[i], size_px, tracking_px,
                            plan, color_rgb, emph_rgb, sag_px,
                            stroke_w_px, stroke_rgb)
        layers.append(_render_line_expressive(
            style, zone_left, zone_w_px, row_top + row_h / 2.0,
            slot.align, canvas_size))
    return layers


def _block_height(fit: FitResult) -> float:
    """The fitted block's nominal height: every line's own size × the
    LINE_HEIGHT stride, summed. The uniform branch keeps the launch
    expression exactly (size × LINE_HEIGHT × count, in that operation
    order) so pre-wave geometry stays float-identical; a justify_stack fit
    sums its per-line strides instead."""
    if fit.line_sizes_px:
        return sum(s * LINE_HEIGHT for s in fit.line_sizes_px)
    return fit.size_px * LINE_HEIGHT * len(fit.lines)


def _line_metrics(slot: TextSlot, fit: FitResult
                  ) -> tuple[tuple[float, float], ...]:
    """(size_px, tracking_px) per fitted line. Uniform fits repeat the
    FitResult's single answer; justify_stack lines each carry their own
    size, and tracking re-derives per line because the em/1000 unit scales
    with the glyphs it spaces."""
    if fit.line_sizes_px:
        return tuple((s, slot.tracking / 1000.0 * s)
                    for s in fit.line_sizes_px)
    return tuple((fit.size_px, fit.tracking_px) for _ in fit.lines)


def _row_spans(slot: TextSlot, fit: FitResult, canvas_size: tuple[int, int]
              ) -> tuple[tuple[float, float], ...]:
    """Each fitted line's nominal (top, height) row on the stacked grid —
    the expressive-path twin of the inline `top + line_h * i` math the
    legacy paths keep verbatim. Rows are contiguous; a justify_stack fit's
    rows are each as tall as their own line's stride."""
    if not fit.lines:
        return ()
    top = _block_top(slot, fit, canvas_size)
    if fit.line_sizes_px:
        out: list[tuple[float, float]] = []
        y = top
        for s in fit.line_sizes_px:
            h = s * LINE_HEIGHT
            out.append((y, h))
            y += h
        return tuple(out)
    line_h = fit.size_px * LINE_HEIGHT
    return tuple((top + line_h * i, line_h) for i in range(len(fit.lines)))


def _block_top(slot: TextSlot, fit: FitResult, canvas_size: tuple[int, int]
              ) -> float:
    """Where the fitted block's own top edge lands, in canvas px — the one
    piece of geometry draw_text, text_mask, and line_boxes all need
    identically (same valign branch, same zone/line-height math), factored
    out once so the three can never quietly drift apart. `fit.lines` must
    be non-empty; callers already guard the empty case themselves (an
    empty block has no top to speak of)."""
    _, zone_top, _, zone_h_px = zone_px(slot.zone, canvas_size)
    block_h = _block_height(fit)
    if slot.valign == "top":
        return float(zone_top)
    if slot.valign == "bottom":
        return zone_top + zone_h_px - block_h
    return zone_top + (zone_h_px - block_h) / 2.0


def line_boxes(slot: TextSlot, fit: FitResult, canvas_size: tuple[int, int]
              ) -> tuple[tuple[float, float], ...]:
    """Every fitted line's NOMINAL (top, bottom) span in canvas px — the
    line-height grid draw_text/text_mask actually lay glyphs out on, sharing
    their exact block-positioning math via _block_top so a caller reasoning
    about line position always agrees with where ink was actually drawn.
    Adjacent boxes are contiguous by construction (line i's bottom is line
    i+1's top) — this is the STRIDE grid, not real ink extent; a caller
    that wants the actual whitespace between two lines' own glyphs (as
    opposed to the nominal leading between their line-height rows) wants
    line_ink_boxes instead. Empty when `fit.lines` is empty (nothing fit —
    e.g. an empty optional slot)."""
    if not fit.lines:
        return ()
    if fit.line_sizes_px:
        return tuple((top, top + h)
                    for top, h in _row_spans(slot, fit, canvas_size))
    line_h = fit.size_px * LINE_HEIGHT
    top = _block_top(slot, fit, canvas_size)
    return tuple((top + line_h * i, top + line_h * (i + 1))
                for i in range(len(fit.lines)))


def line_ink_boxes(slot: TextSlot, fit: FitResult, canvas_size: tuple[int, int]
                   ) -> tuple[tuple[int, int, int, int] | None, ...]:
    """Every fitted line's OWN glyph ink bbox (left, top, right, bottom) in
    canvas px — rendered exactly like text_mask's own per-line loop, but
    kept separate rather than unioned into one mask, so a caller (the
    line-gap snap, v2.2 wave deliverable 3) can measure the REAL
    whitespace between two consecutive lines' actual ink — which varies
    line to line with whether either has ascenders/descenders — rather than
    the nominal, uniform line-height stride line_boxes describes. One entry
    per fitted line, in order; None for any line whose own render has no
    ink at all (a blank line, which _best_break shouldn't produce but
    draw_text/text_mask both defensively skip too)."""
    canvas_w, canvas_h = canvas_size
    if not fit.lines:
        return ()
    zone_left, _, zone_w_px, _ = zone_px(slot.zone, canvas_size)

    stroke_w_px = 0
    if slot.stroke is not None and slot.stroke.width > 0:
        stroke_w_px = max(1, round(slot.stroke.width * canvas_h))
    white = (255, 255, 255)

    if _expressive(slot, fit):
        # Same shared layer builder as draw_text/text_mask; under rotate,
        # every line layer takes the identical whole-slot transform (same
        # angle, same union-derived anchor), so a per-line box here is
        # exactly that line's ink inside the final rotated placement.
        layers = _expressive_line_layers(
            slot, fit, canvas_size, white, white, stroke_w_px,
            white if stroke_w_px else None)
        if slot.rotate == 0.0:
            return tuple(layer.getchannel("A").getbbox() for layer in layers)
        union = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        for layer in layers:
            union.alpha_composite(layer)
        rotated_union = _rotate_layer(union, slot.rotate)
        union_bbox = rotated_union.getchannel("A").getbbox()
        if union_bbox is None:
            return tuple(None for _ in fit.lines)
        dx, dy = _rotated_anchor(union_bbox, slot, canvas_size)
        boxes: list[tuple[int, int, int, int] | None] = []
        for layer in layers:
            rotated = _rotate_layer(layer, slot.rotate)
            bbox = rotated.getchannel("A").getbbox()
            if bbox is None:
                boxes.append(None)
                continue
            left, top_, right, bottom = bbox
            boxes.append((max(0, left + dx), max(0, top_ + dy),
                          min(canvas_w, right + dx),
                          min(canvas_h, bottom + dy)))
        return tuple(boxes)

    font = ImageFont.truetype(font_path(slot.font_family), max(1.0, fit.size_px))
    line_h = fit.size_px * LINE_HEIGHT
    top = _block_top(slot, fit, canvas_size)

    boxes: list[tuple[int, int, int, int] | None] = []
    for i, line in enumerate(fit.lines):
        if not line:
            boxes.append(None)
            continue
        row_center_y = top + line_h * (i + 0.5)
        layer = _render_line(line, font, fit.tracking_px, white, zone_left,
                             zone_w_px, row_center_y, slot.align, stroke_w_px,
                             white if stroke_w_px else None, canvas_size)
        boxes.append(layer.getchannel("A").getbbox())
    return tuple(boxes)


def _shadowed(shadow_layer: Image.Image, shadow: Shadow,
              canvas_h: int) -> tuple[Image.Image, int, int]:
    """One line's shadow treatment — blur, alpha scale, pixel offset — the
    exact ops the legacy draw_text body applies inline, shared with the
    expressive path so the two can never diverge on what a Shadow means."""
    blur_px = max(0.0, shadow.blur * canvas_h)
    if blur_px > 0:
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur_px))
    r, g, b, a = shadow_layer.split()
    a = a.point(lambda v: round(v * shadow.alpha))
    return (Image.merge("RGBA", (r, g, b, a)),
            round(shadow.dx * canvas_h), round(shadow.dy * canvas_h))


def draw_text(base: Image.Image, slot: TextSlot, fit: FitResult, color: str,
             shadow: Shadow | None, canvas_size: tuple[int, int], *,
             emphasis_color: str | None = None) -> Image.Image:
    """Draw one resolved text slot onto `base`, returning a new composited
    image (`base` is not mutated). `color` and `shadow` are the legibility
    autopilot's final answer (docproof.cover.compose) — they may differ from
    `slot.color_role`'s literal palette color / `slot.shadow`, which is why
    they are passed in rather than read off `slot` directly.
    `emphasis_color` is the same kind of argument for §15.12's accent_color
    emphasis: the palette's accent hex, resolved by the caller (compose),
    because typeset never sees a palette; None falls back to `color`, so
    accent emphasis quietly no-ops for a caller that has no palette.

    Each line gets its own transparent layer, including its own shadow pass
    (blur, offset, composited under the glyphs) — per §7.3 point 6, so a
    large blur radius on one line's shadow never bleeds into a neighboring
    line the way blurring one shared multi-line layer would.

    A slot carrying any §15.12 move (or a justify_stack fit) takes the
    expressive path below instead; slots without stay on this legacy body
    byte-for-byte."""
    if not fit.lines:
        return base
    canvas_w, canvas_h = canvas_size
    zone_left, zone_top, zone_w_px, zone_h_px = zone_px(slot.zone, canvas_size)

    stroke_w_px = 0
    stroke_rgb = None
    if slot.stroke is not None and slot.stroke.width > 0:
        stroke_w_px = max(1, round(slot.stroke.width * canvas_h))
        stroke_rgb = ImageColor.getrgb(slot.stroke.color)

    color_rgb = ImageColor.getrgb(color)

    if _expressive(slot, fit):
        emph_rgb = color_rgb
        plan = _emphasis_plan(slot)
        if plan is not None and plan.accent and emphasis_color:
            emph_rgb = ImageColor.getrgb(emphasis_color)
        line_layers = _expressive_line_layers(
            slot, fit, canvas_size, color_rgb, emph_rgb,
            stroke_w_px, stroke_rgb)
        shadow_layers: list[Image.Image] | None = None
        if shadow is not None:
            shadow_rgb = ImageColor.getrgb(shadow.color)
            shadow_layers = _expressive_line_layers(
                slot, fit, canvas_size, shadow_rgb, shadow_rgb, 0, None)

        if slot.rotate == 0.0:
            # Flat expressive: the legacy per-line composite discipline
            # (each line's shadow blurred alone, then its ink), just with
            # the expressive layers standing in for _render_line's.
            out = base
            for i, line in enumerate(fit.lines):
                if not line:
                    continue
                if shadow is not None and shadow_layers is not None:
                    shadow_layer, dx_px, dy_px = _shadowed(
                        shadow_layers[i], shadow, canvas_h)
                    out = out.copy()
                    _composite_clipped(out, shadow_layer, (dx_px, dy_px))
                out = out.copy()
                out.alpha_composite(line_layers[i])
            return out

        # Rotate (§15.12): render the slot FLAT — every line's shadow and
        # ink composited into one slot layer — then rotate that finished
        # layer with expand and re-anchor its glyph ink per align/valign
        # in the zone. The anchor comes from the GLYPH union's rotated
        # bbox (shadow excluded), the same bbox text_mask derives, so ink
        # and mask stay pixel-aligned; the shadow rides along, its offset
        # rotating with the layer, exactly what "render flat, rotate the
        # finished text layer" means.
        flat_full = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        flat_glyphs = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        for i, line in enumerate(fit.lines):
            if not line:
                continue
            if shadow is not None and shadow_layers is not None:
                shadow_layer, dx_px, dy_px = _shadowed(
                    shadow_layers[i], shadow, canvas_h)
                _composite_clipped(flat_full, shadow_layer, (dx_px, dy_px))
            flat_full.alpha_composite(line_layers[i])
            flat_glyphs.alpha_composite(line_layers[i])
        rotated_full = _rotate_layer(flat_full, slot.rotate)
        rotated_glyphs = _rotate_layer(flat_glyphs, slot.rotate)
        bbox = rotated_glyphs.getchannel("A").getbbox()
        if bbox is None:
            return base
        dest = _rotated_anchor(bbox, slot, canvas_size)
        out = base.copy()
        _composite_clipped(out, rotated_full, dest)
        return out

    font = ImageFont.truetype(font_path(slot.font_family), max(1.0, fit.size_px))
    line_h = fit.size_px * LINE_HEIGHT
    block_top = _block_top(slot, fit, canvas_size)

    out = base
    for i, line in enumerate(fit.lines):
        if not line:
            continue
        row_center_y = block_top + line_h * (i + 0.5)
        if shadow is not None:
            shadow_rgb = ImageColor.getrgb(shadow.color)
            shadow_layer = _render_line(line, font, fit.tracking_px, shadow_rgb,
                                        zone_left, zone_w_px, row_center_y,
                                        slot.align, 0, None, canvas_size)
            shadow_layer, dx_px, dy_px = _shadowed(shadow_layer, shadow,
                                                   canvas_h)
            out = out.copy()
            out.alpha_composite(shadow_layer, dest=(dx_px, dy_px))
        text_layer = _render_line(line, font, fit.tracking_px, color_rgb,
                                  zone_left, zone_w_px, row_center_y,
                                  slot.align, stroke_w_px, stroke_rgb, canvas_size)
        out = out.copy()
        out.alpha_composite(text_layer)
    return out


def text_mask(slot: TextSlot, fit: FitResult, canvas_size: tuple[int, int]) -> Image.Image:
    """The fitted text's glyph coverage alone, as a canvas-sized 'L' mask —
    white where ink would land, black everywhere else. Shares draw_text's
    exact block-positioning and per-line math (same valign branch, same
    `_render_line` calls) so a mask always lines up pixel-for-pixel with
    where a normal `fill` render would have put its ink.

    This is the effects rack's (§7.4a) seam into typeset: knockout/art_fill
    text modes are not "draw colored glyphs", they are "punch glyphs out of
    a panel" / "window glyphs onto the ground beneath" — both of which need
    the glyph SHAPE as a mask, never an ink color, which is why this returns
    an 'L' image rather than taking a color and drawing RGBA like draw_text
    does. docproof.cover.compose owns what to DO with the mask (that is
    still a legibility-autopilot decision, same as draw_text's color/shadow
    are), never this module."""
    canvas_w, canvas_h = canvas_size
    if not fit.lines:
        return Image.new("L", canvas_size, 0)
    zone_left, zone_top, zone_w_px, zone_h_px = zone_px(slot.zone, canvas_size)

    stroke_w_px = 0
    if slot.stroke is not None and slot.stroke.width > 0:
        stroke_w_px = max(1, round(slot.stroke.width * canvas_h))
    white = (255, 255, 255)

    if _expressive(slot, fit):
        # The same flat layers draw_text's expressive path builds (white
        # ink — alpha coverage is color-independent), the same rotation,
        # and the same glyph-bbox re-anchor, so this mask is pixel-aligned
        # with the drawn ink under every move — which is exactly what
        # keeps every ink-measuring guard honest about moved type.
        layers = _expressive_line_layers(
            slot, fit, canvas_size, white, white, stroke_w_px,
            white if stroke_w_px else None)
        out = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        for layer in layers:
            out.alpha_composite(layer)
        if slot.rotate == 0.0:
            return out.getchannel("A")
        rotated = _rotate_layer(out, slot.rotate)
        bbox = rotated.getchannel("A").getbbox()
        if bbox is None:
            return Image.new("L", canvas_size, 0)
        dest = _rotated_anchor(bbox, slot, canvas_size)
        anchored = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        _composite_clipped(anchored, rotated, dest)
        return anchored.getchannel("A")

    font = ImageFont.truetype(font_path(slot.font_family), max(1.0, fit.size_px))
    line_h = fit.size_px * LINE_HEIGHT
    block_top = _block_top(slot, fit, canvas_size)

    out = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    for i, line in enumerate(fit.lines):
        if not line:
            continue
        row_center_y = block_top + line_h * (i + 0.5)
        layer = _render_line(line, font, fit.tracking_px, white, zone_left,
                             zone_w_px, row_center_y, slot.align, stroke_w_px,
                             white if stroke_w_px else None, canvas_size)
        out.alpha_composite(layer)
    return out.getchannel("A")


__all__ = ["EMPHASIS_LARGER_SCALE", "FIT_ITERATIONS", "LINE_HEIGHT",
          "MAX_BRUTE_WORDS", "RAQM_AVAILABLE", "STACK_RATIO_CAP",
          "FitResult", "draw_text", "fit_text", "line_boxes",
          "line_ink_boxes", "measure", "text_mask"]
