"""Portable premium concept renderer, separate from the production v1 contract.

Each direction has a complete four-page composition. The only dynamic form is
an explicitly labelled offline signup demonstration; it never retains an email.
"""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from .models import SiteSpec

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app/static/websites/premium"
THEMES = (
    {"id": "nocturne", "name": "Nocturne", "description": "Cinematic · midnight & gold", "color": "#c9b579"},
    {"id": "folio", "name": "The Folio", "description": "Literary · ink & paper", "color": "#eadfc9"},
    {"id": "voltage", "name": "Voltage", "description": "Graphic · electric blue", "color": "#2765ee"},
    {"id": "reliquary", "name": "The Reliquary", "description": "Gothic · oxblood & brass", "color": "#723c46"},
    {"id": "afterlight", "name": "Afterlight", "description": "Personal · teal & white", "color": "#127267"},
)
PAGES = ("home", "books", "about", "contact")
LABELS = {"home": "Home", "books": "The book", "about": "The author", "contact": "Contact"}


def txt(value: Any) -> str:
    return escape(str(value or ""), quote=True)


def paras(value: str, cls: str = "") -> str:
    return "".join(f'<p class="{cls}">{txt(p)}</p>' for p in value.split("\n\n") if p.strip())


def img(asset: str, alt: str, cls: str, *, eager: bool = False) -> str:
    dimensions = 'width="800" height="1237"' if "witch-in" in asset else 'width="1152" height="2048"'
    loading = 'fetchpriority="high" loading="eager"' if eager else 'loading="lazy"'
    return f'<img src="__ASSET__/{asset}" alt="{txt(alt)}" class="{cls}" {dimensions} {loading}>'


def cover(spec: SiteSpec, cls: str = "book-cover", eager: bool = False) -> str:
    return img(spec.books[0].cover_asset_id, f"Cover of {spec.books[0].title} by {spec.author.name}", cls, eager=eager)


def portrait(spec: SiteSpec, eager: bool = False) -> str:
    return img(spec.author.portrait_asset_id, f"{spec.author.name} smiling and holding a black-and-white pig", "portrait", eager=eager)


def action(spec: SiteSpec, *, sample: bool = True) -> str:
    buy = f'<a class="button primary" href="{txt(spec.primary_cta.url)}" target="_blank" rel="noopener noreferrer">Buy the book <span aria-hidden="true">↗</span></a>'
    more = '<a class="button secondary" href="#books">Explore the story <span aria-hidden="true">→</span></a>' if sample else ''
    return f'<div class="actions">{buy}{more}</div>'


def navigation(spec: SiteSpec, page: str, theme: str) -> str:
    items = ''.join(f'<a href="#{p}" {"aria-current=page" if p == page else ""}>{LABELS[p]}</a>' for p in PAGES if p != "home")
    logo = f'<a class="wordmark" href="#home">{txt(spec.author.name)}<span class="brand-dot" aria-hidden="true">.</span></a>'
    return f'''<a class="skip" href="#main">Skip to content</a>
    <header class="site-header wrap">{logo}<button class="menu-toggle" aria-controls="main-nav" aria-expanded="false">Menu <span aria-hidden="true">+</span></button>
    <nav id="main-nav" aria-label="Main navigation">{items}<button class="nav-letter" data-signup>Letters from Quinn <span aria-hidden="true">↗</span></button></nav></header>'''


def praise(spec: SiteSpec, *, full: bool = False) -> str:
    if not spec.praise:
        return ''
    rows = spec.praise if full else spec.praise[:1]
    if not full:
        # Exact contiguous excerpt from the approved endorsement, labelled below.
        quote = spec.praise[0].quote.split('. ', 1)[0] + '.'
        return f'<section class="praise-strip wrap"><span class="eyebrow">Early praise</span><blockquote>“{txt(quote)}”<cite>{txt(rows[0].attribution)} <span class="quote-note">· endorsement excerpt</span></cite></blockquote></section>'
    return '<section class="praise-section wrap"><div class="section-head"><p class="eyebrow">In good company</p><h2>Words from fellow writers.</h2></div><div class="praise-grid">' + ''.join(
        f'<blockquote><span class="quote-mark" aria-hidden="true">“</span><p>{txt(p.quote)}</p><cite>{txt(p.attribution)}</cite></blockquote>' for p in rows) + '</div></section>'


def newsletter(theme: str) -> str:
    heading = {
        'nocturne': 'A letter from beyond the wall.',
        'folio': 'A place for the next chapter.',
        'voltage': 'MORE MAGIC.\nLESS NOISE.',
        'reliquary': 'Some stories find their way to you.',
        'afterlight': 'Let’s keep the story going.',
    }[theme]
    return f'''<section class="newsletter wrap"><div><p class="eyebrow">Letters from Quinn</p><h2>{txt(heading).replace(chr(10), '<br>')}</h2><p>Book news, writing notes, and occasional dispatches from the world of the Squall.</p></div><button class="button primary" data-signup>Join the reader list <span aria-hidden="true">↗</span></button></section>'''


def footer(spec: SiteSpec) -> str:
    return f'''<footer class="site-footer wrap"><a class="wordmark" href="#home">{txt(spec.author.name)}.</a><p>Witch in the Wall · The Squall Series</p><div><a href="#contact">Get in touch</a><button data-signup>Reader letters</button></div><small>© 2026 {txt(spec.author.name)}</small></footer>'''


def signup(spec: SiteSpec, theme: str) -> str:
    headings = {'nocturne': 'Beyond the wall,\nthe story continues.', 'folio': 'Good stories.\nAn occasional letter.',
                'voltage': 'STAY IN\nTHE SQUALL.', 'reliquary': 'A letter.\nA little dark magic.', 'afterlight': 'A note from me\nto your inbox.'}
    return f'''<dialog class="signup-dialog" id="signup" aria-labelledby="signup-title" aria-describedby="signup-description">
      <button class="dialog-close" aria-label="Close newsletter signup">×</button>
      <div class="signup-art">{portrait(spec) if theme == 'afterlight' else cover(spec)}<p>Letters from Quinn</p></div>
      <div class="signup-copy"><p class="eyebrow">The reader list</p><h2 id="signup-title">{txt(headings[theme]).replace(chr(10), '<br>')}</h2>
      <p id="signup-description">Book news, writing notes, and occasional dispatches from the world of the Squall.</p>
      <form id="signup-form"><label for="reader-email">Your email address</label><input type="email" id="reader-email" name="email" autocomplete="email" placeholder="you@example.com" required maxlength="254" aria-describedby="signup-note">
      <button type="submit" class="button primary">Count me in <span aria-hidden="true">→</span></button><p id="signup-note" class="form-note">Design preview · no email is collected.</p></form>
      <div id="signup-result" class="signup-result" role="status" hidden><span class="result-mark" aria-hidden="true">✓</span><h3>That’s the signup experience.</h3><p>This is a design preview. Your email wasn’t saved or sent.</p><button class="button secondary" data-close>Back to the story →</button></div>
      <button class="not-now" data-close>Maybe another time</button></div></dialog>'''


def home(spec: SiteSpec, theme: str) -> str:
    book = spec.books[0]
    title = '<span>Witch in</span><em>the Wall.</em>'
    hook = '<p class="hero-hook">A captive witch. A city on the brink.<br>A bargain that could change everything.</p>'
    series = '<p class="eyebrow">Book One of the Squall Series</p>'
    if theme == 'nocturne':
        hero = f'''<section class="hero hero-nocturne"><div class="landscape" aria-hidden="true"></div><div class="wrap hero-grid"><div class="hero-copy">{series}<h1>{title}</h1><p class="tagline">Every escape has a price.</p>{hook}{action(spec)}</div><div class="cover-stage">{cover(spec, eager=True)}<span class="cover-caption">A novel by {txt(spec.author.name)}</span></div></div><div class="hero-tail wrap"><span>Dark fantasy · Dangerous bargains</span><a href="#the-world">Enter Everwatch ↓</a></div></section>'''
    elif theme == 'folio':
        hero = f'''<section class="hero hero-folio wrap"><div class="folio-topline"><span>The fiction of {txt(spec.author.name)}</span><span>The Squall Series / No. 01</span></div><div class="folio-grid"><div class="hero-copy"><p class="eyebrow">A novel of escape & consequence</p><h1>{title}</h1><p class="tagline">Every escape has a price.</p>{action(spec)}</div><figure class="folio-cover">{cover(spec, eager=True)}<figcaption>Witch in the Wall: Book I</figcaption></figure></div><div class="folio-bottom"><span>01 / The novel</span><p>Below a city that has forgotten mercy,<br>someone is still waiting to make a deal.</p><a class="text-link" href="#the-world">Turn the page ↓</a></div></section>'''
    elif theme == 'voltage':
        hero = f'''<section class="hero hero-voltage"><div class="voltage-heading wrap"><p class="eyebrow">{txt(spec.author.name)} / The Squall Series</p><h1>WITCH IN<br><span>THE WALL</span><sup>01</sup></h1></div><div class="voltage-bottom wrap"><div class="voltage-copy"><p class="eyebrow">Dark magic. Bad bargains.</p><h2>EVERY ESCAPE<br>HAS A PRICE.</h2><p>Enter Everwatch. A prison-city. A war for the throne. A witch with an offer.</p>{action(spec)}</div><div class="voltage-cover">{cover(spec, eager=True)}<span class="vertical-label">BOOK ONE / QUINN HOGSHEAD</span></div></div></section>'''
    elif theme == 'reliquary':
        hero = f'''<section class="hero hero-reliquary wrap"><div class="relic-heading"><p class="eyebrow">The Squall Series · Book the First</p><h1>Witch in the Wall</h1><div class="ornament" aria-hidden="true">— ✦ —</div></div><div class="relic-grid"><div class="relic-intro"><p class="eyebrow">The invitation</p><h2>Some doors<br>should stay<br><em>closed.</em></h2><p>A witch beneath the city.<br>A kingdom at war with itself.<br>A way out—for a price.</p><a class="text-link" href="#books">Discover the novel →</a></div><div class="relic-cover">{cover(spec, eager=True)}</div><div class="relic-aside"><span class="relic-number">I</span><p>For those drawn to cursed kingdoms, dangerous bargains, and complicated loyalties.</p>{action(spec, sample=False)}</div></div></section>'''
    else:
        hero = f'''<section class="hero hero-afterlight wrap"><div class="afterlight-grid"><div class="author-hero-photo">{portrait(spec, eager=True)}<span class="photo-label">Quinn, away from the writing desk.</span></div><div class="hero-copy"><p class="eyebrow">Fantasy author / Orlando, Florida</p><h1>Quinn<br><em>Hogshead.</em></h1><p class="tagline">Dark fantasy.<br>Very human stakes.</p><p class="hero-hook">Stories of fractured loyalties, dangerous magic, and the things we do for a way out.</p><a class="button primary" href="#books">Meet Witch in the Wall <span aria-hidden="true">↗</span></a><div class="afterlight-mini">{cover(spec, cls='mini-cover', eager=True)}<div><p class="eyebrow">The novel</p><h3>Witch in the Wall</h3><a class="text-link" href="{txt(spec.primary_cta.url)}" target="_blank" rel="noopener noreferrer">Buy Book I →</a></div></div></div></div></section>'''
    world = f'''<section class="world-section wrap" id="the-world"><div class="section-head"><p class="eyebrow">Welcome to Everwatch</p><h2>A city built to<br>keep you <em>in.</em></h2></div><div class="world-copy"><p class="lead">Gavriel Hall wants a life beyond the boundary stones. The witch below Everwatch wants something in return.</p><p>He is the bastard son of a dead king, a reluctant scribe, and a spectacularly unreliable liar. As his half-siblings’ war turns his home into a battlefield, the escape he has always wanted begins to look dangerously possible.</p><a class="text-link" href="#books">Explore the story →</a></div></section>'''
    themes = '''<section class="story-threads wrap" aria-label="Inside the story"><article><span>01</span><h3>A buried witch.</h3><p>Imprisoned longer than the faerie lords can remember. Still powerful enough to offer a way out.</p></article><article><span>02</span><h3>A divided kingdom.</h3><p>A dead king. Rival heirs. A prison-city caught in their war for the throne.</p></article><article><span>03</span><h3>A dangerous longing.</h3><p>A best friend with secrets. An ill-timed kiss. A future that suddenly feels out of reach.</p></article></section>'''
    author_section = f'''<section class="author-feature wrap"><div class="author-feature-image">{portrait(spec)}</div><div><p class="eyebrow">The person behind the pages</p><h2>{txt(spec.author.name)}.</h2><p class="lead">{txt(spec.author.bio_short)}</p><p>Miniature war games, cooking, long walks—and a wonderful cat.</p><a class="text-link" href="#about">Meet Quinn →</a></div></section>'''
    return hero + praise(spec) + world + themes + (author_section if theme != 'afterlight' else '') + newsletter(theme)


def books(spec: SiteSpec, theme: str, excerpt: str) -> str:
    book = spec.books[0]
    retail = ''.join(f'<a class="retailer" href="{txt(l.url)}" target="_blank" rel="noopener noreferrer"><span>{txt(l.label)}</span><span aria-hidden="true">↗</span></a>' for l in book.links)
    sample = f'''<section class="reading-section wrap" id="opening"><div class="section-head"><p class="eyebrow">Between the pages</p><h2>Read the opening.</h2><p>Prologue<br>Nine Hundred Ninety-Eight Years Ago</p></div><div class="reading-copy">{paras(excerpt, 'excerpt-paragraph')}<span class="end-mark" aria-hidden="true">✦</span><p class="form-note">Opening excerpt from Witch in the Wall: Book I.</p></div></section>''' if excerpt else ''
    return f'''<section class="book-page-top wrap"><div class="page-heading"><p class="eyebrow">The Squall Series / Book One</p><h1>Witch in<br><em>the Wall.</em></h1></div><div class="book-detail"><div class="book-detail-image">{cover(spec,eager=True)}<p class="cover-caption">{txt(book.subtitle)}</p></div><div><p class="lead">{txt(book.hook)}</p>{paras(book.description)}<div class="retailers">{retail}</div>{'<a class="text-link" href="#opening">Read the opening ↓</a>' if excerpt else ''}</div></div></section>''' + sample + praise(spec, full=True) + newsletter(theme)


def about(spec: SiteSpec, theme: str) -> str:
    return f'''<section class="about-page wrap"><div class="page-heading"><p class="eyebrow">The author</p><h1>Quinn<br><em>Hogshead.</em></h1></div><div class="about-grid"><figure class="about-photo">{portrait(spec,eager=True)}<figcaption>A little time away from the imaginary worlds.</figcaption></figure><div class="about-copy"><p class="eyebrow">Orlando, Florida</p><h2>There’s a world<br>behind every wall.</h2>{paras(spec.author.bio, 'lead')}<div class="author-details"><div><span>On the page</span><p>Dark fantasy. Difficult choices.<br>Complicated loyalties.</p></div><div><span>Off the page</span><p>Cooking. Miniature war games.<br>Long walks (not on the beach).</p></div></div><a class="text-link" href="#contact">A note to Quinn →</a></div></div><div class="about-book">{cover(spec,cls='mini-cover')}<div><p class="eyebrow">Discover the novel</p><h2>Witch in the Wall.</h2><p>Book One of the Squall Series.</p></div><a class="button primary" href="#books">Explore the book →</a></div></section>''' + newsletter(theme)


def contact(spec: SiteSpec, theme: str) -> str:
    return f'''<section class="contact-page wrap"><div class="page-heading"><p class="eyebrow">Get in touch</p><h1>A word beyond<br><em>the wall.</em></h1><p class="lead">{txt(spec.contact.message)}</p></div><div class="contact-grid"><div class="contact-email-panel"><p class="eyebrow">Write to Quinn</p><a class="contact-email" href="mailto:{txt(spec.contact.email)}">{txt(spec.contact.email)}</a><p>Readers, book clubs, interviews, and event invitations.</p><a class="text-link" href="mailto:{txt(spec.contact.email)}">Open your email app ↗</a></div><div class="contact-reader-panel"><p class="eyebrow">Prefer a letter?</p><h2>A little more<br>of the story.</h2><p>Join the reader list for book news and writing notes.</p><button class="button primary" data-signup>Join the list →</button></div></div><div class="contact-signoff"><div>{portrait(spec)}</div><p>Thanks for reading.<br><span>Quinn.</span></p></div></section>'''


def render_premium_pages(spec: SiteSpec | dict, *, excerpt: str = "") -> dict[str, dict[str, str]]:
    public = spec if isinstance(spec, SiteSpec) else SiteSpec.model_validate(spec)
    if not public.books or not public.author.portrait_asset_id:
        raise ValueError("This premium review requires the supplied book and author portrait.")
    css = (STATIC / "sites.css").read_text()
    js = (STATIC / "sites.js").read_text()
    pages: dict[str, dict[str, str]] = {}
    for theme in THEMES:
        family = theme['id']
        pages[family] = {}
        for page in PAGES:
            body = home(public, family) if page == 'home' else books(public, family, excerpt) if page == 'books' else about(public, family) if page == 'about' else contact(public, family)
            pages[family][page] = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{txt(public.author.name)} · {LABELS[page]} · {theme['name']}</title><meta name="description" content="{txt(public.seo.description)}"><style>__FONTS__\n{css}</style></head><body class="theme-{family} page-{page}">{navigation(public,page,family)}<main id="main">{body}</main>{footer(public)}{signup(public,family)}<script>{js}</script></body></html>'''
    return pages
