"""Manuscript prep, end to end.

Same three steps as the review pipeline — prepare, run, finish — so the CLI and
the app drive prep the way they already drive a review. What differs is the
middle: one ordered classification pass instead of many independent ones, and a
finish that writes documents only after proving they still say what the author
wrote.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..models import Usage
from ..providers import Provider
from ..utils.tokens import estimate_tokens
from ..utils.xml_helpers import DocxPackage
from . import ingest as prep_ingest
from .book_design import BookDesign, BookMeta, load_book_design
from .chunker import Window
from .model import PrepPlan, Structure, Tag
from .notes import write_notes
from .rules import build_plan
from .styles import StyleSheet, load_style_sheet
from .tagger import (Tagger, TaggingPrompt, load_tagging_prompt, render_window)
from .verify import Verification, VerificationFailed, verify_output
from .writers.book import write_book
from .writers.clean import WriteStats, write_clean
from .writers.tracked import write_tracked

log = logging.getLogger("docproof.prep.pipeline")

OUTPUT_KINDS = ("book", "indesign", "tracked")
_PREFIXES = {"book": "book_", "indesign": "tagged_", "tracked": "tracked_"}
# One label per paragraph, and the paragraph id is most of it.
OUTPUT_TOKENS_PER_PARAGRAPH = 25


@dataclass
class PreparedPrep:
    pkg: DocxPackage
    structure: Structure
    sheet: StyleSheet
    prompt: TaggingPrompt
    windows: list[Window]
    design: BookDesign | None = None
    system_tokens: int = 0

    @property
    def request_count(self) -> int:
        return len(self.windows)

    @property
    def paragraph_count(self) -> int:
        return len(self.structure.paragraphs)

    @property
    def est_document_tokens(self) -> int:
        """An upper bound: every window's text, plus the instructions once per
        request. In practice the instructions are read from cache after the
        first window, so the real bill is lower."""
        return sum(
            estimate_tokens(render_window(w, {},
                                          header=self.prompt.context_header,
                                          preview_chars=400))
            for w in self.windows) + self.system_tokens * self.request_count

    @property
    def est_output_tokens(self) -> int:
        return self.paragraph_count * OUTPUT_TOKENS_PER_PARAGRAPH


@dataclass(frozen=True)
class PrepOutputs:
    documents: dict[str, Path]           # kind → written file
    notes_md: Path
    notes_json: Path
    plan: PrepPlan
    verifications: tuple[Verification, ...]
    flags: int
    tagged: int
    words: int


def resolve(config_dir: str | Path, value: str) -> Path:
    """A path from the config, read relative to the config file that named it."""
    path = Path(value)
    return path if path.is_absolute() else Path(config_dir) / path


def prepare(cfg: Config, input_path: str | Path, *, config_dir: str | Path,
            override_dir: str | Path | None = None) -> PreparedPrep:
    """Open the manuscript, read every paragraph, and load the house style set.

    Raises IngestError on a document prep refuses — a legacy .doc, an InDesign
    layout, or one that still has tracked changes in it."""
    pkg = prep_ingest.preflight(input_path)
    structure = prep_ingest.build_structure(pkg)
    sheet = load_style_sheet(resolve(config_dir, cfg.prep.style_sheet),
                             override_dir=override_dir)
    prompt = load_tagging_prompt(resolve(config_dir, cfg.prep.tagging_prompt),
                                 override_dir=override_dir)
    design = load_book_design(resolve(config_dir, cfg.prep.book_design),
                              override_dir=override_dir)
    tagger = _tagger(cfg, sheet, prompt, provider=None)
    prepared = PreparedPrep(
        pkg=pkg, structure=structure, sheet=sheet, prompt=prompt,
        windows=tagger.plan_windows(structure), design=design,
        system_tokens=estimate_tokens(tagger.system_prompt))
    log.info("Prep ready: %d paragraphs, %d window(s), style sheet '%s'.",
             prepared.paragraph_count, prepared.request_count, sheet.name)
    return prepared


def _tagger(cfg: Config, sheet: StyleSheet, prompt: TaggingPrompt,
            provider: Provider | None) -> Tagger:
    return Tagger(sheet, prompt, provider, model=cfg.api.model,
                  max_paragraphs=cfg.prep.max_paragraphs_per_window,
                  token_budget=cfg.prep.token_budget,
                  context=cfg.prep.context_paragraphs,
                  preview_chars=cfg.prep.preview_chars,
                  max_output_tokens=cfg.api.max_output_tokens)


def run(cfg: Config, prepared: PreparedPrep, provider: Provider, *,
        progress=None, checkpoint=None) -> tuple[list[Tag], Usage]:
    """Label the manuscript. Windows run in order: what a paragraph is depends
    on what came before it."""
    usage = Usage()
    tagger = _tagger(cfg, prepared.sheet, prepared.prompt, provider)
    tags = tagger.tag(prepared.structure, usage, progress=progress,
                      checkpoint=checkpoint)
    return tags, usage


def detect_meta(cfg: Config, prepared: PreparedPrep, provider: Provider, *,
                usage: Usage) -> BookMeta:
    """The one extra model call the book output wants: subject, title, author
    from the opening pages. Callers merge overrides on top with merge_meta."""
    from .meta import detect_meta as _detect

    return _detect(prepared.structure, prepared.design, provider,
                   model=cfg.api.model, usage=usage,
                   source_name=Path(prepared.structure.source_path).name)


def merge_meta(detected: BookMeta, *, subject: str = "", title: str = "",
               author: str = "") -> BookMeta:
    from .meta import merge_meta as _merge

    return _merge(detected, subject=subject, title=title, author=author)


def run_mock(prepared: PreparedPrep, canned: dict[str, str] | None = None
             ) -> tuple[list[Tag], Usage]:
    """Everything downstream of the model, without the model."""
    from .tagger import MockTagger

    usage = Usage()
    tags = MockTagger(prepared.sheet, canned).tag(prepared.structure, usage)
    return tags, usage


def default_meta(source_path: str | Path) -> BookMeta:
    """What the book output knows with no detector and no overrides: a title
    guessed from the file name, and nothing else."""
    stem = Path(source_path).stem or "Manuscript"
    return BookMeta(title=" ".join(stem.replace("_", " ").split()))


def finish(prepared: PreparedPrep, tags: list[Tag], usage: Usage, cfg: Config,
           *, out_dir: str | Path, source_path: str | Path,
           outputs: tuple[str, ...] | list[str] = ("book",),
           meta: BookMeta | None = None) -> PrepOutputs:
    """Turn labels into documents, then prove the documents still say what the
    manuscript said. A file that fails is deleted, not shipped."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    kinds = [k for k in OUTPUT_KINDS if k in tuple(outputs)]
    if not kinds:
        raise ValueError(
            f"Nothing to write: outputs must include at least one of "
            f"{', '.join(OUTPUT_KINDS)}.")

    book_meta: BookMeta | None = None
    if "book" in kinds:
        if prepared.design is None:
            raise ValueError("The book output needs the interior design; "
                             "prepare() loads it from cfg.prep.book_design.")
        book_meta = meta or BookMeta()
        if not book_meta.title:
            book_meta = dataclasses.replace(
                book_meta, title=default_meta(source_path).title)

    plan = build_plan(prepared.structure, tags, prepared.sheet)
    stem = Path(source_path).stem or "document"
    written: dict[str, Path] = {}
    stats: dict[str, WriteStats] = {}
    checks: list[Verification] = []
    failures: list[str] = []

    for kind in kinds:
        path = out / f"{_prefix(kind)}{stem}.docx"
        # A fresh package per file: the writers mutate in place, and the
        # tracked file has to start from the manuscript, not from the clean
        # one.
        pkg = DocxPackage(Path(prepared.structure.source_path))
        if kind == "book":
            stats[kind] = write_book(
                pkg, prepared.structure, plan, prepared.sheet,
                prepared.design, book_meta,
                strip_formatting=cfg.prep.strip_direct_formatting)
            views = ("clean",)
        elif kind == "indesign":
            stats[kind] = write_clean(
                pkg, prepared.structure, plan, prepared.sheet,
                strip_formatting=cfg.prep.strip_direct_formatting)
            views = ("clean",)
        else:
            stats[kind] = write_tracked(pkg, prepared.structure, plan,
                                        prepared.sheet,
                                        author=cfg.revision_author)
            # Accepting every change must give the clean text; rejecting every
            # change must give the manuscript back.
            views = ("accept", "reject")
        pkg.save(path)

        if not cfg.prep.verify:
            written[kind] = path
            continue
        try:
            checks.extend(verify_output(prepared.structure, path, plan.glyph,
                                        views=views))
            written[kind] = path
        except VerificationFailed as e:
            path.unlink(missing_ok=True)
            failures.append(str(e))

    notes_md, notes_json = write_notes(
        out, structure=prepared.structure, plan=plan, sheet=prepared.sheet,
        stats=stats, checks=checks, usage=usage, cfg=cfg, written=written,
        failures=failures, source_path=source_path,
        prompt=prepared.prompt, tags=tags, meta=book_meta)

    if failures:
        raise VerificationFailed(" ".join(failures))

    return PrepOutputs(documents=written, notes_md=notes_md,
                       notes_json=notes_json, plan=plan,
                       verifications=tuple(checks), flags=len(plan.flags),
                       tagged=len([p for p in plan.plans if p.style]),
                       words=prepared.structure.word_count)


def _prefix(kind: str) -> str:
    return _PREFIXES[kind]
