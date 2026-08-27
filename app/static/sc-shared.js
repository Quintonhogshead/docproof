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

  // The permanent party. `plain` is the no-prior-knowledge sentence; `lane`
  // is the honest machine name underneath.
  var MEMBERS = [
    { id: 'pip', name: 'Pip', role: 'Scout', icon: 'dagger',
      plain: 'Reads every page hunting typos and misspelled words.',
      lane: 'spelling_sweep · AI ensemble' },
    { id: 'bram', name: 'Bram', role: 'Knight', icon: 'shield',
      plain: 'Checks grammar and punctuation, sentence by sentence.',
      lane: 'grammar_watch · rules + AI judge' },
    { id: 'maple', name: 'Maple', role: 'Archivist', icon: 'book',
      plain: 'Makes sure names and spellings stay identical from page 12 to page 312.',
      lane: 'consistency_scan · deterministic' },
    { id: 'cinder', name: 'Cinder', role: 'Blacksmith', icon: 'hammer',
      plain: 'Repairs sentences that came out broken or garbled.',
      lane: 'repair_channel · density-triggered' },
    { id: 'sage', name: 'Sage', role: 'Wizard', icon: 'staff',
      plain: 'Remembers the whole book — flags it if the timeline or an eye color quietly changes.',
      lane: 'continuity · whole-book pass' },
    { id: 'lark', name: 'Lark', role: 'Bard', icon: 'lute',
      plain: 'Suggests where a line could read better — always as a question, never a rewrite.',
      lane: 'smoothing · query-only' }
  ];

  // The five rungs. Prices anchor a 60–120k-word manuscript; the band from the
  // quote scales the paid fixed tiers. `party` names who rides.
  var TIERS = [
    { id: 'spellcheck', name: 'Spellcheck', sub: "Galley's lantern, alone",
      price: 0, priceLabel: 'Free',
      blurb: 'The mechanical pass: dictionary, house sweeps, and the consistency scans. No AI reads your book. Results by email.',
      party: [] },
    { id: 'typohunt', name: 'Typo Hunt', sub: 'The Scout rides alone',
      price: 9,
      blurb: 'Pip reads every page for typos and misspellings, plus everything in Spellcheck.',
      party: ['pip'] },
    { id: 'proofread', name: 'Proofread', sub: 'The party of four',
      price: 29, recommended: true,
      blurb: 'Typos, grammar, consistency, and broken sentences — the full sweep, delivered as tracked changes.',
      party: ['pip', 'bram', 'maple', 'cinder'] },
    { id: 'deep', name: 'Deep Proofread', sub: 'The full company, twice over',
      price: 99,
      blurb: 'Everything above read twice, plus whole-book continuity and gentle style questions, with an AI judge checking every change.',
      party: ['pip', 'bram', 'maple', 'cinder', 'sage', 'lark'] },
    { id: 'campaign', name: 'The Grand Campaign', sub: "Galley's undivided attention",
      price: null, priceLabel: 'from $250', bespoke: true,
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

  /* Galley types her line(s). Reduced motion gets them instantly. */
  function galleySay(el, lines, done) {
    var text = Array.isArray(lines) ? lines.join(' ') : lines;
    var reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) { el.innerHTML = text; if (done) done(); return; }
    var plain = text.replace(/<[^>]*>/g, '');
    el.innerHTML = '<span class="typed"></span><span class="caret"></span>';
    var typed = el.querySelector('.typed'), i = 0;
    (function step() {
      if (i <= plain.length) {
        typed.textContent = plain.slice(0, i++);
        setTimeout(step, 20);
      } else {
        el.innerHTML = text;  // swap in the marked-up version (<em> accents)
        if (done) done();
      }
    })();
  }

  function applyTint(palette) {
    var cls = PALETTE_TINTS[palette] || '';
    document.body.className = document.body.className
      .split(/\s+/).filter(function (c) { return c.indexOf('tint-') !== 0; })
      .concat(cls ? [cls] : []).join(' ').trim();
  }

  function memberCard(m, skinAdv) {
    var s = skinAdv && skinAdv[m.id];
    var alias = s ? s.alias : m.name;
    var job = s ? s.job : m.plain;
    var tag = m.role + (alias !== m.name ? ' · always ' + m.name : '');
    return '<div class="member">' +
      '<div class="sigil"' + (s && s.look ? ' title="' + esc(s.look) + '"' : '') + '>' + svg(m.icon) + '</div>' +
      '<div><div class="mname">' + esc(alias) + '<small>' + esc(tag) + '</small></div>' +
      '<div class="mjob">' + esc(job) + '</div>' +
      '<div class="mlane">lane: ' + m.lane + '</div></div></div>';
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
                memberCard: memberCard, joinWaitlist: joinWaitlist };
})(window);
