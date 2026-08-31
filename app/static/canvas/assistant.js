/* The AI box (spec §6): the art director, docked.

   It sees what we see — every turn carries a downscaled composite of the whole
   cover, not the letterboxed viewport, because "move the title off her face"
   only means something if it can look at her face. The server answers with a
   whole doc rather than the ops it applied, so the swap is recorded as a
   single undo entry and reverting it travels the wire as ops like every other
   edit (see ops.js replaceDoc). */

import { el } from './panels.js';
import { postJSON } from './api.js';
import { renderMarkdown } from './md.js';

const MODE_STORAGE = 'sc-canvas-mode';
const MODEL_STORAGE = 'sc-canvas-model';
const MAX_TURNS = 20;

/* Opus 5 is the owner-chosen default driver; the rest are there because the
   right brain for a cover conversation is a taste call, not a constant. */
const MODELS = [
  ['claude-opus-5', 'Opus 5'],
  ['claude-sonnet-5', 'Sonnet 5'],
  ['claude-fable-5', 'Fable 5'],
  ['claude-haiku-4-5-20251001', 'Haiku 4.5'],
];

export function buildAssistant(ctx) {
  let mode = 'act';
  try { mode = localStorage.getItem(MODE_STORAGE) || 'act'; } catch { /* private mode */ }
  let model = MODELS[0][0];
  try {
    const saved = localStorage.getItem(MODEL_STORAGE);
    if (saved && MODELS.some(([id]) => id === saved)) model = saved;
  } catch { /* private mode */ }
  const messages = [];            // {role, content} — what the server is sent
  let busy = false;

  const log = el('div', { class: 'chatlog' });
  const input = el('textarea', {
    rows: 2, placeholder: 'Move the title down and left, off her face…',
    'aria-label': 'Message the art director',
  });
  const sendBtn = el('button', { class: 'btn primary', type: 'button', text: 'Send', onclick: () => send() });

  const planBtn = el('button', { class: 'btn', type: 'button', text: 'Plan', onclick: () => setMode('plan') });
  const actBtn = el('button', { class: 'btn', type: 'button', text: 'Act', onclick: () => setMode('act') });
  const modes = el('div', { class: 'modepick' }, [planBtn, actBtn]);

  const modelPick = el('select', {
    class: 'modelpick', 'aria-label': 'Art director model',
    onchange: () => {
      model = modelPick.value;
      try { localStorage.setItem(MODEL_STORAGE, model); } catch { /* private mode */ }
    },
  }, MODELS.map(([id, label]) => {
    const opt = el('option', { value: id, text: label });
    if (id === model) opt.selected = true;
    return opt;
  }));

  const head = el('div', { class: 'rail-head' }, [el('span', { text: 'Art director' }), modelPick, modes]);
  const form = el('div', { class: 'chatform' }, [input, sendBtn]);
  const grip = el('div', {
    class: 'grip', role: 'separator', 'aria-orientation': 'horizontal',
    'aria-label': 'Resize the art director panel', tabindex: 0,
  });
  const root = el('div', { class: 'assistant' }, [grip, head, log, form]);

  /* The split between the properties panel above and this one. Stored as a
     percentage so it survives a resized window, and clamped so neither half
     can be dragged away entirely. */
  const SPLIT_STORAGE = 'sc-canvas-split';
  const MIN_PCT = 18, MAX_PCT = 82;
  const setSplit = (pct, save) => {
    const v = Math.max(MIN_PCT, Math.min(MAX_PCT, pct));
    root.style.height = v + '%';
    if (save) { try { localStorage.setItem(SPLIT_STORAGE, String(Math.round(v))); } catch { /* private mode */ } }
  };
  try {
    const saved = Number(localStorage.getItem(SPLIT_STORAGE));
    if (saved) setSplit(saved, false);
  } catch { /* private mode */ }

  grip.addEventListener('pointerdown', (e) => {
    const rail = root.parentElement;
    if (!rail) return;
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    root.classList.add('is-sizing');
    const move = (ev) => {
      const box = rail.getBoundingClientRect();
      if (box.height) setSplit(((box.bottom - ev.clientY) / box.height) * 100, false);
    };
    const up = () => {
      grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', up);
      root.classList.remove('is-sizing');
      setSplit(parseFloat(root.style.height) || 44, true);
    };
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', up);
  });
  grip.addEventListener('keydown', (e) => {
    const step = e.key === 'ArrowUp' ? 4 : e.key === 'ArrowDown' ? -4 : 0;
    if (!step) return;
    e.preventDefault();
    e.stopPropagation();                       // the canvas owns arrows otherwise
    setSplit((parseFloat(root.style.height) || 44) + step, true);
  });

  function setMode(next) {
    mode = next;
    try { localStorage.setItem(MODE_STORAGE, next); } catch { /* private mode */ }
    planBtn.classList.toggle('on', mode === 'plan');
    actBtn.classList.toggle('on', mode === 'act');
    input.placeholder = mode === 'plan'
      ? 'What would you change about this cover?'
      : 'Move the title down and left, off her face…';
  }
  setMode(mode);

  /* What the model wrote is Markdown and renders as Markdown (md.js builds
     text nodes, never innerHTML). What a PERSON typed, and what the server
     said in its own voice, stay literal: their asterisks are asterisks. */
  function bubble(who, text, cls = '') {
    const markdown = who === 'assistant' && !cls;
    const body = markdown
      ? renderMarkdown(text, el('div', { class: 'body md' }))
      : el('div', { class: 'body', text });
    const node = el('div', { class: 'msg ' + who + (cls ? ' ' + cls : '') },
      [el('span', { class: 'who', text: who === 'user' ? 'You' : 'Director' }), body]);
    log.appendChild(node);
    log.scrollTop = log.scrollHeight;
    return { node, body };
  }

  function typing() {
    const node = el('div', { class: 'msg assistant' }, [
      el('span', { class: 'who', text: 'Director' }),
      el('span', { class: 'wdots' }, [el('i'), el('i'), el('i')]),
    ]);
    log.appendChild(node);
    log.scrollTop = log.scrollHeight;
    return node;
  }

  async function send() {
    const text = input.value.trim();
    if (!text || busy) return;
    input.value = '';
    messages.push({ role: 'user', content: text });
    bubble('user', text);
    busy = true;
    sendBtn.disabled = true;
    const wait = typing();
    try {
      const res = await postJSON(`/api/canvas/${encodeURIComponent(ctx.jobId)}/chat`, {
        messages: messages.slice(-MAX_TURNS),
        mode,
        model,
        snapshot_b64: ctx.engine.snapshotBase64(700),
      });
      wait.remove();
      const reply = res.reply || '(no reply)';
      messages.push({ role: 'assistant', content: reply });
      const { node } = bubble('assistant', reply);
      if (res.doc) ctx.store.replaceDoc(res.doc);
      const applied = (res.ops_applied || []).length;
      if (applied) {
        node.appendChild(el('div', { class: 'applied' }, [
          `${applied} edit${applied === 1 ? '' : 's'} applied`,
          el('button', { type: 'button', text: 'undo', onclick: () => ctx.undo() }),
        ]));
      }
      if (typeof res.cost_usd === 'number') {
        ctx.store.doc.cost_usd = res.cost_usd;
        ctx.refreshChrome();
      }
      log.scrollTop = log.scrollHeight;
    } catch (err) {
      wait.remove();
      // Errors belong in the transcript — an alert would lose the thread.
      bubble('assistant', err.message || 'That turn failed.', 'err');
    } finally {
      busy = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    e.stopPropagation();       // the canvas owns arrows/undo, not this box
  });

  /* A Director line that no turn produced — today, the rebalance verb's
     measurement. It lands in the transcript because that is where reasoning
     about this cover lives, and a measurement nobody can find later is a
     measurement that may as well not have been taken.

     Deliberately NOT pushed into `messages`: the model did not say it, and
     filing a server sentence as an assistant turn would put words in its
     mouth and then feed them back as if it had meant them. */
  function note(text) {
    bubble('assistant', text, 'note');
    log.scrollTop = log.scrollHeight;
  }

  bubble('assistant', 'Opened. Ask for a change, or switch to Plan and I\'ll critique first.');

  return { root, send, note, get mode() { return mode; } };
}
