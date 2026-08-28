/* Spell & Check — shared data and behaviors for the papery pages. */
(function (global) {
  'use strict';

  var ICONS = {
    dagger: '<path d="M12 3l2 7-2 11-2-11 2-7z"/><path d="M8 10h8"/>',
    shield: '<path d="M12 3l7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6l7-3z"/><path d="M9 12l2 2 4-4"/>',
    book: '<path d="M5 4h11a3 3 0 013 3v13H8a3 3 0 01-3-3V4z"/><path d="M5 4a3 3 0 013 3v13"/><path d="M12 9h4M12 13h4"/>',
    hammer: '<path d="M4 15l7-7 3 3-7 7H4v-3z"/><path d="M13 6l3-3 5 5-3 3"/>',
    staff: '<path d="M12 21V8"/><circle cx="12" cy="5" r="2.5"/><path d="M9 12h6"/>',
    lute: '<circle cx="9" cy="15" r="5"/><path d="M12.5 11.5L20 4"/><path d="M18 3l3 3"/>',
    lantern: '<rect x="8" y="7" width="8" height="11" rx="2"/><path d="M10 7V5h4v2"/><path d="M12 11v3"/>'
  };
  function svg(k) { return '<svg viewBox="0 0 24 24" aria-hidden="true">' + ICONS[k] + '</svg>'; }

  /* ---- art registry + loader ------------------------------------------
     One entry per asset; every render point pulls its path from here. Swap a
     value to a commissioned .png/.webp and the loader auto-falls-back to an
     <img> — no render-code touched. Files live in /assets/art/. */
  var ART_BASE = '/assets/art/';
  var ART = {
    galley: 'galley.svg',
    pip: 'pip.svg', bram: 'bram.svg', maple: 'maple.svg',
    cinder: 'cinder.svg', sage: 'sage.svg', lark: 'lark.svg',
    crestSpellcheck: 'crest-spellcheck.svg', crestTypohunt: 'crest-typohunt.svg',
    crestProofread: 'crest-proofread.svg', crestDeep: 'crest-deep.svg',
    crestCampaign: 'crest-campaign.svg',
    ornamentDivider: 'ornament-divider.svg', ornamentCorner: 'ornament-corner.svg',
    dropcap: 'dropcap-frame.svg', favicon: 'favicon.svg', og: 'og.svg'
  };
  var ART_PATHS = {};
  Object.keys(ART).forEach(function (k) { ART_PATHS[k] = ART_BASE + ART[k]; });

  var RASTER = /\.(png|jpe?g|webp|avif|gif)$/i;
  var artCache = {};                       // path -> Promise<markup|null>
  function fetchArt(path) {
    if (!artCache[path]) {
      artCache[path] = fetch(path)
        .then(function (r) { return r.ok ? r.text() : null; })
        .catch(function () { return null; });
    }
    return artCache[path];
  }
  function injectOne(host) {
    if (!host || host.dataset.artLoaded) return;
    var file = ART[host.dataset.art];
    if (!file) return;
    host.dataset.artLoaded = '1';
    host.classList.add('art-figure');
    var path = ART_BASE + file;
    if (RASTER.test(file)) {
      var img = new Image();
      img.src = path; img.alt = ''; img.setAttribute('aria-hidden', 'true');
      host.appendChild(img);
      return;
    }
    fetchArt(path).then(function (markup) {
      if (!markup) { host.dataset.artLoaded = ''; return; }
      host.innerHTML = markup;
      var el = host.querySelector('svg');
      if (el) { el.setAttribute('aria-hidden', 'true'); el.classList.add('artwork'); }
    });
  }
  function injectArt(root) {
    (root || document).querySelectorAll('[data-art]').forEach(injectOne);
  }
  // Auto-inject anything added later (dynamically rendered members/tiers), so
  // pages never wire the loader themselves.
  if (typeof MutationObserver !== 'undefined') {
    new MutationObserver(function (muts) {
      muts.forEach(function (m) {
        m.addedNodes.forEach(function (n) {
          if (n.nodeType !== 1) return;
          if (n.matches && n.matches('[data-art]')) injectOne(n);
          if (n.querySelectorAll) n.querySelectorAll('[data-art]').forEach(injectOne);
        });
      });
    }).observe(document.documentElement, { childList: true, subtree: true });
  }
  function artFigure(key, cls) {
    return '<span class="art-figure' + (cls ? ' ' + cls : '') + '" data-art="' + key + '"></span>';
  }

  // The permanent party. `plain` is the no-prior-knowledge sentence; `lane`
  // is the honest machine name underneath.
  // `role` is the plain job word a stranger scans — the fantasy classes
  // (Scout, Knight, Bard) live only in the candlelit workshop, where a Cozy
  // Coastal Mystery won't trip over them.
  var MEMBERS = [
    { id: 'pip', name: 'Pip', role: 'Typos', icon: 'dagger',
      plain: 'Reads every page hunting typos and misspelled words.',
      lane: 'spelling_sweep · AI ensemble' },
    { id: 'bram', name: 'Bram', role: 'Grammar', icon: 'shield',
      plain: 'Checks grammar and punctuation, sentence by sentence.',
      lane: 'grammar_watch · rules + AI judge' },
    { id: 'maple', name: 'Maple', role: 'Consistency', icon: 'book',
      plain: 'Makes sure names and spellings stay identical from page 12 to page 312.',
      lane: 'consistency_scan · deterministic' },
    { id: 'cinder', name: 'Cinder', role: 'Repairs', icon: 'hammer',
      plain: 'Repairs sentences that came out broken or garbled.',
      lane: 'repair_channel · density-triggered' },
    { id: 'sage', name: 'Sage', role: 'Continuity', icon: 'staff',
      plain: 'Remembers the whole book — flags it if the timeline or an eye color quietly changes.',
      lane: 'continuity · whole-book pass' },
    { id: 'lark', name: 'Lark', role: 'Style', icon: 'lute',
      plain: 'Suggests where a line could read better — always as a question, never a rewrite.',
      lane: 'smoothing · query-only' }
  ];

  // The five rungs. Prices anchor a 60–120k-word manuscript; the band from the
  // quote scales the paid fixed tiers. `party` names who rides.
  var TIERS = [
    { id: 'spellcheck', name: 'Spellcheck', sub: "Galley's lantern, alone",
      price: 0, priceLabel: 'Free', crest: 'crestSpellcheck',
      blurb: 'The mechanical pass: dictionary, house sweeps, and the consistency scans. No AI reads your book. Results by email.',
      party: [] },
    { id: 'typohunt', name: 'Typo Hunt', sub: 'Pip rides alone',
      price: 9, crest: 'crestTypohunt',
      blurb: 'Pip reads every page for typos and misspellings, plus everything in Spellcheck.',
      party: ['pip'] },
    { id: 'proofread', name: 'Proofread', sub: 'The party of four',
      price: 29, recommended: true, crest: 'crestProofread',
      blurb: 'Typos, grammar, consistency, and broken sentences — the full sweep, delivered as tracked changes.',
      party: ['pip', 'bram', 'maple', 'cinder'] },
    { id: 'deep', name: 'Deep Proofread', sub: 'The full company, twice over',
      price: 99, crest: 'crestDeep',
      blurb: 'Everything above read twice, plus whole-book continuity and gentle style questions, with an AI judge checking every change.',
      party: ['pip', 'bram', 'maple', 'cinder', 'sage', 'lark'] },
    { id: 'campaign', name: 'The Grand Campaign', sub: "Galley's undivided attention",
      price: null, priceLabel: 'from $250', bespoke: true, crest: 'crestCampaign',
      blurb: 'Several full expeditions, planned and re-planned by Galley herself between passes. For the book that deserves a siege. Send a raven for a quote.',
      party: ['pip', 'bram', 'maple', 'cinder', 'sage', 'lark'] }
  ];

  var PALETTE_TINTS = {
    ember: '', rose: 'tint-rose', rain: 'tint-rain', honey: 'tint-honey',
    void: 'tint-void', neon: 'tint-neon', verdigris: 'tint-verdigris',
    bone: 'tint-bone', gold: 'tint-gold', slate: 'tint-slate',
    rust: 'tint-rust', frost: 'tint-frost'
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function tierPrice(tier, band) {
    if (tier.price === null) return tier.priceLabel;
    if (tier.price === 0) return 'Free';
    return '$' + Math.round(tier.price * (band || 1));
  }

  /* Galley speaks — instantly. The typewriter effect read as the page
     rewriting itself, so her lines simply appear, finished. */
  function galleySay(el, lines, done) {
    if (el.getAttribute('aria-live') !== 'polite') el.setAttribute('aria-live', 'polite');
    el.innerHTML = Array.isArray(lines) ? lines.join(' ') : lines;
    if (done) done();
  }

  function tintSweep() {
    if (typeof matchMedia !== 'undefined' &&
        matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    var el = document.createElement('div');
    el.className = 'tint-sweep';
    el.setAttribute('aria-hidden', 'true');
    document.body.appendChild(el);
    var kill = function () { if (el.parentNode) el.parentNode.removeChild(el); };
    el.addEventListener('animationend', kill);
    setTimeout(kill, 1400);
  }

  function applyTint(palette) {
    var cls = PALETTE_TINTS[palette] || '';
    document.body.className = document.body.className
      .split(/\s+/).filter(function (c) { return c.indexOf('tint-') !== 0; })
      .concat(cls ? [cls] : []).join(' ').trim();
    tintSweep();   // the payoff: a wash of the book's colour sweeps the page
  }

  var ROMAN = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII'];

  /* A tier plate for the storefront pages (home, pricing) — crest on top,
     printer's-tariff styling from CSS. `opts.typical` appends the anchor note
     to priced tiers. Links to /quote. */
  function tierPlate(t, band, opts) {
    opts = opts || {};
    var price = tierPrice(t, band || 1);
    return '<a class="tier" href="/quote">' +
      (t.recommended ? '<span class="flag">Most hired</span>' : '') +
      (t.crest ? '<div class="tcrest">' + artFigure(t.crest) + '</div>' : '') +
      '<div class="tname">' + esc(t.name) + '</div>' +
      '<div class="tsub">' + esc(t.sub) + '</div>' +
      '<div class="tblurb">' + esc(t.blurb) + '</div>' +
      '<div class="tprice">' + price +
      (opts.typical && t.price ? '<small> · typical novel</small>' : '') +
      '</div></a>';
  }

  /* A chapter-style gallery entry for the party page: large portrait, a
     chapter number + name, the plain sentence, and the honest machine name in
     an illuminated sidebar. */
  function memberChapter(m, i) {
    return '<article class="chapter">' +
      '<div class="portrait">' + artFigure(m.id) + '</div>' +
      '<div class="chapter-body">' +
        '<div class="chapter-no">Chapter ' + (ROMAN[i] || (i + 1)) + '</div>' +
        '<h3 class="chapter-name">' + esc(m.name) + '<small>' + esc(m.role) + '</small></h3>' +
        '<p class="chapter-say">' + esc(m.plain) + '</p>' +
      '</div>' +
      '<aside class="chapter-lane">' +
        '<div class="label">under the costume</div>' +
        '<div class="mono">' + esc(m.lane) + '</div>' +
      '</aside></article>';
  }

  function memberCard(m, skinAdv) {
    var s = skinAdv && skinAdv[m.id];
    var alias = s ? s.alias : m.name;
    var job = s ? s.job : m.plain;
    var tag = m.role + (alias !== m.name ? ' · always ' + m.name : '');
    return '<div class="member">' +
      '<div class="sigil"' + (s && s.look ? ' title="' + esc(s.look) + '"' : '') + '>' + artFigure(m.id) + '</div>' +
      '<div><div class="mname">' + esc(alias) + '<small>' + esc(tag) + '</small></div>' +
      '<div class="mjob">' + esc(job) + '</div>' +
      '<div class="mlane">lane: ' + m.lane + '</div></div></div>';
  }

  /* ---- shared chrome, rendered from here so the pages stop drifting ----
     Each page sets <body data-page="home|party|pricing|quote|quote-active">.
     The header/footer inject on load; opt out with data-sc-chrome="off". */
  var ORNAMENT = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1l2.2 4.8L15 8l-4.8 2.2L8 15l-2.2-4.8L1 8l4.8-2.2z"/></svg>';

  function headerHTML(page) {
    var onQuote = page === 'quote' || page === 'quote-active';
    var goLabel = onQuote ? 'Your quote' : 'Bring me your book';
    return '<header class="top">' +
      '<a class="wordmark" href="/">Spell <span class="amp">&amp;</span> Check</a>' +
      '<nav class="nav" aria-label="Primary">' +
        '<a href="/party"' + (page === 'party' ? ' class="now"' : '') + '>Meet the party</a>' +
        '<a href="/pricing"' + (page === 'pricing' ? ' class="now"' : '') + '>Pricing</a>' +
        '<a class="go' + (onQuote ? ' now' : '') + '" href="/quote"' +
          (onQuote ? ' aria-current="page"' : '') + '>' + goLabel + '</a>' +
      '</nav></header>';
  }

  function footerHTML() {
    return '<footer><div class="colophon">' +
      '<span class="mark">Spell &amp; Check</span>' +
      '<a href="/party">Meet the party</a>' +
      '<a href="/pricing">Pricing</a>' +
      '<a href="/quote">Bring me your book</a>' +
      '<span class="right">AI does the reading. You keep the pen.</span>' +
      '</div></footer>';
  }

  function divider() {
    return '<div class="divider" aria-hidden="true"><span class="di">' + ORNAMENT + '</span></div>';
  }

  function mountChrome() {
    var body = document.body;
    if (!body || body.dataset.scChrome === 'off') return;
    var page = body.dataset.page || '';
    var wrap = document.querySelector('.wrap') || body;
    var hdr = wrap.querySelector('[data-sc-header]');
    if (hdr) hdr.outerHTML = headerHTML(page);
    else wrap.insertAdjacentHTML('afterbegin', headerHTML(page));
    var ftr = wrap.querySelector('[data-sc-footer]');
    if (ftr) ftr.outerHTML = footerHTML();
    else wrap.insertAdjacentHTML('beforeend', footerHTML());
    // Skip link + a focus target on the first content block after the header.
    if (!document.querySelector('a.skip')) {
      var skip = document.createElement('a');
      skip.className = 'skip'; skip.href = '#main'; skip.textContent = 'Skip to content';
      body.insertBefore(skip, body.firstChild);
    }
    var header = wrap.querySelector('header.top');
    var main = header && header.nextElementSibling;
    if (main && !main.id) { main.id = 'main'; main.setAttribute('tabindex', '-1'); }
  }

  function joinWaitlist(email) {
    return fetch('/api/quest/waitlist', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email })
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (body) {
        if (!resp.ok) throw new Error(body.detail || 'The raven got lost. Try again?');
        return body;
      });
    });
  }

  global.SC = { MEMBERS: MEMBERS, TIERS: TIERS, svg: svg, esc: esc,
                tierPrice: tierPrice, galleySay: galleySay, applyTint: applyTint,
                memberCard: memberCard, joinWaitlist: joinWaitlist,
                headerHTML: headerHTML, footerHTML: footerHTML,
                divider: divider, mountChrome: mountChrome,
                ART: ART_PATHS, injectArt: injectArt, artFigure: artFigure,
                tierPlate: tierPlate, memberChapter: memberChapter };

  /* ---- motion: scroll reveals + gentle hero parallax --------------------
     Opt-in and progressive: <html> gets .js-motion so the reveal hidden-state
     only exists when JS runs; a no-JS page stays fully visible. All movement is
     transform/opacity, and the CSS honours prefers-reduced-motion. */
  var revealScheduled = false;
  function vh() { return window.innerHeight || document.documentElement.clientHeight || 0; }
  function checkReveals() {
    revealScheduled = false;
    var line = vh() * 0.9, left = 0;
    document.querySelectorAll('[data-reveal]:not(.in)').forEach(function (el) {
      // top < line catches both entering AND already-scrolled-past elements
      // (top < 0), so anchor/keyboard jumps never strand a hidden block.
      if (el.getBoundingClientRect().top < line) el.classList.add('in');
      else left++;
    });
    if (left === 0) window.removeEventListener('scroll', scheduleReveals);
  }
  function scheduleReveals() {
    if (revealScheduled) return;
    revealScheduled = true;
    requestAnimationFrame(checkReveals);
  }
  function tagReveal(el, delay) {
    if (el.hasAttribute('data-reveal')) return;
    // Leave anything already on-screen untouched — no flash-then-hide; only
    // content below the fold gets the reveal-on-scroll treatment.
    if (el.getBoundingClientRect().top < vh() * 0.95) return;
    el.setAttribute('data-reveal', '');
    if (delay) el.style.transitionDelay = delay + 'ms';
  }
  function initReveals() {
    // Staggered groups.
    ['.members', '.tiers', '.gallery', '.journey'].forEach(function (sel) {
      document.querySelectorAll(sel).forEach(function (p) {
        Array.prototype.forEach.call(p.children, function (c, i) {
          tagReveal(c, Math.min(i * 70, 420));
        });
      });
    });
    // Standalone blocks (skip anything already inside a staggered group).
    document.querySelectorAll('h2, .lede, .plaque, .card, .divider, .hero-cta').forEach(function (el) {
      if (el.closest('.members, .tiers, .gallery, .journey')) return;
      tagReveal(el, 0);
    });
    window.addEventListener('scroll', scheduleReveals, { passive: true });
    checkReveals();
  }
  function initParallax() {
    var host = document.querySelector('.hero-art');
    if (!host || typeof matchMedia === 'undefined' ||
        matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    var ticking = false;
    window.addEventListener('scroll', function () {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(function () {
        host.style.transform = 'translateY(' + (window.scrollY * 0.05) + 'px)';
        ticking = false;
      });
    }, { passive: true });
  }
  function initMotion() {
    document.documentElement.classList.add('js-motion');
    initReveals();
    initParallax();
    // Late dynamic content (members/tiers rendered by page scripts) — catch it.
    requestAnimationFrame(initReveals);
  }

  function boot() { mountChrome(); injectArt(document); initMotion(); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window);
