#!/usr/bin/env python3
"""Build the requested single-file premium comparison using approved local data."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from docproof.website.models import Asset, SiteSpec
from docproof.website.premium import STATIC, THEMES, render_premium_pages

SOURCE = ROOT / "output/website-sources/quinton-johnson"
OUTPUT = ROOT / "output/website-premium/quinn-hogshead"
FONT_INPUTS = (
    ('Marcellus', 'config/cover/fonts/Marcellus-Regular.ttf', 'config/cover/fonts/marcellus-OFL.txt'),
    ('Libre Caslon', 'config/cover/fonts/LibreCaslonDisplay-Regular.ttf', 'config/cover/fonts/librecaslondisplay-OFL.txt'),
    ('Bebas', 'config/cover/fonts/BebasNeue-Regular.ttf', 'config/cover/fonts/bebasneue-OFL.txt'),
    ('Cormorant', 'config/prep/fonts/CormorantGaramond-Medium.ttf', 'config/prep/fonts/licenses/cormorantgaramond-OFL.txt'),
    ('Spectral', 'config/prep/fonts/Spectral-Regular.ttf', 'config/prep/fonts/licenses/spectral-OFL.txt'),
)


def embedded_image(body: bytes, media_type: str) -> str:
    return f"data:{media_type};base64," + base64.b64encode(body).decode('ascii')


def font_css() -> tuple[str, dict]:
    from fontTools import subset
    from fontTools.ttLib import TTFont
    css, licenses = [], {}
    # Include Latin text, smart punctuation and UI arrows used in the review.
    codepoints = list(range(0x20, 0x250)) + list(range(0x2000, 0x2070)) + list(range(0x2190, 0x2200))
    for name, path, license_path in FONT_INPUTS:
        font = TTFont(ROOT / path)
        options = subset.Options()
        options.name_IDs = ['*']
        options.name_legacy = True
        options.name_languages = ['*']
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(unicodes=codepoints)
        subsetter.subset(font)
        font.flavor = 'woff'
        output = io.BytesIO()
        font.save(output)
        encoded = base64.b64encode(output.getvalue()).decode('ascii')
        css.append(f"@font-face{{font-family:'{name}';src:url(data:font/woff;base64,{encoded}) format('woff');font-weight:400;font-style:normal;font-display:swap;}}")
        licenses[name] = (ROOT / license_path).read_text()
    return '\n'.join(css), licenses


def build() -> Path:
    spec = SiteSpec.model_validate_json((SOURCE / 'site.json').read_text())
    rows = [Asset.model_validate(row) for row in json.loads((SOURCE / 'assets.json').read_text())]
    assets = {}
    for asset in rows:
        if not asset.approved:
            continue
        body = (SOURCE / 'assets' / asset.id).read_bytes()
        if hashlib.sha256(body).hexdigest() != asset.sha256:
            raise ValueError(f'Approved asset hash mismatch: {asset.id}')
        assets[asset.id] = embedded_image(body, asset.media_type)
    required = {spec.books[0].cover_asset_id, spec.author.portrait_asset_id}
    if not required.issubset(assets):
        raise ValueError('The approved cover and portrait must both be available.')
    backdrop = ROOT / 'output/website-premium/assets/everwatch.png'
    if not backdrop.is_file():
        raise FileNotFoundError('The original Everwatch backdrop has not been installed.')
    assets['everwatch.png'] = embedded_image(backdrop.read_bytes(), 'image/png')
    # A short contiguous opening excerpt; never bundle the complete manuscript.
    manuscript = (SOURCE / 'manuscript.txt').read_text()
    beginning = manuscript.index('I am bad at walking across corpses.')
    finish = manuscript.index('\n\nOne mortal wheezes', beginning)
    excerpt = manuscript[beginning:finish].strip()
    pages = render_premium_pages(spec, excerpt=excerpt)
    fonts, licenses = font_css()
    payload = {'themes': THEMES, 'pages': pages, 'assets': assets, 'fontCss': fonts, 'fontLicenses': licenses}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    for original, replacement in (('&', '\\u0026'), ('<', '\\u003c'), ('>', '\\u003e'), ('\u2028', '\\u2028'), ('\u2029', '\\u2029')):
        encoded = encoded.replace(original, replacement)
    shell = (STATIC / 'gallery.html').read_text()
    if shell.count('__PREMIUM_DATA__') != 1:
        raise ValueError('The review shell needs exactly one data slot.')
    rendered = shell.replace('__PREMIUM_DATA__', encoded)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / 'Witch-in-the-Wall-Premium-Website-Review.html'
    path.write_text(rendered)
    shutil.copyfile(path, OUTPUT / 'index.html')
    print(json.dumps({'file': str(path), 'bytes': path.stat().st_size, 'designs':len(THEMES), 'pages':sum(len(p) for p in pages.values()), 'assets':len(assets), 'fonts':len(FONT_INPUTS)}))
    return path


if __name__ == '__main__':
    build()
