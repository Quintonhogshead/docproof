from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
NODE = shutil.which("node")


def _run_node(script: str) -> dict:
    if NODE is None:
        pytest.skip("Node.js is not available")
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_native_queue_rendering_formats_epoch_seconds_as_real_dates():
    result = _run_node(r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('app/static/app.js', 'utf8');
const start = source.indexOf('function nativeReceiptTime');
const end = source.indexOf('async function refreshNativeQueue', start);
const code = source.slice(start, end);
const nodes = {};
function node(tag) {
  return {tag, textContent: '', hidden: false, className: '', children: [],
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; }};
}
function $(id) { return nodes[id] || (nodes[id] = node('div')); }
const context = {document: {createElement: node}, $, Date, Number, Array, Math,
  console, JSON};
vm.runInNewContext(code, context, {filename: 'app.js'});
context.renderNativeQueue({quiet_seconds: 10800, events: [{
  project_id: 'p1', marker: 'submission', received_at: 1700000000,
  ready_at: 1700000000, reason: ''}], batches: [],
  books: [{project_id: 'p1', title: 'A Book', author: 'An Author'}]});
const rendered = nodes['native-queue-events'].children
  .map(item => item.textContent).join(' ');
if (!rendered.includes('2023') || rendered.includes('1970'))
  throw new Error('epoch-second queue timestamps were rendered as the Unix epoch');
process.stdout.write(JSON.stringify({rendered}));
''')
    assert "2023" in result["rendered"]
    assert "1970" not in result["rendered"]


def test_verified_book_callback_sends_integer_version_and_alias_arrays():
    result = _run_node(r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('app/static/app.js', 'utf8');
const start = source.indexOf("$('native-book-form').addEventListener('submit'");
const end = source.indexOf('// History:', start);
const code = source.slice(start, end);
const nodes = {};
function node(tag) {
  return {tag, value: '', textContent: '', hidden: false, disabled: false,
    className: '', children: [], reset() { this.wasReset = true; },
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; }};
}
function $(id) { return nodes[id] || (nodes[id] = node('input')); }
const form = $('native-book-form');
let submit;
form.addEventListener = (name, callback) => { if (name === 'submit') submit = callback; };
const values = {
  'native-book-project-id': 'project-1', 'native-book-title': 'Book',
  'native-book-author': 'Author', 'native-book-surname': 'Author',
  'native-book-folder-id': 'folder-1', 'native-book-source-id': 'source-7',
  'native-book-source-version': '7',
  'native-book-title-aliases': 'Book Two\n  Another Title  \n',
  'native-book-author-aliases': 'A. Author\nAuthor Jr.\n',
};
for (const [id, value] of Object.entries(values)) $(id).value = value;
let request;
const context = {document: {createElement: node}, $, Date, Number, Array, Math,
  console, JSON, state: {},
  api: async (path, options) => { request = {path, options}; return {}; },
  refreshNativeQueue: async () => {}};
vm.runInNewContext(code, context, {filename: 'app.js'});
if (!submit) throw new Error('native book submit callback was not registered');
submit({preventDefault() {}, target: form}).then(() => {
  const body = JSON.parse(request.options.body);
  if (typeof body.source_version !== 'number' || body.source_version !== 7)
    throw new Error('book version was not sent as an integer');
  if (JSON.stringify(body.title_aliases) !== JSON.stringify(['Book Two', 'Another Title']))
    throw new Error('title aliases were not split into trimmed lines');
  if (JSON.stringify(body.author_aliases) !== JSON.stringify(['A. Author', 'Author Jr.']))
    throw new Error('author aliases were not split into trimmed lines');
  process.stdout.write(JSON.stringify({body}));
});
''')
    body = result["body"]
    assert body["source_version"] == 7
    assert body["title_aliases"] == ["Book Two", "Another Title"]
    assert body["author_aliases"] == ["A. Author", "Author Jr."]
