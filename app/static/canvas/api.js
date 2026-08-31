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

async function raw(path, opts = {}) {
  const headers = Object.assign({}, opts.headers);
  const key = getKey();
  if (key) headers['X-Cover-Key'] = key;
  let resp;
  try {
    resp = await fetch(path, {
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
