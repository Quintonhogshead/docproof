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
    // Commissioned cut-paper art, background removed — every cutout sits
    // directly on the page's paper (transparent webp). Busts are square;
    // poses/figures ~2:3; scenes, steps, and the group shot are full-bleed.
    galley: 'galley.webp',
    pip: 'pip.webp', bram: 'bram.webp', maple: 'maple.webp',
    cinder: 'cinder.webp', sage: 'sage.webp', lark: 'lark.webp',
    // Galley's poses — each now has its own commissioned cutout.
    galleyHero: 'galley-hero.webp', galleyPresenting: 'galley-presenting.webp',
    galleyReading: 'galley-reading.webp', galleyBeckoning: 'galley-beckoning.webp',
    galleyWaving: 'galley-waving.webp', galleyLost: 'galley-lost.webp',
    // Party full-body puppets (the bench).
    figPip: 'fig-pip.webp', figBram: 'fig-bram.webp', figMaple: 'fig-maple.webp',
    figCinder: 'fig-cinder.webp', figSage: 'fig-sage.webp', figLark: 'fig-lark.webp',
    // Diorama scenes, how-it-works spots, the whole-party group shot, odds/ends.
    sceneHills: 'scene-hills.webp', sceneCamp: 'scene-camp.webp', sceneGrass: 'scene-grass.webp',
    stepManuscript: 'step-manuscript.webp', stepReading: 'step-reading.webp', stepTracked: 'step-tracked.webp',
    party: 'party.webp',
    raven: 'raven.webp', lantern: 'lantern.webp',
    crestSpellcheck: 'crest-spellcheck.webp', crestTypohunt: 'crest-typohunt.webp',
    crestProofread: 'crest-proofread.webp', crestDeep: 'crest-deep.webp',
    crestCampaign: 'crest-campaign.webp',
    ornamentDivider: 'ornament-divider.svg', ornamentCorner: 'ornament-corner.svg',
    dropcap: 'dropcap-frame.svg', favicon: 'favicon.svg', og: 'og.svg'
  };
  // member id -> full-body puppet key
  function figKey(id) { return 'fig' + id.charAt(0).toUpperCase() + id.slice(1); }
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
      lane: 'spelling_sweep · AI ensemble',
      example: { from: 'teh', to: 'the', why: 'a plain typo' },
      greet: "I'll take the typos." },
    { id: 'bram', name: 'Bram', role: 'Grammar', icon: 'shield',
      plain: 'Checks grammar and punctuation, sentence by sentence.',
      lane: 'grammar_watch · rules + AI judge',
      example: { from: '"Run" she said.', to: '"Run," she said.', why: 'dialogue punctuation' },
      greet: "Grammar's mine." },
    { id: 'maple', name: 'Maple', role: 'Consistency', icon: 'book',
      plain: 'Makes sure names and spellings stay identical from page 12 to page 312.',
      lane: 'consistency_scan · deterministic',
      example: { from: 'Sara / Sarah', to: 'pick one', why: 'one name, two spellings — flagged' },
      greet: "I'll keep the names straight." },
    { id: 'cinder', name: 'Cinder', role: 'Repairs', icon: 'hammer',
      plain: 'Repairs sentences that came out broken or garbled.',
      lane: 'repair_channel · density-triggered',
      example: { from: 'He picked up the and left.', to: 'He picked up the bag and left.', why: 'missing word' },
      greet: "I'll mend the broken lines." },
    { id: 'sage', name: 'Sage', role: 'Continuity', icon: 'staff',
      plain: 'Remembers the whole book — flags it if the timeline or an eye color quietly changes.',
      lane: 'continuity · whole-book pass',
      example: { from: 'gray eyes (ch. 2)', to: 'green eyes (ch. 19)', why: 'flagged as a question, never changed' },
      greet: "I'll hold the whole book in mind." },
    { id: 'lark', name: 'Lark', role: 'Style', icon: 'lute',
      plain: 'Suggests where a line could read better — always as a question, never a rewrite.',
      lane: 'smoothing · query-only',
      example: { from: '"her heart hammered" ×7', to: 'a gentle comment', why: 'a question, not an edit' },
      greet: "I'll whisper where it sings." }
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

  function applyTint(palette, quiet) {
    var cls = PALETTE_TINTS[palette] || '';
    document.body.className = document.body.className
      .split(/\s+/).filter(function (c) { return c.indexOf('tint-') !== 0; })
      .concat(cls ? [cls] : []).join(' ').trim();
    if (!quiet) tintSweep();   // the payoff: the book's colour sweeps the page
  }

  /* ---- the book on Galley's desk -------------------------------------
     The quote (skin + band + word count) rides sessionStorage, so wandering
     to the party or pricing page — or back — never loses the dropped book.
     One tab, one reading session; closing the browser clears the desk. */
  var QUOTE_KEY = 'sc-quote';
  function saveQuote(state) {
    try { sessionStorage.setItem(QUOTE_KEY, JSON.stringify(state)); }
    catch (e) { /* private mode: the quote simply doesn't persist */ }
  }
  function loadQuote() {
    try {
      var s = JSON.parse(sessionStorage.getItem(QUOTE_KEY) || 'null');
      return (s && s.skin) ? s : null;
    } catch (e) { return null; }
  }
  function clearQuote() {
    try { sessionStorage.removeItem(QUOTE_KEY); } catch (e) { /* ditto */ }
  }

  var ROMAN = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII'];

  function findMember(id) {
    for (var i = 0; i < MEMBERS.length; i++) if (MEMBERS[i].id === id) return MEMBERS[i];
    return null;
  }

  /* A real before→after catch, rendered as a torn scrap of LIVE text (never a
     generated image — the words must stay crisp and searchable). */
  function correctionScrap(m) {
    if (!m || !m.example) return '';
    var e = m.example;
    return '<div class="correction"><span class="from">' + esc(e.from) + '</span>' +
      '<span class="arrow">→</span><span class="to">' + esc(e.to) + '</span>' +
      (e.why ? '<span class="why">' + esc(e.why) + '</span>' : '') + '</div>';
  }

  /* The row of tiny member busts riding on a tier plate (who actually rides). */
  function riderBusts(party) {
    if (!party || !party.length) return '<span class="alone">Galley’s lantern, alone</span>';
    return party.map(function (id) {
      return '<span class="bust" title="' + esc((findMember(id) || {}).name || '') + '">' +
        artFigure(id) + '</span>';
    }).join('');
  }

  /* A tier plate for the storefront pages (home, pricing) — crest stamp, a row
     of the busts who ride, and the price. `opts.typical` appends the anchor
     note to priced tiers. Links to /quote (opts.href overrides). */
  function tierPlate(t, band, opts) {
    opts = opts || {};
    var price = tierPrice(t, band || 1);
    var href = opts.href || '/quote';
    return '<a class="tier" href="' + href + '">' +
      (t.recommended ? '<span class="flag">Most hired</span>' : '') +
      (t.crest ? '<div class="tcrest">' + artFigure(t.crest) + '</div>' : '') +
      '<div class="tname">' + esc(t.name) + '</div>' +
      '<div class="tsub">' + esc(t.sub) + '</div>' +
      '<div class="tblurb">' + esc(t.blurb) + '</div>' +
      '<div class="triders" aria-hidden="true">' + riderBusts(t.party) + '</div>' +
      '<div class="tprice">' + price +
      (opts.typical && t.price ? '<small> · typical novel</small>' : '') +
      '</div></a>';
  }

  /* A program-booklet entry for the party page: large portrait sheet, a
     chapter number + name, the plain sentence, a live correction scrap, and
     the honest machine name in a pasted sidebar. */
  function memberChapter(m, i) {
    return '<article class="chapter">' +
      '<div class="portrait">' + artFigure(m.id) + '</div>' +
      '<div class="chapter-body">' +
        '<div class="chapter-no">Chapter ' + (ROMAN[i] || (i + 1)) + '</div>' +
        '<h3 class="chapter-name">' + esc(m.name) + '<small>' + esc(m.role) + '</small></h3>' +
        '<p class="chapter-say">' + esc(m.plain) + '</p>' +
        correctionScrap(m) +
      '</div>' +
      '<aside class="chapter-lane">' +
        '<div class="label">under the costume</div>' +
        '<div class="mono">' + esc(m.lane) + '</div>' +
      '</aside></article>';
  }

  /* A stacked member sheet: bust + name + one plain sentence + a live
     correction scrap + the honest lane. Used on the homepage party act and on
     the quote page's "who rides" list. */
  function memberCard(m, skinAdv) {
    // Names are permanent: the skin tailors the job line, never the name.
    var s = skinAdv && skinAdv[m.id];
    var job = s ? s.job : m.plain;
    return '<div class="member rich">' +
      '<div class="top"><div class="sigil"' + (s && s.look ? ' title="' + esc(s.look) + '"' : '') + '>' +
        artFigure(m.id) + '</div>' +
      '<div><div class="mname">' + esc(m.name) + '<small>' + esc(m.role) + '</small></div>' +
      '<div class="mjob">' + esc(job) + '</div></div></div>' +
      correctionScrap(m) +
      '<div class="mlane">lane: ' + m.lane + '</div></div>';
  }

  /* Galley as a lantern-lit paper puppet in a given pose. */
  var POSES = {
    hero: 'galleyHero', presenting: 'galleyPresenting', reading: 'galleyReading',
    beckoning: 'galleyBeckoning', waving: 'galleyWaving', lost: 'galleyLost'
  };
  function galleyFig(pose, cls) {
    var key = POSES[pose] || pose;
    return '<div class="galley-fig' + (cls ? ' ' + cls : '') + '">' +
      '<span class="glow" aria-hidden="true"></span>' + artFigure(key) + '</div>';
  }

  function prefersReduce() {
    return typeof matchMedia !== 'undefined' &&
      matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  /* ---- the cover cast: join/leave choreography -----------------------------
     hostEl is the `.cover-cast` layer (its Galley anchor puppet stays put).
     .set(partyIds) diffs against what's shown: departures peel off the cover,
     arrivals are pasted into their spot in the scene and drop a greeting
     scrap. Under reduced motion everything swaps instantly but the greeting
     still appears. */
  function makeBench(hostEl) {
    var shown = [];
    function makePuppet(id) {
      var m = findMember(id) || { name: '', greet: '' };
      var el = document.createElement('div');
      el.className = 'puppet';
      el.setAttribute('data-member', id);
      el.innerHTML =
        '<div class="greet"><b>' + esc(m.name) + '</b> ' + esc(m.greet) + '</div>' +
        '<div class="fig">' + artFigure(figKey(id)) + '</div>';
      return el;
    }
    function set(party, quiet) {
      party = party || [];
      var reduce = prefersReduce();
      // departures
      shown.forEach(function (id) {
        if (party.indexOf(id) >= 0) return;
        var el = hostEl.querySelector('.puppet[data-member="' + id + '"]');
        if (!el) return;
        if (reduce) { el.parentNode && el.remove(); return; }
        el.classList.add('leaving');
        var gone = function () { if (el.parentNode) el.remove(); };
        el.addEventListener('animationend', gone, { once: true });
        setTimeout(gone, 700);
      });
      // arrivals
      var joinIndex = 0;
      party.forEach(function (id) {
        if (shown.indexOf(id) >= 0) return;
        var el = makePuppet(id);
        hostEl.appendChild(el);
        var greet = el.querySelector('.greet');
        var i = joinIndex++;
        if (!reduce) {
          el.classList.add('joining');
          el.style.animationDelay = (i * 90) + 'ms';
          var landed = function () {
            el.classList.remove('joining');
            el.style.animationDelay = '';
          };
          el.addEventListener('animationend', landed, { once: true });
          // Animations stall in hidden tabs; never leave a puppet mid-walk.
          setTimeout(landed, 800 + i * 90);
        }
        if (!quiet) {
          // A roll call: one greeting scrap at a time, so a full company
          // joining at once never piles its hellos into a heap.
          setTimeout(function () { greet.classList.add('show'); }, 420 + i * 700);
          setTimeout(function () { greet.classList.remove('show'); }, 2020 + i * 700);
        }
      });
      shown = party.slice();
    }
    return { set: set };
  }

  /* ---- shared chrome, rendered from here so the pages stop drifting ----
     Each page sets <body data-page="home|party|pricing|quote|quote-active">.
     The header/footer inject on load; opt out with data-sc-chrome="off". */
  var ORNAMENT = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1l2.2 4.8L15 8l-4.8 2.2L8 15l-2.2-4.8L1 8l4.8-2.2z"/></svg>';

  function headerHTML(page) {
    var onQuote = page === 'quote' || page === 'quote-active';
    // Once a book is on the desk, every page's CTA points at the open quote.
    var goLabel = (onQuote || loadQuote()) ? 'Your quote' : 'Bring me your book';
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
                tierPlate: tierPlate, memberChapter: memberChapter,
                findMember: findMember, figKey: figKey,
                correctionScrap: correctionScrap, riderBusts: riderBusts,
                galleyFig: galleyFig, makeBench: makeBench,
                saveQuote: saveQuote, loadQuote: loadQuote, clearQuote: clearQuote };

  /* ---- motion: scroll reveals + gentle hero parallax --------------------
     Opt-in and progressive: <html> gets .js-motion so the reveal hidden-state
     only exists when JS runs; a no-JS page stays fully visible. All movement is
     transform/opacity, and the CSS honours prefers-reduced-motion. */
  function vh() { return window.innerHeight || document.documentElement.clientHeight || 0; }
  /* One observer reveals blocks however they reach the viewport — scrolling,
     a window resize, layout settling as images land. A scroll-line check
     alone stranded already-in-view sections invisible whenever no scroll
     event ever fired (large windows, resizes, restored positions). */
  var revealIO = null;
  function watchReveals() {
    if (!revealIO && 'IntersectionObserver' in window) {
      revealIO = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (!en.isIntersecting) return;
          en.target.classList.add('in');
          revealIO.unobserve(en.target);
        });
      }, { threshold: 0.05 });
    }
    document.querySelectorAll('[data-reveal]:not(.in)').forEach(function (el) {
      if (revealIO) revealIO.observe(el);
      else el.classList.add('in');   // no observer support: never hide content
    });
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
    ['.members', '.tiers', '.gallery', '.strip'].forEach(function (sel) {
      document.querySelectorAll(sel).forEach(function (p) {
        Array.prototype.forEach.call(p.children, function (c, i) {
          tagReveal(c, Math.min(i * 70, 420));
        });
      });
    });
    // Standalone blocks (skip anything already inside a staggered group).
    document.querySelectorAll('h2, .lede, .plaque, .card, .divider, .hero-cta, .act-cta, .headline-sheet').forEach(function (el) {
      if (el.closest('.members, .tiers, .gallery, .strip')) return;
      tagReveal(el, 0);
    });
    watchReveals();
  }
  function initParallax() {
    if (prefersReduce()) return;
    var els = document.querySelectorAll('[data-parallax]');
    if (!els.length) return;
    var ticking = false;
    window.addEventListener('scroll', function () {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(function () {
        var y = window.scrollY;
        Array.prototype.forEach.call(els, function (el) {
          var sp = parseFloat(el.getAttribute('data-parallax')) || 0.05;
          el.style.transform = 'translateY(' + (y * sp) + 'px)';
        });
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

  function boot() {
    // A book already on the desk keeps its costume on every page — quietly,
    // with no sweep: the reveal already happened when it was dropped.
    var held = loadQuote();
    if (held && held.skin && held.skin.palette) applyTint(held.skin.palette, true);
    mountChrome(); injectArt(document); initMotion();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window);
