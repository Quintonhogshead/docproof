/* One door for every server call, and the toast rail the failures come out of.
   They live together on purpose: the rule this module enforces is "no fetch
   anywhere else, no alert() ever", and the failure surface is half of that.

   Cover Studio's API is key-gated (app/routes/cover.py:_gate → X-Cover-Key),
   and the canvas endpoints hang off the same job store, so every request here
   carries the key from the *same* sessionStorage slot sc-cover.html uses:
   unlock on one page, both pages work. */

const KEY_STORAGE = 'sc-cover-key';

export function getKey() {
  try { return sessionStorage.getItem(KEY_STORAGE) || ''; } catch { return ''; }
}
export function setKey(k) {
  try { sessionStorage.setItem(KEY_STORAGE, k); } catch { /* private mode */ }
}
export function clearKey() {
  try { sessionStorage.removeItem(KEY_STORAGE); } catch { /* private mode */ }
}

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

/* Which CONCEPT of the cover job this page is editing.

   A cover job holds several concepts and each one is a different cover with
   its own editing session on the server (app/routes/canvas.py:_session_path),
   so every /api/canvas call has to say which one it means or the second
   concept you open hands you the first one's document. It is one value for
   the life of the page, so it is appended here — in the one door every
   request already goes through — rather than threaded through a dozen call
   sites that would each be a place to forget it.

   Null means "say nothing", which is what a client with no concept in its
   URL has always done and what the server still answers for. */
let concept = null;

export function setConcept(value) {
  concept = (value === null || value === undefined || value === '')
    ? null : Number(value);
}

export function getConcept() { return concept; }

function withConcept(path) {
  if (concept === null || Number.isNaN(concept)) return path;
  if (!path.startsWith('/api/canvas/')) return path;
  if (/[?&]concept=/.test(path)) return path;      // an explicit one wins
  return path + (path.includes('?') ? '&' : '?') + `concept=${concept}`;
}

async function raw(path, opts = {}) {
  const headers = Object.assign({}, opts.headers);
  const key = getKey();
  if (key) headers['X-Cover-Key'] = key;
  let resp;
  try {
    resp = await fetch(withConcept(path), {
      method: opts.method || 'GET', body: opts.body, headers, cache: 'no-store',
    });
  } catch (e) {
    // A dead uvicorn and a pulled cable look the same from here; say so plainly.
    throw new ApiError(0, 'Could not reach the server — is it still running?');
  }
  if (resp.ok) return resp;
  if (resp.status === 401) clearKey();
  let detail = '';
  try { detail = (await resp.json()).detail || ''; } catch { /* not JSON */ }
  throw new ApiError(resp.status, detail || `Something went wrong (${resp.status}).`);
}

export async function api(path, opts) {
  const resp = await raw(path, opts);
  const text = await resp.text();
  if (!text) return {};
  try { return JSON.parse(text); } catch { return {}; }
}

export function postJSON(path, body) {
  return api(path, {
    method: 'POST', body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' },
  });
}

/* A plate call, answered progressively (app/routes/canvas.py:_plate_answer).

   The server streams NDJSON: `{event:"partial"}` frames as the vendor paints
   them, then one final line — the same payload the plain JSON endpoint
   returns, plus `event:"done"`, or `{event:"error"}`. `onPartial(frame)` gets
   each partial; the finished payload is returned.

   Two things this cannot do the ordinary way. It cannot use EventSource (no
   way to send a POST body or the key header), hence fetch + a body reader.
   And it cannot report failure as a status code: by the time the vendor
   fails, the 200 and its headers are long gone — so an error arrives as the
   last LINE, and is re-thrown here as the ApiError the callers already know
   how to show. A response that simply stops (a dropped connection) is its
   own sentence rather than a silent success. */
export async function postStream(path, body, onPartial) {
  const resp = await raw(path, {
    method: 'POST', body: JSON.stringify({ ...body, stream: true }),
    headers: { 'Content-Type': 'application/json' },
  });
  if (!resp.body || !resp.body.getReader) {
    // No streaming in this browser: the whole body arrives at once, which is
    // exactly the old behaviour. Parse the same lines out of it.
    return finishFrames(splitFrames(await resp.text()).frames, onPartial);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let last = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const split = splitFrames(buffer);
    buffer = split.rest;
    for (const frame of split.frames) {
      if (frame.event === 'partial') onPartial && onPartial(frame);
      else last = frame;
    }
  }
  return finishFrames(last ? [last] : [], onPartial);
}

function splitFrames(text) {
  const parts = text.split('\n');
  const rest = parts.pop();            // whatever is left before the next \n
  const frames = [];
  for (const line of parts) {
    if (!line.trim()) continue;
    try { frames.push(JSON.parse(line)); } catch { /* a half line; skip */ }
  }
  return { frames, rest };
}

function finishFrames(frames, onPartial) {
  let last = null;
  for (const frame of frames) {
    if (frame.event === 'partial') onPartial && onPartial(frame);
    else last = frame;
  }
  if (!last) {
    throw new ApiError(0, 'The server stopped answering before the plate was '
      + 'finished — it may still be rendering; reload to see.');
  }
  if (last.event === 'error') throw new ApiError(502, last.detail || 'That plate call failed.');
  return last;
}

/* An image the browser will draw: fetched as a blob so the key header rides
   along (a bare <img src> can't carry one), then handed over as an object URL.

   A plate reference IS a relative path ("assets/c0_focal.png") and the route
   takes it as {name:path}, so the separators must stay separators — encode
   each segment, never the whole string. */
export async function fileObjectURL(jobId, name) {
  const rel = String(name).split('/').map(encodeURIComponent).join('/');
  const resp = await raw(`/api/canvas/${encodeURIComponent(jobId)}/file/${rel}`);
  return URL.createObjectURL(await resp.blob());
}

/* ------------------------------------------------------------------ toasts */
let toastHost = null;

export function toast(message, kind = 'info', ms = 5200) {
  if (!toastHost) {
    toastHost = document.createElement('div');
    toastHost.className = 'toasts';
    document.body.appendChild(toastHost);
  }
  const el = document.createElement('div');
  el.className = 'toast' + (kind === 'info' ? '' : ' ' + kind);
  el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  el.textContent = message;
  toastHost.appendChild(el);
  setTimeout(() => el.remove(), ms);
  return el;
}
