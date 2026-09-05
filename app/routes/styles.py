"""The house style guide: reading it, replacing it, adjusting it."""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Literal

import yaml
from fastapi import Depends, FastAPI, HTTPException, UploadFile
from pydantic import BaseModel, Field

from docproof import prep as preplib
from docproof.config import load_config
from docproof.prep.styles import StyleSheetError

from . import common
from ..settings import CONFIG_PATH, Paths

# A style sheet is a page of YAML. Anything this size is the wrong file.
MAX_SHEET_BYTES = 1_000_000
# A house template is an exported IDML — a few MB with fonts and spreads. Well
# past that is the wrong file (a packaged .indd, an image).
MAX_TEMPLATE_BYTES = 50_000_000


class StyleFormatUpdate(BaseModel):
    """How one house style looks in the tagged .docx. Points throughout, the
    same units the style sheet is written in. Bounds are here so a slip in the
    UI cannot write a 400-point chapter heading into somebody's style set."""
    size: float | None = Field(default=None, ge=4, le=96)
    bold: bool | None = None
    italic: bool | None = None
    align: Literal["left", "center", "right"] | None = None
    space_before: float | None = Field(default=None, ge=0, le=200)
    space_after: float | None = Field(default=None, ge=0, le=200)
    page_break_before: bool | None = None
    keep_next: bool | None = None
    indent: float | None = Field(default=None, ge=-100, le=200)
    # Format keys to drop entirely, which is not the same as setting them to
    # zero: an unset size inherits the template's.
    clear: list[str] = []


class SheetFormatUpdate(BaseModel):
    styles: dict[str, StyleFormatUpdate] = {}
    trim: str | None = Field(default=None, max_length=40)
    scene_break_glyph: str | None = Field(default=None, max_length=20)


def _shipped_sheet() -> Path:
    cfg = load_config(CONFIG_PATH)
    return preplib.pipeline.resolve(CONFIG_PATH.parent, cfg.prep.style_sheet)


def _override_path(paths: Paths) -> Path:
    """Where a replacement style set lives. The name is the one the config
    asks for, because that is what `load_style_sheet` looks for."""
    return paths.prep / _shipped_sheet().name


def _style_payload(paths: Paths) -> dict:
    """The style set in force, as the Settings screen reads it."""
    shipped = _shipped_sheet()
    override = paths.prep / shipped.name
    try:
        sheet = preplib.load_style_sheet(shipped, override_dir=paths.prep)
    except StyleSheetError as e:
        return {"ok": False, "error": str(e), "override_path": str(override),
                "shipped_path": str(shipped), "using_override": override.is_file()}
    payload = {
        "ok": True,
        "name": sheet.name, "version": sheet.version, "trim": sheet.trim,
        "glyph": sheet.scene_break_glyph,
        "path": sheet.path,
        "shipped_path": str(shipped),
        "override_path": str(override),
        "using_override": Path(sheet.path).parent == paths.prep,
        "styles": [{"name": s.name, "id": s.id, "describe": s.describe,
                    "assign": s.assign, "opens": s.opens,
                    "format": dict(s.format)} for s in sheet.styles],
        "character_styles": [{"name": s.name, "id": s.id}
                             for s in sheet.character_styles],
    }
    # The book output's subject choices, for the panel's override dropdown. A
    # broken design file must not take the styles screen down with it — the
    # styles above are a different file.
    try:
        cfg = load_config(CONFIG_PATH)
        design = preplib.load_book_design(
            preplib.pipeline.resolve(CONFIG_PATH.parent, cfg.prep.book_design),
            override_dir=paths.prep)
        payload["subjects"] = [
            {"key": s.key, "family": s.family, "describe": s.describe}
            for s in design.subjects.values()]
        payload["default_subject"] = design.default_subject
    except Exception as e:                    # noqa: BLE001 - any bad YAML
        payload["subjects"] = []
        payload["subjects_error"] = str(e)
    return payload


def _install_sheet(paths: Paths, body: bytes, override: Path) -> dict:
    """Put a style sheet in force, but only once it loads.

    Written beside its destination and moved onto it, so a sheet that fails to
    parse never becomes the one prep reads, and a half-written file never
    exists under the name prep looks for."""
    override.parent.mkdir(parents=True, exist_ok=True)
    staging = override.with_name(override.name + ".uploading")
    staging.write_bytes(body)
    try:
        preplib.load_style_sheet(staging)
    except StyleSheetError as e:
        # The message names the file and the problem, and was written for
        # somebody editing YAML — but the staging path is ours, not theirs.
        raise HTTPException(400, str(e).replace(f"{staging}: ", "")
                                       .replace(str(staging), "that file"))
    except Exception as e:                    # noqa: BLE001 - any bad YAML
        raise HTTPException(400, f"That file could not be read: {e}")
    else:
        staging.replace(override)
        return _style_payload(paths)
    finally:
        staging.unlink(missing_ok=True)


def _shipped_template() -> Path:
    cfg = load_config(CONFIG_PATH)
    return preplib.pipeline.resolve(CONFIG_PATH.parent, cfg.prep.indesign_template)


def _template_override_path(paths: Paths) -> Path:
    """Where a replacement template lives — under the name the config asks for,
    because that is what `_resolve_template` looks for."""
    return paths.prep / _shipped_template().name


def _template_payload(paths: Paths) -> dict:
    """The house template in force, as the Settings screen reads it: shipped or
    uploaded, and enough of its shape to show it is a real template — how many
    stories and spreads, and which story the manuscript would flow into."""
    from docproof.prep.writers.indesign_idml import (body_style_names,
                                                     discover_body_story)
    shipped = _shipped_template()
    override = paths.prep / shipped.name
    active = override if override.is_file() else shipped
    info: dict = {"shipped_path": str(shipped), "override_path": str(override),
                  "using_override": override.is_file(), "name": active.name}
    try:
        with zipfile.ZipFile(active) as z:
            names = z.namelist()
        sheet = preplib.load_style_sheet(_shipped_sheet(), override_dir=paths.prep)
        info.update(
            ok=True,
            stories=sum(1 for n in names if n.startswith("Stories/Story_")),
            spreads=sum(1 for n in names if n.startswith("Spreads/Spread_")),
            body_story=discover_body_story(active, body_style_names(sheet)))
    except Exception as e:                    # noqa: BLE001 - a bad/absent template
        info.update(ok=False, error=str(e))
    return info


def _install_template(paths: Paths, body: bytes, override: Path) -> dict:
    """Put a template in force, but only once it reads as an IDML whose body
    story can be found — so a wrong file (a packaged .indd, a PDF) is refused
    here, in front of the person choosing it, not hours later at a run. Staged
    beside its destination and moved on, so prep never reads a half-written one."""
    from docproof.prep.writers.indesign_idml import (body_style_names,
                                                     discover_body_story)
    override.parent.mkdir(parents=True, exist_ok=True)
    staging = override.with_name(override.name + ".uploading")
    staging.write_bytes(body)
    try:
        try:
            with zipfile.ZipFile(staging) as z:
                has_stories = any(n.startswith("Stories/Story_")
                                  for n in z.namelist())
        except zipfile.BadZipFile:
            raise HTTPException(
                400, "That is not an IDML file. In InDesign, File → Export and "
                     "choose InDesign Markup (IDML).")
        if not has_stories:
            raise HTTPException(400, "That IDML has no stories — is it the right "
                                     "file?")
        sheet = preplib.load_style_sheet(_shipped_sheet(), override_dir=paths.prep)
        try:
            discover_body_story(staging, body_style_names(sheet))
        except ValueError as e:
            raise HTTPException(
                400, f"That template can't be used — {e}. It needs a text frame "
                     "the manuscript can flow into (a chapter frame styled with "
                     "your body text).")
        staging.replace(override)
        return _template_payload(paths)
    finally:
        staging.unlink(missing_ok=True)


def register(app: FastAPI) -> None:

    may_edit = common.admin_gate(
        app, "Only an administrator can change the house style guide.")

    @app.get("/api/prep/styles")
    def prep_styles() -> dict:
        """The house style set as loaded, and where to put a replacement.

        The point of this route is that the style guide is data: it shows the
        publisher exactly which file is in force and what is in it."""
        return _style_payload(app.state.paths)

    @app.post("/api/prep/styles/sheet", dependencies=[Depends(may_edit)])
    async def upload_style_sheet(file: UploadFile) -> dict:
        """Take a replacement style set from the Settings screen.

        It is written under the name the config asks for, whatever the file was
        called on the way in, because `load_style_sheet` finds a replacement by
        filename. Nothing is installed until it has been loaded successfully:
        a sheet with two styles sharing a name would otherwise take prep down
        at the next run, hours after the mistake was made."""
        override = _override_path(app.state.paths)
        raw = await file.read()
        if len(raw) > MAX_SHEET_BYTES:
            raise HTTPException(
                400, f"{file.filename} is larger than a style sheet can "
                     f"sensibly be. Is it the right file?")
        return _install_sheet(app.state.paths, raw, override)

    @app.delete("/api/prep/styles/sheet", dependencies=[Depends(may_edit)])
    def reset_style_sheet() -> dict:
        """Go back to the style set DocProof ships with. This also discards any
        adjustments made below — there is one file, so there is one undo."""
        _override_path(app.state.paths).unlink(missing_ok=True)
        return _style_payload(app.state.paths)

    @app.put("/api/prep/styles/format", dependencies=[Depends(may_edit)])
    def set_style_formats(req: SheetFormatUpdate) -> dict:
        """Adjust how the styles look, and nothing else.

        Only `format` values and the two sheet-level settings can be reached
        from here. Names, ids, descriptions and roles are copied through
        untouched: the name of a style is what InDesign matches when the file
        is placed and what the model is allowed to answer, so it is not a knob.

        The edited sheet is written to the same override file an uploaded one
        goes to, leaving the shipped copy alone."""
        paths: Paths = app.state.paths
        payload = _style_payload(paths)
        if not payload["ok"]:
            raise HTTPException(400, payload["error"])

        raw = yaml.safe_load(Path(payload["path"]).read_text("utf-8")) or {}
        entries = {str(e.get("name")): e for e in raw.get("styles") or []}
        unknown = [name for name in req.styles if name not in entries]
        if unknown:
            raise HTTPException(
                400, f"This style set has no style called "
                     f"'{unknown[0]}'. It has: "
                     f"{', '.join(sorted(entries))}.")

        for name, update in req.styles.items():
            fmt = dict(entries[name].get("format") or {})
            for key, value in update.model_dump(exclude_none=True).items():
                if key != "clear":
                    fmt[key] = value
            for key in update.clear:
                fmt.pop(key, None)
            entries[name]["format"] = fmt
        if req.trim is not None:
            raw["trim"] = req.trim
        if req.scene_break_glyph is not None:
            raw["scene_break_glyph"] = req.scene_break_glyph

        body = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True,
                              width=88).encode("utf-8")
        return _install_sheet(paths, body, _override_path(paths))


    @app.get("/api/prep/template")
    def prep_template() -> dict:
        """The house template in force, and where a replacement goes. Like the
        style guide, the template is data: this shows which file prep flows into
        and its shape."""
        return _template_payload(app.state.paths)

    @app.post("/api/prep/template", dependencies=[Depends(may_edit)])
    async def upload_template(file: UploadFile) -> dict:
        """Take a house template from the Settings screen, exported as IDML.

        Nothing is installed until it reads as an IDML with a findable body
        story, so a wrong file is refused here rather than at the next run."""
        if not (file.filename or "").lower().endswith(".idml"):
            raise HTTPException(
                400, "Upload an IDML file — in InDesign, File → Export and "
                     "choose InDesign Markup (IDML).")
        raw = await file.read()
        if len(raw) > MAX_TEMPLATE_BYTES:
            raise HTTPException(
                400, f"{file.filename} is larger than a template should be. Is "
                     f"it the right file (an IDML export, not a packaged .indd)?")
        return _install_template(app.state.paths, raw,
                                 _template_override_path(app.state.paths))

    @app.delete("/api/prep/template", dependencies=[Depends(may_edit)])
    def reset_template() -> dict:
        """Go back to the template DocProof ships with."""
        _template_override_path(app.state.paths).unlink(missing_ok=True)
        return _template_payload(app.state.paths)
