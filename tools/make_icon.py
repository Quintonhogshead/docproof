"""Draw the DocProof app icon and package it as app/DocProof.icns.

    .venv/bin/python tools/make_icon.py

The .icns is checked in, so a build needs nothing from this file — it exists so
the icon can be changed by editing values here rather than by opening a design
tool. Rendering is Core Graphics through pyobjc, which is already in the
environment because pywebview depends on it.

Each size is drawn at its own resolution rather than scaled down from one big
image: at 32 points a stacked wordmark is a smudge, so the small sizes carry
the monogram instead. That is the same simplification Apple's own icons make.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import AppKit
import Foundation

ROOT = Path(__file__).resolve().parent.parent
ICONSET = ROOT / "build" / "DocProof.iconset"
ICNS = ROOT / "app" / "DocProof.icns"

# The app's own palette, so the icon and the window agree.
INK = (0x7c / 255, 0x4a / 255, 0x2d / 255)          # accent brown
INK_DEEP = (0x5d / 255, 0x36 / 255, 0x1f / 255)
PAPER = (0xfb / 255, 0xfa / 255, 0xf8 / 255)

# (pixel size, filename) — the set macOS expects inside an .iconset.
VARIANTS = [
    (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
]


def _color(rgb, alpha=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, alpha)


def _text(string: str, size: float, color):
    font = AppKit.NSFont.systemFontOfSize_weight_(size, AppKit.NSFontWeightBold)
    return Foundation.NSAttributedString.alloc().initWithString_attributes_(
        string, {AppKit.NSFontAttributeName: font,
                 AppKit.NSForegroundColorAttributeName: color})


def _draw_centered(text, cx: float, cy: float) -> None:
    size = text.size()
    text.drawAtPoint_(Foundation.NSMakePoint(cx - size.width / 2,
                                             cy - size.height / 2))


def _fit(string: str, target_width: float, color):
    """Text sized so the word is `target_width` wide.

    Sizing by point size instead would make "Proof" overrun the tile while
    "Doc" floated in the middle of it — the two lines have to be set to a
    width, not to a size."""
    probe = _text(string, 100.0, color)
    return _text(string, 100.0 * target_width / probe.size().width, color)


def render(px: int) -> bytes:
    rep = AppKit.NSBitmapImageRep.alloc().\
        initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, px, px, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(context)

    # The rounded square, in the macOS proportion, inset so it doesn't touch
    # the edge of its tile.
    inset = px * 0.06
    side = px - inset * 2
    body = Foundation.NSMakeRect(inset, inset, side, side)
    radius = side * 0.2237
    tile = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        body, radius, radius)

    gradient = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
        _color(INK), _color(INK_DEEP))
    gradient.drawInBezierPath_angle_(tile, -90.0)

    paper = _color(PAPER)
    if px < 64:
        # Two letters, as large as they will go: at this size a wordmark is a
        # grey smear, and a monogram still reads as this app.
        _draw_centered(_fit("DP", side * 0.58, paper), px / 2, px / 2)
    else:
        _draw_centered(_fit("Doc", side * 0.44, paper), px / 2, px * 0.635)
        _draw_centered(_fit("Proof", side * 0.62, paper), px / 2, px * 0.435)
        # A proof mark under the word: the reason the app exists, in one line.
        rule_w, rule_h = side * 0.34, max(1.0, side * 0.026)
        rule = Foundation.NSMakeRect((px - rule_w) / 2, px * 0.245,
                                     rule_w, rule_h)
        paper.setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rule, rule_h / 2, rule_h / 2).fill()

    AppKit.NSGraphicsContext.restoreGraphicsState()
    return bytes(rep.representationUsingType_properties_(
        AppKit.NSBitmapImageFileTypePNG, {}))


def main() -> int:
    if ICONSET.exists():
        shutil.rmtree(ICONSET)
    ICONSET.mkdir(parents=True)
    for px, name in VARIANTS:
        (ICONSET / name).write_bytes(render(px))
    ICNS.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return 1
    print(f"wrote {ICNS} ({ICNS.stat().st_size:,} bytes) from "
          f"{len(VARIANTS)} renderings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
