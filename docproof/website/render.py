"""Deterministic, deliberately small public website renderer.

The generation pipeline produces data, never markup.  This module is the only
place that turns a SiteSpec into a public page so the staff preview and the
WordPress release have precisely the same HTML.
"""
from __future__ import annotations

from html import escape
from typing import Any, Mapping
from urllib.parse import urlparse

PAGES = ("home", "about", "books", "contact")
TEMPLATES = (
    "literary-journal", "midnight-narrative", "public-voice",
    "cover-gallery", "storybook-studio",
)


def _plain(value: Any) -> str:
    """Render untrusted content as text; line breaks are retained safely."""
    return escape(str(value or ""), quote=True).replace("\n", "<br>\n")


def _data(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif hasattr(value, "dict"):
        value = value.dict()
    return dict(value or {})


def _url(value: Any, *, mailto: bool = False) -> str:
    value = str(value or "").strip()
    parsed = urlparse(value)
    allowed = {"http", "https"} | ({"mailto"} if mailto else set())
    return value if parsed.scheme.lower() in allowed else ""


def _asset_index(assets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows = [_data(asset) for asset in assets]
    return {str(a.get("id")): a for a in rows if a.get("approved") and a.get("id")}


def _asset(asset_id: Any, assets: Mapping[str, dict[str, Any]], asset_base: str,
           label: str, klass: str = "media") -> str:
    item = assets.get(str(asset_id or ""))
    if not item:
        return f'<div class="{klass} media--empty" aria-label="{_plain(label)}"><span>{_plain(label)}</span></div>'
    base = asset_base.rstrip("/")
    # ids are a server-owned ASCII token and are never a supplied URL.
    src = f"{base}/{item['id']}" if base else f"__DOCPROOF_ASSETS__/{item['id']}"
    alt = item.get("alt") or label
    def focal(key: str) -> float:
        try:
            point = float(item.get(key, .5))
        except (TypeError, ValueError):
            point = .5
        return max(0, min(100, point * 100 if point <= 1 else point))
    focal_x, focal_y = focal("focal_x"), focal("focal_y")
    return (f'<img class="{klass}" src="{escape(src, quote=True)}" alt="{_plain(alt)}" '
            f'style="object-position:{focal_x:g}% {focal_y:g}%" loading="lazy">')


def _monogram(name: Any) -> str:
    words = [part for part in str(name or "Author").replace("-", " ").split() if part]
    letters = "".join(word[0] for word in words[:2]).upper() or "A"
    return f'<div class="portrait-monogram" aria-label="{_plain(name or "Author")}"><span>{_plain(letters)}</span></div>'


def _portrait(author: Mapping[str, Any], assets: Mapping[str, dict[str, Any]],
              asset_base: str) -> str:
    """A designed typographic fallback is more intentional than a fake photo."""
    asset_id = author.get("portrait_asset_id")
    if str(asset_id or "") not in assets:
        return _monogram(author.get("name"))
    return _asset(asset_id, assets, asset_base,
                  f'Portrait of {author.get("name") or "the author"}', "portrait")


def _link(label: Any, url: Any, klass: str = "link") -> str:
    safe = _url(url, mailto=True)
    return (f'<a class="{klass}" href="{escape(safe, quote=True)}">{_plain(label)}</a>'
            if safe and label else "")


def _nav(author: dict[str, Any], page: str, route_base: str) -> str:
    brand = _plain(author.get("name") or "Author")
    items = "".join(
        f'<a href="{escape(route_base, quote=True)}/{name}"{(" aria-current=\"page\"" if page == name else "")}>{name.title()}</a>'
        for name in PAGES)
    return f'''<header class="site-header"><a class="wordmark" href="{escape(route_base, quote=True)}/home">{brand}</a>
    <button class="menu-button" type="button" aria-expanded="false" aria-controls="site-nav">Menu</button>
    <nav id="site-nav" aria-label="Main navigation">{items}</nav></header>'''


def _footer(author: dict[str, Any], contact: dict[str, Any]) -> str:
    email = _url(f"mailto:{contact.get('email', '')}", mailto=True) if contact.get("email") else ""
    mail = f'<a href="{escape(email, quote=True)}">{_plain(contact.get("email"))}</a>' if email else ""
    return f'<footer><span>© { _plain(author.get("name") or "Author") }</span>{mail}<span>Made with care.</span></footer>'


def _book_card(book: dict[str, Any], assets: Mapping[str, dict[str, Any]], asset_base: str,
               *, featured: bool = False) -> str:
    title = book.get("title") or "Untitled book"
    links = "".join(_link(x.get("label") or "Learn more", x.get("url"), "book-link")
                    for x in book.get("links", []) if isinstance(x, Mapping))
    date = f'<p class="eyebrow">{_plain(book.get("publication_date"))}</p>' if book.get("publication_date") else ""
    hook = f'<p class="hook">{_plain(book.get("hook"))}</p>' if book.get("hook") else ""
    description = f'<p class="description">{_plain(book.get("description"))}</p>' if book.get("description") else ""
    return f'''<article class="book-card{' book-card--featured' if featured else ''}">
      <div class="cover-frame">{_asset(book.get('cover_asset_id'), assets, asset_base, f'Cover for {title}', 'cover')}</div>
      <div class="book-copy">{date}<h2>{_plain(title)}</h2>
      <p class="subtitle">{_plain(book.get('subtitle'))}</p>{hook}{description}
      <div class="book-links">{links}</div></div></article>'''


def _praise(spec: dict[str, Any]) -> str:
    rows = spec.get("praise") or []
    if not rows:
        return ""
    cards = "".join(f'<blockquote>“{_plain(x.get("quote"))}”<cite>{_plain(x.get("attribution"))}</cite></blockquote>'
                    for x in rows if isinstance(x, Mapping) and x.get("quote"))
    return f'<section class="praise" aria-label="Praise">{cards}</section>' if cards else ""


def _home(spec: dict[str, Any], assets: Mapping[str, dict[str, Any]], asset_base: str,
          template: str, route_base: str) -> str:
    author, books, cta = spec.get("author", {}), spec.get("books", []), spec.get("primary_cta", {})
    book = books[0] if books else {}
    cta_html = _link(cta.get("label") or "Explore the books", cta.get("url"), "button")
    if not cta_html:
        cta_html = f'<a class="button" href="{escape(route_base, quote=True)}/books">Explore the books</a>'
    kicker = f'The writings of {author.get("name")}' if author.get("name") else "The writings"
    hero_copy = f'<p class="kicker">{_plain(kicker)}</p><h1>{_plain(spec.get("headline") or author.get("name") or "A life in words")}</h1><p class="lede">{_plain(spec.get("intro") or author.get("bio_short"))}</p>{cta_html}'
    book_html = _book_card(book, assets, asset_base, featured=True) if book else ""
    portrait = _portrait(author, assets, asset_base)
    if template == "literary-journal":
        cover = _asset(book.get("cover_asset_id"), assets, asset_base, "Featured book cover", "hero-cover")
        return f'<main><section class="hero journal-hero"><div>{hero_copy}</div><div class="journal-cover-panel">{cover}<span class="journal-rule">Selected work</span></div></section><section class="feature split">{book_html}<aside><p class="eyebrow">The author</p><p>{_plain(author.get("bio_short"))}</p></aside></section>{_praise(spec)}</main>'
    if template == "midnight-narrative":
        return f'<main><section class="hero midnight-hero"><div>{hero_copy}</div>{_asset(book.get("cover_asset_id"), assets, asset_base, "Featured book cover", "hero-cover")}</section><section class="feature dark-feature">{book_html}</section>{_praise(spec)}</main>'
    if template == "public-voice":
        return f'<main><section class="hero voice-hero"><div class="portrait-wrap">{portrait}</div><div>{hero_copy}<div class="rule"></div></div></section><section class="feature">{book_html}</section>{_praise(spec)}</main>'
    if template == "cover-gallery":
        return f'<main><section class="hero gallery-hero"><div>{hero_copy}</div><div class="cover-wall">{_asset(book.get("cover_asset_id"), assets, asset_base, "Featured book cover", "hero-cover")}</div></section><section class="feature gallery-feature">{book_html}</section>{_praise(spec)}</main>'
    return f'<main><section class="hero story-hero"><div class="sun">✦</div><div>{hero_copy}</div>{_asset(book.get("cover_asset_id"), assets, asset_base, "Featured book cover", "hero-cover")}</section><section class="feature story-feature">{book_html}</section>{_praise(spec)}</main>'


def _about(spec: dict[str, Any], assets: Mapping[str, dict[str, Any]], asset_base: str,
           template: str) -> str:
    author = spec.get("author", {})
    portrait = _portrait(author, assets, asset_base)
    heading = _plain(author.get("name") or "About the author")
    bio = _plain(author.get("bio") or author.get("bio_short") or "A biography will appear here.")
    excerpt = _plain(spec.get("excerpt"))
    ex = f'<section class="excerpt"><p class="eyebrow">An excerpt</p><p>{excerpt}</p></section>' if excerpt else ""
    return f'<main class="about-page template-{template}"><section class="about-hero"><div class="portrait-wrap">{portrait}</div><div><p class="kicker">About</p><h1>{heading}</h1><p class="bio">{bio}</p></div></section>{ex}</main>'


def _books(spec: dict[str, Any], assets: Mapping[str, dict[str, Any]], asset_base: str,
           template: str) -> str:
    books = [x for x in spec.get("books", []) if isinstance(x, Mapping)]
    cards = "".join(_book_card(x, assets, asset_base, featured=i == 0) for i, x in enumerate(books))
    empty = '<p class="empty">Books are being added.</p>' if not cards else cards
    author = _data(spec.get("author"))
    heading = f'Books by {author.get("name")}' if author.get("name") else "Books"
    return f'<main class="books-page template-{template}"><section class="page-title"><p class="kicker">Books</p><h1>{_plain(heading)}</h1></section><section class="book-grid">{empty}</section></main>'


def _contact(spec: dict[str, Any], template: str) -> str:
    contact = spec.get("contact", {})
    email = _url(f"mailto:{contact.get('email', '')}", mailto=True) if contact.get("email") else ""
    route = (f'<a class="contact-email" href="{escape(email, quote=True)}">{_plain(contact.get("email"))}</a>'
             if email else '<p>Please return soon for contact details.</p>')
    links = "".join(_link(x.get("label"), x.get("url"), "social-link")
                    for x in contact.get("links", []) if isinstance(x, Mapping))
    return f'''<main class="contact-page template-{template}"><section class="contact-card">
      <p class="kicker">Contact</p><h1>{_plain(contact.get('headline') or 'Let’s stay in touch.')}</h1>
      <p class="lede">{_plain(contact.get('message') or 'For rights, events, and kind notes, please get in touch.')}</p>
      {route}<div class="social-links">{links}</div></section></main>'''


_STYLE = r'''<style>
*{box-sizing:border-box}html{font-size:16px}body{margin:0;background:var(--paper);color:var(--ink);font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.55}body.template-midnight-narrative{--paper:#11151b;--ink:#edf0ed;--muted:#aab4b1;--accent:#e3bb77;--wash:#1a222b}body.template-literary-journal{--paper:#f4f0e8;--ink:#1d2928;--muted:#69706c;--accent:#a84932;--wash:#e8dfd1}body.template-public-voice{--paper:#f7f7f4;--ink:#151a20;--muted:#626a72;--accent:#285a90;--wash:#e4eef3}body.template-cover-gallery{--paper:#f8f4ed;--ink:#251c1a;--muted:#756960;--accent:#bd4a33;--wash:#efded1}body.template-storybook-studio{--paper:#fbf7eb;--ink:#25363c;--muted:#597075;--accent:#db7650;--wash:#dbe9df}h1,h2,p{margin-top:0}h1,h2,.wordmark{font-family:Georgia,"Times New Roman",serif;line-height:.98;letter-spacing:-.035em}h1{font-size:clamp(3.1rem,9vw,7.5rem);max-width:13ch}h2{font-size:clamp(1.75rem,3vw,3rem)}a{color:inherit}.site-header{max-width:1400px;margin:auto;padding:1.5rem 5vw;display:flex;align-items:center;justify-content:space-between;gap:2rem}.wordmark{font-size:1.3rem;text-decoration:none;font-weight:bold}.site-header nav{display:flex;gap:1.15rem}.site-header nav a{text-decoration:none;text-transform:uppercase;font-size:.73rem;font-weight:700;letter-spacing:.1em}.site-header nav a[aria-current=page]{text-decoration:underline;text-underline-offset:.4em}.menu-button{display:none;background:transparent;border:1px solid currentColor;padding:.45rem .7rem;border-radius:2px;color:inherit}.hero,.feature,.about-hero,.book-grid,.contact-card,.page-title,.excerpt{max-width:1400px;margin:auto;padding-left:5vw;padding-right:5vw}.hero{min-height:68vh;display:grid;align-items:center;gap:4vw;padding-top:4rem;padding-bottom:6rem}.kicker,.eyebrow{text-transform:uppercase;letter-spacing:.16em;font-size:.7rem;font-weight:750;color:var(--accent);margin-bottom:1rem}.lede{font-size:clamp(1.1rem,1.8vw,1.45rem);max-width:43ch;color:var(--muted)}.button{display:inline-block;background:var(--ink);color:var(--paper);padding:.8rem 1.1rem;text-decoration:none;font-weight:700;margin-top:1rem}.journal-hero{grid-template-columns:2fr 1fr;border-bottom:1px solid var(--ink)}.journal-mark{font-family:Georgia,serif;font-size:15rem;text-align:center;color:var(--accent)}.split{display:grid;grid-template-columns:2fr 1fr;gap:7vw;padding-top:7rem;padding-bottom:7rem}.midnight-hero{grid-template-columns:1.15fr .85fr;background:radial-gradient(circle at 70% 30%,#303c4a,transparent 34%)}.hero-cover{max-width:100%;max-height:61vh;width:auto;justify-self:center;box-shadow:22px 26px 0 var(--wash);object-fit:contain}.dark-feature{background:var(--wash);max-width:none;padding:6rem max(5vw,calc((100vw - 1400px)/2 + 5vw))}.voice-hero{grid-template-columns:minmax(220px,.7fr) 1.3fr;background:var(--wash)}.portrait-wrap{aspect-ratio:4/5;overflow:hidden;background:var(--wash)}.portrait{width:100%;height:100%;object-fit:cover}.rule{height:2px;background:var(--accent);width:10rem;margin-top:3rem}.gallery-hero{grid-template-columns:1fr 1fr}.cover-wall{padding:6vw;background:var(--wash);transform:rotate(2deg)}.gallery-feature{border-top:1px solid var(--ink)}.story-hero{grid-template-columns:1.3fr .7fr;position:relative;background:var(--wash);border-radius:0 0 18% 0}.story-hero .sun{font-size:4rem;color:var(--accent);position:absolute;right:8%;top:9%}.feature{padding-top:6rem;padding-bottom:6rem}.book-card{display:grid;grid-template-columns:minmax(150px,280px) minmax(0,1fr);gap:clamp(2rem,6vw,8rem);align-items:center}.cover-frame{background:var(--wash);padding:1rem}.cover{display:block;width:100%;max-height:500px;object-fit:contain;aspect-ratio:2/3}.media--empty{display:grid;place-items:center;min-height:240px;background:var(--wash);color:var(--muted);padding:1rem;text-align:center}.subtitle{color:var(--muted);font-family:Georgia,serif;font-size:1.1rem}.book-links,.social-links{display:flex;gap:.7rem;flex-wrap:wrap;margin-top:1.5rem}.book-link,.social-link{font-weight:700;text-underline-offset:.25em}.praise{max-width:1400px;margin:auto;padding:0 5vw 6rem;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:2rem}.praise blockquote{margin:0;padding:2rem 0;border-top:1px solid var(--ink);font-family:Georgia,serif;font-size:1.3rem}.praise cite{display:block;font-family:ui-sans-serif,system-ui,sans-serif;font-size:.8rem;font-style:normal;margin-top:1rem;color:var(--muted)}.about-hero{display:grid;grid-template-columns:minmax(220px,.65fr) 1.35fr;gap:7vw;padding-top:5rem;padding-bottom:6rem}.bio{font-family:Georgia,serif;font-size:clamp(1.25rem,2vw,1.75rem);max-width:42ch;white-space:normal}.excerpt{padding-bottom:7rem;max-width:870px;margin-left:max(5vw,calc((100vw - 1400px)/2 + 5vw))}.excerpt>p:last-child{font-family:Georgia,serif;font-size:1.45rem;border-left:3px solid var(--accent);padding-left:2rem}.page-title{padding-top:6rem;padding-bottom:4rem}.book-grid{display:grid;gap:5rem;padding-bottom:7rem}.book-grid .book-card:not(:first-child){border-top:1px solid color-mix(in srgb,var(--ink),transparent 70%);padding-top:5rem}.contact-page{min-height:72vh;display:grid;place-items:center;padding:5vw}.contact-card{max-width:900px;background:var(--wash);padding-top:clamp(3rem,9vw,8rem);padding-bottom:clamp(3rem,9vw,8rem)}.contact-email{font-family:Georgia,serif;font-size:clamp(1.5rem,4vw,3rem);text-underline-offset:.2em;overflow-wrap:anywhere}footer{max-width:1400px;margin:auto;padding:2rem 5vw;display:flex;justify-content:space-between;gap:1rem;border-top:1px solid color-mix(in srgb,var(--ink),transparent 72%);font-size:.82rem;color:var(--muted)}@media(max-width:700px){.site-header{padding-top:1rem;position:relative}.menu-button{display:block}.site-header nav{display:none;position:absolute;right:5vw;top:4rem;z-index:2;background:var(--paper);border:1px solid var(--ink);padding:1rem;flex-direction:column;min-width:11rem}.site-header nav.is-open{display:flex}.hero,.journal-hero,.midnight-hero,.voice-hero,.gallery-hero,.story-hero,.about-hero{grid-template-columns:1fr;min-height:auto}.hero{padding-top:3rem}.journal-mark{display:none}.hero-cover{max-height:45vh;order:-1}.split,.book-card{grid-template-columns:1fr;gap:2rem}.book-card .cover-frame{max-width:230px}.portrait-wrap{max-width:340px}.about-hero{padding-top:3rem}.story-hero{border-radius:0}.story-hero .sun{display:none}footer{flex-direction:column}}@media(prefers-reduced-motion:no-preference){a,.button{transition:opacity .18s ease,transform .18s ease}.button:hover{transform:translateY(-2px);opacity:.9}.book-card{transition:transform .2s ease}.book-card:hover{transform:translateY(-3px)}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
</style>'''


_TEMPLATE_STYLE = r'''<style>
/* Composition changes per family, rather than a palette-only reskin. */
body.template-midnight-narrative{--paper:#0b1017;--ink:#eeeade;--muted:#afbac4;--accent:#e0c879;--wash:#111c27;--moon:#78d9f5}
.template-midnight-narrative .site-header{border-bottom:1px solid #e0c87926}
.template-midnight-narrative .midnight-hero{background:radial-gradient(ellipse at 79% 45%,#17647c30,transparent 43%),radial-gradient(ellipse at 20% 80%,#e0c87908,transparent 50%)}
.template-midnight-narrative .kicker{color:var(--moon)}
.template-midnight-narrative .hero-cover{box-shadow:0 0 70px #37bceb17,24px 24px 50px #0009;outline:1px solid #e0c87940;outline-offset:8px}
.template-midnight-narrative .button{background:var(--accent);color:var(--paper);padding:1rem 1.5rem;border:1px solid var(--accent);transition:background .2s,border-color .2s}
.template-midnight-narrative .button:hover{background:var(--moon);border-color:var(--moon)}
.template-midnight-narrative .button:focus-visible,.template-midnight-narrative a:focus-visible{outline:2px solid var(--moon);outline-offset:5px}
.template-midnight-narrative .dark-feature{border-top:1px solid #78d9f530}
.template-midnight-narrative .book-copy h2,.template-midnight-narrative .wordmark{color:var(--accent)}
.template-midnight-narrative .book-link{text-decoration-color:var(--moon);text-underline-offset:.35em}
.template-midnight-narrative footer{border-top:1px solid #e0c87926}
h1,h2,.wordmark,.contact-email{overflow-wrap:anywhere}
.media--empty{min-width:0}.cover-frame{min-width:0}
.portrait-monogram{width:100%;height:100%;min-height:280px;display:grid;place-items:center;background:linear-gradient(145deg,var(--wash),var(--paper));border:1px solid color-mix(in srgb,var(--ink),transparent 70%);color:var(--accent);font:clamp(4rem,11vw,8rem)/1 Georgia,serif}.portrait-monogram span{border:1px solid currentColor;border-radius:50%;height:1.35em;width:1.35em;display:grid;place-items:center}.journal-cover-panel{min-height:460px;background:var(--wash);display:grid;place-items:center;padding:clamp(2rem,5vw,5rem);position:relative}.journal-cover-panel .hero-cover{box-shadow:14px 17px 0 color-mix(in srgb,var(--accent),transparent 35%)}.journal-rule{position:absolute;left:1.25rem;bottom:1.1rem;writing-mode:vertical-rl;text-transform:uppercase;letter-spacing:.16em;font-size:.65rem;font-weight:800;color:var(--muted)}.hook{font-family:Georgia,serif;font-size:1.18rem;line-height:1.35;margin-bottom:.65rem}.description{max-width:62ch}
.books-page.template-literary-journal .page-title{border-bottom:3px double var(--ink)}
.books-page.template-literary-journal .book-card--featured{grid-template-columns:35% 1fr}
.about-page.template-literary-journal .about-hero{grid-template-columns:30% 1fr;border-bottom:1px solid var(--ink)}
.books-page.template-midnight-narrative .page-title{background:linear-gradient(120deg,var(--paper),var(--wash));padding-top:10rem}
.books-page.template-midnight-narrative .book-grid{background:var(--wash);max-width:none;padding-left:max(5vw,calc((100vw - 1400px)/2 + 5vw));padding-right:max(5vw,calc((100vw - 1400px)/2 + 5vw))}
.books-page.template-midnight-narrative .book-card{grid-template-columns:1fr minmax(150px,310px)}
.books-page.template-midnight-narrative .cover-frame{order:2}.about-page.template-midnight-narrative .about-hero{background:var(--wash);max-width:none;padding-left:max(5vw,calc((100vw - 1400px)/2 + 5vw));padding-right:max(5vw,calc((100vw - 1400px)/2 + 5vw))}
.books-page.template-public-voice .page-title h1{font-family:ui-sans-serif,system-ui,sans-serif;font-weight:800;letter-spacing:-.07em}.books-page.template-public-voice .book-grid{grid-template-columns:repeat(auto-fit,minmax(280px,1fr));align-items:start}.books-page.template-public-voice .book-card{display:block}.books-page.template-public-voice .cover-frame{max-width:280px}.books-page.template-public-voice .book-card:not(:first-child){border-top:0;padding-top:0}.about-page.template-public-voice .about-hero{grid-template-columns:1fr 1fr;align-items:center}.about-page.template-public-voice .bio{font-family:ui-sans-serif,system-ui,sans-serif;line-height:1.55}
.books-page.template-cover-gallery .page-title{text-align:center;padding-top:8rem}.books-page.template-cover-gallery .page-title h1{margin-inline:auto}.books-page.template-cover-gallery .book-grid{grid-template-columns:repeat(auto-fit,minmax(235px,1fr));gap:3rem}.books-page.template-cover-gallery .book-card{display:flex;flex-direction:column;gap:1.25rem}.books-page.template-cover-gallery .cover-frame{padding:1.25rem;background:var(--wash)}.books-page.template-cover-gallery .book-card:not(:first-child){border:0;padding-top:0}.about-page.template-cover-gallery .about-hero{grid-template-columns:1fr;max-width:900px}.about-page.template-cover-gallery .portrait-wrap{max-width:390px;transform:rotate(-2deg)}
.books-page.template-storybook-studio .page-title{background:var(--wash);border-radius:0 0 18% 0;padding-top:8rem}.books-page.template-storybook-studio .book-grid{gap:4rem}.books-page.template-storybook-studio .book-card{grid-template-columns:minmax(150px,250px) 1fr}.books-page.template-storybook-studio .cover-frame{border-radius:45% 45% 8% 8%;overflow:hidden}.about-page.template-storybook-studio .about-hero{background:var(--wash);border-radius:22% 0 0 0;padding-top:6rem}.about-page.template-storybook-studio .portrait-wrap{border-radius:48% 48% 12% 12%}
@media(max-width:700px){.about-page[class*="template-"] .about-hero{grid-template-columns:minmax(0,1fr);gap:2rem}.about-page .portrait-wrap{max-width:230px;width:100%}.about-page .portrait-monogram{max-height:270px}.books-page.template-midnight-narrative .book-card--featured,.books-page.template-literary-journal .book-card--featured,.books-page.template-storybook-studio .book-card{grid-template-columns:1fr}.books-page.template-midnight-narrative .cover-frame{order:0}.books-page.template-public-voice .book-grid,.books-page.template-cover-gallery .book-grid{grid-template-columns:1fr}.books-page.template-midnight-narrative .book-grid{padding-left:5vw;padding-right:5vw}}
</style>'''

_SCRIPT = """<script>document.querySelector('.menu-button')?.addEventListener('click',function(){const n=document.querySelector('#site-nav'),o=n.classList.toggle('is-open');this.setAttribute('aria-expanded',String(o));});</script>"""


def render_page(spec: Any, page: str, *, assets: list[dict[str, Any]] | None = None,
                asset_base: str = "", base_url: str = "", preview: bool = True,
                release_id: str = "") -> str:
    """Return a complete deterministic and escaped HTML page for *page*."""
    if page not in PAGES:
        raise ValueError(f"Unknown website page: {page}")
    data = _data(spec)
    template = data.get("template_id") or "literary-journal"
    if template not in TEMPLATES:
        template = "literary-journal"
    author, contact = _data(data.get("author")), _data(data.get("contact"))
    data["author"], data["contact"] = author, contact
    data["books"] = [_data(x) for x in data.get("books", [])]
    data["primary_cta"] = _data(data.get("primary_cta"))
    route_base = (base_url.rstrip("/") if base_url else "__DOCPROOF_BASE__")
    media = _asset_index(list(assets or []))
    bodies = {"home": _home, "about": _about, "books": _books}
    body = (_contact(data, template) if page == "contact" else
            _home(data, media, asset_base, template, route_base) if page == "home" else
            bodies[page](data, media, asset_base, template))
    desc = _plain(_data(data.get("seo")).get("description") or data.get("intro") or author.get("bio_short"))
    title = _plain(f"{author.get('name') or 'Author'} — {page.title()}")
    release = f' data-release="{_plain(release_id)}"' if release_id else ""
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="{desc}"><title>{title}</title>{_STYLE}{_TEMPLATE_STYLE}</head><body class="template-{template}"{release}>{_nav(author, page, route_base)}{body}{_footer(author, contact)}{_SCRIPT}</body></html>'''


def build_pages(spec: Any, *, assets: list[dict[str, Any]] | None = None,
                asset_base: str = "", base_url: str = "", preview: bool = True,
                release_id: str = "") -> dict[str, str]:
    """Build the immutable four-page bundle used for review and publishing."""
    return {page: render_page(spec, page, assets=assets, asset_base=asset_base,
                              base_url=base_url, preview=preview,
                              release_id=release_id) for page in PAGES}
