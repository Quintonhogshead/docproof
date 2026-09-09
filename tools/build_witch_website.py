#!/usr/bin/env python3
"""Build a complete, offline Witch in the Wall website from approved assets."""
from __future__ import annotations

import base64
import hashlib
from html import escape
import io
import json
from math import ceil
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from docproof.website.models import Asset, SiteSpec

SOURCE = ROOT / 'output/website-sources/quinton-johnson'
STATIC = ROOT / 'app/static/websites/witch'
OUTPUT = ROOT / 'output/witch-in-the-wall'
DELIVERABLE = ROOT.parent / 'Witch-in-the-Wall.html'


def paragraphs(text: str) -> str:
    return '\n'.join('<p>' + escape(p.strip(), quote=True) + '</p>' for p in text.split('\n\n') if p.strip())


def data_uri(data: bytes, media_type: str) -> str:
    return f'data:{media_type};base64,' + base64.b64encode(data).decode('ascii')


def fonts() -> tuple[str, str]:
    from fontTools import subset
    from fontTools.ttLib import TTFont
    css, licenses = [], {}
    for name, path, license_path in (
        ('Marcellus', 'config/cover/fonts/Marcellus-Regular.ttf', 'config/cover/fonts/marcellus-OFL.txt'),
        ('Spectral', 'config/prep/fonts/Spectral-Regular.ttf', 'config/prep/fonts/licenses/spectral-OFL.txt'),
    ):
        font = TTFont(ROOT / path)
        options = subset.Options()
        options.name_IDs = ['*']
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(unicodes=list(range(32, 592)) + list(range(8192, 8304)) + list(range(8592, 8704)))
        subsetter.subset(font)
        font.flavor = 'woff'
        buffer = io.BytesIO()
        font.save(buffer)
        css.append(f"@font-face{{font-family:'{name}';src:url('{data_uri(buffer.getvalue(),'font/woff')}') format('woff');font-weight:400;font-style:normal;font-display:swap;}}")
        licenses[name] = (ROOT / license_path).read_text()
    return '\n'.join(css), json.dumps(licenses).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')


def build() -> Path:
    spec = SiteSpec.model_validate_json((SOURCE / 'site.json').read_text())
    approved = [Asset.model_validate(row) for row in json.loads((SOURCE / 'assets.json').read_text())]
    assets = {}
    for asset in approved:
        if not asset.approved:
            continue
        body = (SOURCE / 'assets' / asset.id).read_bytes()
        if hashlib.sha256(body).hexdigest() != asset.sha256:
            raise ValueError(f'Approved asset changed: {asset.id}')
        assets[asset.id] = data_uri(body,asset.media_type)
    backdrop = ROOT / 'output/website-premium/assets/everwatch.png'
    art = data_uri(backdrop.read_bytes(),'image/png')
    font_css, licenses = fonts()
    manuscript = (SOURCE / 'manuscript.txt').read_text()
    start = manuscript.index('I am bad at walking across corpses.')
    end = manuscript.index('=== OEBPS/chap005.xhtml ===', start)
    excerpt = manuscript[start:end].strip()
    quotes = ''.join(f'<blockquote><span class="quote-mark" aria-hidden="true">“</span><p>{escape(q.quote)}</p><cite>{escape(q.attribution)}</cite></blockquote>' for q in spec.praise)
    synopsis = spec.books[0].description.split('\n\n')
    replacements = {
        '__DESCRIPTION__':escape(spec.seo.description,quote=True),
        '__FONTS__':font_css,
        '__STYLES__':(STATIC/'site.css').read_text().replace('__EVERWATCH__',art),
        '__SCRIPT__':(STATIC/'site.js').read_text(),
        '__COVER__':assets[spec.books[0].cover_asset_id],
        '__PORTRAIT__':assets[spec.author.portrait_asset_id],
        '__BUY_URL__':escape(spec.primary_cta.url,quote=True),
        '__PUBLISHER_URL__':escape(spec.books[0].links[1].url,quote=True),
        '__BIO__':escape(spec.author.bio,quote=True),
        '__EMAIL__':escape(spec.contact.email,quote=True),
        '__SYNOPSIS_REST__':paragraphs('\n\n'.join(synopsis[1:])),
        '__EXCERPT__':paragraphs(excerpt),
        '__READING_TIME__':str(ceil(len(excerpt.split())/220)),
        '__REVIEWS__':quotes,
        '__LICENSES__':licenses,
    }
    markup = (STATIC/'site.html').read_text()
    for token, value in replacements.items():
        markup=markup.replace(token,value)
    if re.search(r'__[A-Z_]+__',markup):
        raise ValueError('Unresolved website content token.')
    OUTPUT.mkdir(parents=True,exist_ok=True)
    path = OUTPUT/'index.html'
    path.write_text(markup)
    shutil.copyfile(path, DELIVERABLE)
    print(json.dumps({'file':str(DELIVERABLE),'bytes':DELIVERABLE.stat().st_size,'excerpt_words':len(excerpt.split()),'embedded_images':3,'embedded_fonts':2}))
    return DELIVERABLE


if __name__ == '__main__':
    build()
