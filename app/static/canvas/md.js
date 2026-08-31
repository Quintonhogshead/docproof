/* The director speaks Markdown; the transcript has to read like it.

   A numbered plan is the shape Plan mode is TOLD to answer in, and the model
   writes `**bold**`, backticked layer ids and `- ` bullets the way anything
   writing to a person does. Dropped into a `white-space: pre-wrap` box that
   was all asterisks and backticks — the punctuation of the format leaking
   through as if it were prose.

   So: a deliberately small renderer for the subset that actually shows up in
   this box — headings, bullet and numbered lists (one level of nesting),
   fenced and inline code, bold/italic, blockquotes, rules, and http links.
   Everything else stays literal text, which is the right failure: an
   unrendered `~~strike~~` is legible, a half-parsed one is not.

   SAFETY: nothing here ever assigns innerHTML. Every scrap of model text
   reaches the page as a text node, so a reply containing `<script>` renders
   those characters and does nothing else. That property is why this is
   hand-rolled rather than a vendored library — it has to be auditable in one
   sitting, and it has to work with the wifi off. */

import { el } from './panels.js';

/* Inline spans, innermost-first so `**a `b` c**` keeps the code span inside
   the bold. Order matters: code is claimed before emphasis, or a backticked
   `*` would open one. */
const CODE = /`([^`\n]+)`/;
const BOLD = /(\*\*|__)(?=\S)([\s\S]*?\S)\1/;
const ITALIC = /(^|[\s(])([*_])(?=\S)([^*_]*?\S)\2(?=$|[\s.,;:!?)])/;
const LINK = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/;
const RAW_LINK = /(^|[\s(])(https?:\/\/[^\s)<>]+)/;

/* One inline pass: find whichever construct starts earliest, emit the text
   before it, emit its node, recurse on the rest. */
function inline(text, out) {
  if (!text) return;
  const tries = [
    [CODE, (m) => el('code', { text: m[1] })],
    [BOLD, (m) => withInline('strong', m[2])],
    [LINK, (m) => el('a', {
      href: m[2], target: '_blank', rel: 'noreferrer noopener', text: m[1],
    })],
    [ITALIC, (m) => withInline('em', m[3])],
    [RAW_LINK, (m) => el('a', {
      href: m[2], target: '_blank', rel: 'noreferrer noopener', text: m[2],
    })],
  ];
  let best = null;
  for (const [re, make] of tries) {
    const m = re.exec(text);
    if (!m) continue;
    // ITALIC and RAW_LINK capture a leading space that is not part of the
    // construct; the node starts after it.
    const lead = (re === ITALIC || re === RAW_LINK) ? m[1].length : 0;
    const at = m.index + lead;
    if (!best || at < best.at) {
      best = { at, end: m.index + m[0].length, node: make(m) };
    }
  }
  if (!best) { out.appendChild(document.createTextNode(text)); return; }
  if (best.at) out.appendChild(document.createTextNode(text.slice(0, best.at)));
  out.appendChild(best.node);
  inline(text.slice(best.end), out);
}

function withInline(tag, text) {
  const n = el(tag);
  inline(text, n);
  return n;
}

/* A paragraph's own newlines are meaningful here — the director writes short
   lines and a plan's continuation lines are aligned on purpose. */
function para(tag, lines, cls) {
  const n = el(tag, cls ? { class: cls } : {});
  lines.forEach((line, i) => {
    if (i) n.appendChild(el('br'));
    inline(line, n);
  });
  return n;
}

const BULLET = /^\s{0,3}([-*+])\s+(.*)$/;
const NUMBER = /^\s{0,3}(\d{1,3})[.)]\s+(.*)$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*)$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const FENCE = /^\s{0,3}(```|~~~)(.*)$/;
const INDENT = /^(\s+)/;

function listItemOf(line) {
  const b = BULLET.exec(line);
  if (b) return { ordered: false, text: b[2], indent: (INDENT.exec(line) || ['', ''])[1].length };
  const n = NUMBER.exec(line);
  if (n) return { ordered: true, text: n[2], start: Number(n[1]), indent: (INDENT.exec(line) || ['', ''])[1].length };
  return null;
}

/* Block scan. Lines are consumed by whichever block claims them; anything
   unclaimed is prose. */
export function renderMarkdown(src, host) {
  const root = host || el('div');
  const lines = String(src == null ? '' : src).replace(/\r\n?/g, '\n').split('\n');
  let i = 0;

  const flushProse = (buf) => {
    if (buf.length) root.appendChild(para('p', buf));
    buf.length = 0;
  };
  const prose = [];

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { flushProse(prose); i += 1; continue; }

    const fence = FENCE.exec(line);
    if (fence) {
      flushProse(prose);
      const marker = fence[1];
      const body = [];
      i += 1;
      while (i < lines.length && !lines[i].trimStart().startsWith(marker)) {
        body.push(lines[i]); i += 1;
      }
      if (i < lines.length) i += 1;           // the closing fence
      root.appendChild(el('pre', {}, [el('code', { text: body.join('\n') })]));
      continue;
    }

    if (RULE.test(line)) { flushProse(prose); root.appendChild(el('hr')); i += 1; continue; }

    const head = HEADING.exec(line);
    if (head) {
      flushProse(prose);
      // h1/h2 in a 330px rail would shout; the transcript's own scale caps at
      // three visible steps, so everything lands in h4-h6.
      const level = Math.min(6, head[1].length + 3);
      root.appendChild(withInline(`h${level}`, head[2]));
      i += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      flushProse(prose);
      const body = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        body.push(QUOTE.exec(lines[i])[1]); i += 1;
      }
      root.appendChild(para('blockquote', body));
      continue;
    }

    const first = listItemOf(line);
    if (first) {
      flushProse(prose);
      const list = el(first.ordered ? 'ol' : 'ul',
        first.ordered && first.start !== 1 ? { start: first.start } : {});
      let li = null;
      let sub = null;                          // the nested list, if one opens
      const baseIndent = first.indent;
      while (i < lines.length) {
        const cur = lines[i];
        if (!cur.trim()) {
          // A blank line ends the list unless another item follows it.
          const next = lines[i + 1];
          if (!next || !listItemOf(next)) break;
          i += 1;
          continue;
        }
        const item = listItemOf(cur);
        if (item) {
          if (item.indent > baseIndent + 1 && li) {
            // One level of nesting, kept on the item that opened it.
            if (!sub || sub.tagName.toLowerCase() !== (item.ordered ? 'ol' : 'ul')) {
              sub = el(item.ordered ? 'ol' : 'ul');
              li.appendChild(sub);
            }
            sub.appendChild(withInline('li', item.text));
          } else if (item.ordered === first.ordered) {
            sub = null;
            li = withInline('li', item.text);
            list.appendChild(li);
          } else {
            break;                             // a different list starts here
          }
          i += 1;
          continue;
        }
        if (INDENT.test(cur) && li) {          // a wrapped continuation line
          const target = sub ? sub.lastChild : li;
          target.appendChild(el('br'));
          inline(cur.trim(), target);
          i += 1;
          continue;
        }
        break;
      }
      root.appendChild(list);
      continue;
    }

    prose.push(line);
    i += 1;
  }
  flushProse(prose);

  // An empty reply still needs a box with a line in it.
  if (!root.childNodes.length) root.appendChild(el('p', { text: '' }));
  return root;
}
