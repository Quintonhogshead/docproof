"""Exercise Drop controller transitions without starting the browser or paid jobs."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


APP_JS = Path(__file__).parents[1] / "app/static/app.js"


def _function(name: str) -> str:
    source = APP_JS.read_text()
    start = re.search(rf"(?m)^(?:async )?function {name}\(", source)
    assert start is not None
    end = source.index("\n}", start.end()) + 2
    return source[start.start():end]


def _node(functions: list[str], script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for browser-controller regression checks")
    declarations = "\n".join(_function(name) for name in functions)
    program = (
        "const assert = require('node:assert/strict');\n"
        "const vm = require('node:vm');\n"
        f"const source = {json.dumps(declarations)};\n"
        "(async () => {\n" + script + "\n})().catch(error => {\n"
        "  console.error(error); process.exitCode = 1;\n});\n"
    )
    result = subprocess.run([node, "-e", program], capture_output=True, text=True,
                            timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


GATES = ["filesToRun", "keptFor", "galleyBudget", "dropStartIssue", "renderStartState"]
CONTEXT = """
  const fields = new Map();
  const field = id => {
    if (!fields.has(id)) fields.set(id, {
      value: '', checked: false, dataset: {}, hidden: false, attrs: {},
      classList: {add() {}, remove() {}},
      setAttribute(name, value) { this.attrs[name] = value; },
    });
    return fields.get(id);
  };
  let currentKind = 'review';
  const first = {id: 'one', ok: true, filename: 'Book.docx', paragraphs: 2,
    chunks: [{chunk_id: 'a'}, {chunk_id: 'b'}], can_review: true,
    can_prep: true, can_promo: true, can_correct: true};
  const state = {files: [first], selected: new Map(), startBusy: false,
    stagingCount: 0, correctionsReading: 0, correctionsReadFailed: false,
    correctionsSource: null, tier: 'standard', models: [{id: 'model', available: true}]};
  field('model').value = 'model'; field('rounds').value = '1';
  field('galley-tier').value = 'T2';
  const noop = () => {};
  const errors = [], calls = [], screens = [];
  const context = vm.createContext({
    state, $: field, kind: () => currentKind,
    isPrep: () => currentKind === 'prep', isPromo: () => currentKind === 'promo',
    isGalley: () => currentKind === 'galley', isCorrections: () => currentKind === 'corrections',
    usableFiles: () => state.files.filter(f => f.ok),
    canRun: f => f['can_' + ({galley: 'review', corrections: 'correct'}[currentKind] || currentKind)] !== false,
    api: async (url, options) => { calls.push({url, payload: JSON.parse(options.body)}); },
    fail: error => errors.push(error), show: screen => screens.push(screen),
    renderFiles: noop, renderKind: noop, clearCorrectionsSource: noop,
    prepOutput: () => 'book', mode: () => 'now', effortValue: () => 'high',
    collectFeatures: () => ({storysheet: true, spelling: true}), collectCategoryKnobs: () => ({}),
    resolveTier: () => ({}), selectionPayload: () => ({one: null}),
    updateAdvancedSummary: noop, renderDropSummary: noop, syncBatchAvailability: noop,
    pricePrep: () => 0.3, priceReview: () => ({now: 1, batch: 0.5}),
    bundleFromControls: () => ({}), stagedReviewFiles: () => [], updateTierPrices: noop,
    renderPromoCost: noop, setStartPrice: noop,
  });
  vm.runInContext(source, context);
"""


def test_picker_can_stage_the_same_file_again_after_removal():
    _node(["pickDroppedFiles"], """
      const file = {name: 'Book.docx'}, uploads = [];
      const input = {files: [file], value: 'Book.docx'};
      const context = vm.createContext({input, upload: files => {
        assert.equal(input.value, '', 'reset before the upload starts');
        uploads.push(files);
      }});
      vm.runInContext(source, context);
      context.pickDroppedFiles();
      input.value = 'Book.docx';
      context.pickDroppedFiles();
      assert.equal(uploads.length, 2);
      assert.equal(uploads[0][0], file);
      assert.equal(uploads[1][0], file);
    """)


def test_corrections_word_proof_can_arrive_before_its_indesign_book():
    _node(GATES + ["upload", "routeCorrectionsDrop"], CONTEXT + """
      currentKind = 'corrections'; state.files = [];
      const proof = {name: 'Redlined proof.docx'}, book = {name: 'Book.idml'};
      context.FormData = class {
        constructor() { this.entries = []; }
        append(name, file) { this.entries.push({name, file}); }
      };
      context.setCorrectionsKind = () => { currentKind = 'corrections'; };
      context.attachCorrectionsSource = file => { state.correctionsSource = file; };
      context.renderCorrectionsSource = noop;
      context.allowedSuffixes = () => ['.docx', '.idml'];
      context.showStaging = noop; context.hideStaging = noop;
      context.loadModels = async () => {};
      context.api = async (url, options) => {
        calls.push({url, entries: options.body.entries});
        return {files: options.body.entries.map(({file}) => ({
          ...first, id: 'book', filename: file.name, can_correct: true,
        }))};
      };
      await context.upload([proof]);
      assert.equal(state.correctionsSource, proof);
      assert.equal(state.files.length, 0);
      assert.equal(calls.length, 0, 'a proof must never go through manuscript preflight');
      assert.match(context.dropStartIssue(), /Add the InDesign file/);
      await context.upload([book]);
      assert.equal(calls.length, 1);
      assert.equal(calls[0].entries[0].file, book);
      assert.equal(state.files.length, 1);
      assert.equal(state.files[0].filename, 'Book.idml');
      assert.equal(state.correctionsSource, proof);
      assert.equal(context.dropStartIssue(), '');
    """)


def test_review_word_upload_remains_a_manuscript_without_a_corrections_book():
    _node(["routeCorrectionsDrop"], CONTEXT + """
      const proof = {name: 'Book.docx'}, book = {name: 'Layout.idml'};
      state.files = [{...first, can_correct: false}];
      const existing = state.files[0];
      context.setCorrectionsKind = () => { currentKind = 'corrections'; };
      context.attachCorrectionsSource = file => { state.correctionsSource = file; };
      const incoming = [proof];
      assert.equal(context.routeCorrectionsDrop(incoming), incoming);
      assert.equal(state.correctionsSource, null);
      assert.equal(currentKind, 'review');
      // The existing book-plus-proof heuristic still works for a combined drop.
      const staged = context.routeCorrectionsDrop([book, proof]);
      assert.equal(staged.length, 1);
      assert.equal(staged[0], book);
      assert.equal(state.correctionsSource, proof);
      assert.equal(currentKind, 'corrections');
      assert.equal(state.files[0], existing, 'routing new files must not reinterpret old files');
    """)


def test_galley_uses_the_whole_book_and_its_budget_instead_of_review_model():
    _node(GATES + ["renderCost", "setStartNote"], CONTEXT + """
      state.selected.set('one', new Set());
      assert.equal(context.filesToRun().length, 0);
      currentKind = 'galley';
      state.models = [{id: 'model', available: false}];
      context.priceReview = () => { throw new Error('Galley must not use review pricing'); };
      context.renderCost();
      assert.equal(context.filesToRun().length, 1);
      assert.equal(field('start').disabled, false);
      assert.equal(field('start-price').textContent, 'T2 default budget');
      field('galley-budget').value = '25';
      context.renderCost();
      assert.equal(field('start-price').textContent, '$25 budget limit');
      for (const invalid of ['0', '-2', 'Infinity', 'bad']) {
        field('galley-budget').value = invalid;
        context.renderCost();
        assert.equal(field('start').disabled, true, invalid);
        assert.match(field('start-hint').textContent, /greater than [$]0/);
      }
      field('galley-budget').value = '';
      field('galley-budget').validity = {badInput: true};
      assert.match(context.dropStartIssue(), /greater than [$]0/);
    """)


@pytest.mark.parametrize("kind", ["review", "prep", "promo", "corrections", "galley"])
def test_pricing_refresh_cannot_reenable_start_during_submission(kind):
    _node(GATES + ["renderCost", "setStartNote"], CONTEXT + f"""
      currentKind = {json.dumps(kind)};
      state.startBusy = true;
      field('corrections-input').value = '[{{"find":"a","replace":"b"}}]';
      context.renderCost();
      assert.equal(field('start').disabled, true);
      assert.equal(field('start').attrs['aria-busy'], 'true');
    """)


def test_empty_invalid_unselected_and_missing_corrections_have_clear_hints():
    _node(GATES, CONTEXT + """
      state.files = [];
      assert.match(context.dropStartIssue(), /Add a document/);
      state.files = [{...first, ok: false}];
      assert.match(context.dropStartIssue(), /No documents are ready/);
      state.files = [first]; state.selected.set('one', new Set());
      assert.match(context.dropStartIssue(), /at least one section/);
      currentKind = 'corrections';
      assert.match(context.dropStartIssue(), /Attach a marked proof/);
      state.correctionsSource = {name: 'Proof.pdf'};
      assert.equal(context.dropStartIssue(), '');
      state.correctionsReading = 1;
      assert.match(context.dropStartIssue(), /still being read/);
      state.correctionsReading = 0; state.correctionsReadFailed = true;
      assert.match(context.dropStartIssue(), /did not finish/);
    """)


@pytest.mark.parametrize("kind", ["corrections", "galley"])
def test_multiple_books_are_visibly_blocked_without_discarding_any(kind):
    _node(GATES + ["startDocuments"], CONTEXT + f"""
      currentKind = {json.dumps(kind)};
      state.files.push({{...first, id: 'two'}});
      await context.startDocuments();
      assert.equal(calls.length, 0);
      assert.match(state.startError, /one book at a time/);
      assert.equal(state.files.length, 2);
      assert.equal(state.startBusy, false);
    """)


def test_duplicate_start_is_ignored_and_unsubmitted_files_are_preserved():
    _node(GATES + ["startDocuments"], CONTEXT + """
      let resolve;
      context.api = (url, options) => {
        calls.push({url, payload: JSON.parse(options.body)});
        return new Promise(done => { resolve = done; });
      };
      const omitted = {...first, id: 'unchecked'};
      state.files.push(omitted);
      state.selected.set('unchecked', new Set());
      const starting = context.startDocuments();
      await context.startDocuments();
      assert.equal(calls.length, 1);
      assert.deepEqual(calls[0].payload.file_ids, ['one']);
      assert.equal(state.startBusy, true);
      // A different in-flight staging request must not lose its result either.
      const later = {...first, id: 'later'};
      state.files.push(later);
      resolve({}); await starting;
      assert.deepEqual(Array.from(state.files, f => f.id), ['unchecked', 'later']);
      assert.equal(state.selected.has('unchecked'), true);
      assert.equal(state.selected.has('one'), false);
      assert.equal(state.startBusy, false);
      assert.deepEqual(screens, ['jobs']);
    """)


def test_failed_submission_keeps_documents_and_a_changed_task_cannot_apply():
    _node(GATES + ["startDocuments"], CONTEXT + """
      context.api = async () => { throw new Error('Server unavailable'); };
      await context.startDocuments();
      assert.equal(state.files[0], first);
      assert.equal(state.startBusy, false);
      assert.deepEqual(errors, [], 'submit errors should have only one alert');
      assert.equal(field('start-error').textContent, 'Server unavailable');
      assert.equal(field('start-error').hidden, false);
      context.renderStartState();
      assert.equal(field('start-error').textContent, 'Server unavailable');
      currentKind = 'corrections'; state.correctionsSource = {name: 'Proof.pdf'};
      context.readCorrectionsSource = async () => { currentKind = 'review'; };
      await context.startDocuments();
      assert.match(state.startError, /task or documents changed/);
      assert.equal(calls.length, 0);
      assert.equal(state.files[0], first);
    """)


@pytest.mark.parametrize("kind", ["prep", "galley"])
def test_non_review_payload_omits_review_sections_and_prep_storysheet(kind):
    _node(GATES + ["startDocuments"], CONTEXT + f"""
      currentKind = {json.dumps(kind)};
      state.selected.set('one', new Set());
      const sharedFeatures = {{storysheet: true, spelling: true}};
      context.collectFeatures = () => sharedFeatures;
      await context.startDocuments();
      assert.deepEqual(errors, []);
      assert.equal(calls.length, 1);
      const payload = calls[0].payload;
      assert.deepEqual(payload.selections, {{}});
      assert.deepEqual(payload.file_ids, ['one']);
      if (currentKind === 'prep') {{
        assert.equal(payload.features.storysheet, false);
        assert.equal(payload.glossary_model, 'off');
        assert.equal(sharedFeatures.storysheet, true, 'keep the proofing choice intact');
      }} else {{
        assert.equal(payload.model, '');
        assert.equal(payload.budget_usd, null);
      }}
    """)


def test_submit_error_falls_back_for_pages_without_inline_error_field():
    _node(GATES + ["startDocuments"], CONTEXT + """
      context.$ = id => id === 'start-error' ? null : field(id);
      context.api = async () => { throw new Error('Server unavailable'); };
      await context.startDocuments();
      assert.deepEqual(errors, ['Server unavailable']);
      assert.equal(state.files[0], first);
    """)


def test_overlapping_staging_keeps_start_locked_until_all_uploads_finish():
    _node(GATES + ["showStaging", "hideStaging"], CONTEXT + """
      context.showStaging(2); context.showStaging(1);
      assert.equal(state.stagingCount, 3);
      assert.equal(field('start').disabled, true);
      context.hideStaging(2);
      assert.equal(field('staging').hidden, false);
      assert.equal(field('staging-text').textContent, 'Reading your document…');
      assert.equal(field('start').disabled, true);
      context.hideStaging(1);
      assert.equal(field('staging').hidden, true);
      assert.equal(field('start').disabled, false);
    """)


def test_format_filters_preserve_task_and_describe_proofs_without_conversion_jargon():
    _node(["allowedSuffixes", "renderDropFormats", "applyFormatChoice"], """
      let currentKind = 'prep';
      const state = {formatChoice: 'all', formats: [{suffix: '.docx'}, {suffix: '.idml'}], extraSuffixes: []};
      const formats = {}, input = {}, copy = {}, foot = {};
      const context = vm.createContext({state, input,
        $: id => ({'drop-formats': formats, 'drop-upload-copy': copy, 'drop-upload-foot': foot})[id],
        isCorrections: () => currentKind === 'corrections',
        isGalley: () => currentKind === 'galley',
        renderFiles() {}, renderKind() {}});
      vm.runInContext(source, context);
      context.applyFormatChoice('all');
      assert.equal(formats.textContent, '.docx, .idml · PDF proofs open Corrections');
      context.applyFormatChoice('.idml');
      assert.equal(currentKind, 'prep');
      currentKind = 'corrections'; context.renderDropFormats();
      assert.match(formats.textContent, /marked PDF or Word proof/);
      assert.match(copy.textContent, /one IDML/);
      assert.match(foot.textContent, /Word proofs are read automatically/);
      assert.match(foot.textContent, /small cost/);
      for (const suffix of ['.idml', '.pdf', '.docx']) assert.ok(input.accept.includes(suffix));
      currentKind = 'galley'; context.renderDropFormats();
      assert.match(copy.textContent, /one Word manuscript/);
      assert.match(foot.textContent, /tier and budget/);
      currentKind = 'review'; context.renderDropFormats();
      assert.equal(copy.textContent, 'Add a manuscript, a layout, or a whole batch.');
    """)


def test_entering_review_applies_initial_tier_and_rechecks_later_model_changes():
    _node(GATES + ["changeDropKind", "maybeInitTier", "reEvaluateTier"], CONTEXT + """
      state.tier = null;
      state.presets = {standard: {}};
      context.document = {querySelector: () => ({})};
      context.applyPreset = tier => {
        state.tier = tier; field('model').value = 'standard-model';
      };
      context.TIER_ORDER = ['standard'];
      context.currentMatchesTier = () => field('model').value === 'standard-model';
      context.paintTierCards = noop;
      currentKind = 'prep';
      field('model').value = 'format-model';
      context.changeDropKind();
      assert.equal(state.tier, null);
      currentKind = 'review'; context.changeDropKind();
      assert.equal(state.tier, 'standard');
      assert.equal(field('model').value, 'standard-model');
      currentKind = 'promo'; field('model').value = 'promo-model';
      context.changeDropKind();
      currentKind = 'review'; context.changeDropKind();
      assert.equal(state.tier, 'custom', 'the old tier must not mislabel changed controls');
      assert.equal(field('model').value, 'promo-model');
    """)


def test_galley_refuses_indesign_because_its_ingest_requires_word():
    _node(GATES + ["canRun", "reasonBlocked"], CONTEXT + """
      currentKind = 'galley';
      assert.equal(context.canRun(first), true);
      const layout = {...first, filename: 'Layout.idml'};
      assert.equal(context.canRun(layout), false);
      assert.match(context.reasonBlocked(layout), /Word manuscript/);
      state.files = [layout];
      assert.equal(context.filesToRun().length, 0);
      currentKind = 'review';
      assert.equal(context.canRun(layout), true);
    """)


def test_file_list_labels_removal_and_only_offers_sections_for_review():
    _node(["renderFiles", "fileSummary", "keptFor"], CONTEXT + """
      function element(tag) {
        return {tag, children: [], attrs: {}, listeners: {},
          append(...children) { this.children.push(...children); },
          setAttribute(name, value) { this.attrs[name] = value; },
          addEventListener(name, fn) { this.listeners[name] = fn; },
          focus() {},
        };
      }
      const list = element('ul');
      Object.defineProperty(list, 'innerHTML', {set() { this.children = []; }});
      fields.set('file-list', list);
      context.document = {createElement: element};
      context.renderCost = noop;
      context.sectionPicker = () => element('details');
      context.loadModels = async () => {};
      context.renderFiles();
      const remove = list.children[0].children.find(child => child.tag === 'button');
      assert.equal(remove.attrs['aria-label'], 'Remove Book.docx');
      assert.equal(remove.type, 'button');
      assert.ok(list.children[0].children.some(child => child.tag === 'details'));
      currentKind = 'galley';
      context.renderFiles();
      assert.ok(!list.children[0].children.some(child => child.tag === 'details'));
      assert.match(list.children[0].children[1].textContent, /whole book/);
      state.startBusy = true; context.renderFiles();
      const busyRemove = list.children[0].children.find(child => child.tag === 'button');
      assert.equal(busyRemove.disabled, true);
      busyRemove.listeners.click();
      assert.equal(state.files.length, 1);
    """)
