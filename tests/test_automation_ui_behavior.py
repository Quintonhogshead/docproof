"""Run the Automations controller functions against small local DOM/API stubs."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


APP_JS = Path(__file__).parents[1] / "app/static/app.js"


def _function(name: str) -> str:
    source = APP_JS.read_text(encoding='utf-8')
    start = re.search(rf"(?m)^(?:async )?function {name}\(", source)
    assert start is not None
    # These top-level functions use unindented closing braces; their nested
    # blocks are indented. Extract only the function, not the app bootstrap.
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


def test_quiet_poll_updates_clock_without_rebuilding_unchanged_rows():
    _node(["loadWatch", "renderWatch"], """
      let rows = 0, clock = 0;
      const fields = new Map();
      const state = {watchModels: []};
      let watch = {folder_id: 'folder', signed_in: true, corrections_enabled: false};
      const noop = () => {};
      const context = vm.createContext({
        state,
        $: id => { if (!fields.has(id)) fields.set(id, {}); return fields.get(id); },
        api: async () => ({watch, can_schedule: false}),
        renderRegistryPreservingFocus: () => { rows++; },
        renderPassesSummary: () => { clock++; },
        renderWatchSignIn: noop, renderWatchRun: noop, renderWatchBanner: noop,
        renderWatchFiles: noop, renderProofReadout: noop, applyWatchSchedule: noop,
        renderNativeWorker: noop, renderNativeIntake: noop, renderInteriorComputer: noop, refreshNativeQueue: noop,
        renderWatchNextRun: noop,
      });
      vm.runInContext(source, context);
      await context.loadWatch({quiet: true});
      assert.equal(rows, 1);
      rows = 0; clock = 0;
      watch = {...watch, last_tick_at: '2026-09-08T15:00:00Z', files: ['changed']};
      await context.loadWatch({quiet: true});
      assert.equal(rows, 0, 'a clock/history update must not remove focused controls');
      assert.equal(clock, 1);
      watch = {...watch, corrections_enabled: true};
      await context.loadWatch({quiet: true});
      assert.equal(rows, 1, 'corrections readiness must refresh the actual row');
    """)


@pytest.mark.parametrize("workflow,field,endpoint", [
    ("proof", "proofing_enabled", "/api/watch"),
    ("corrections", "corrections_enabled", "/api/watch"),
    ("promo", "promo_enabled", "/api/promo/settings"),
    ("plan", "plan_enabled", "/api/promo/plan-settings"),
])
def test_toggle_waits_for_save_blocks_double_clicks_and_keeps_unsaved_fields(
        workflow, field, endpoint):
    _node(["toggleWorkflow"], f"""
      const id = {json.dumps(workflow)}, field = {json.dumps(field)};
      const endpoint = {json.dumps(endpoint)};
      const wfUI = {{pending: new Set(), errors: new Map([[id, 'old error']])}};
      const fields = new Map([['unsaved', {{value: 'An unfinished draft'}}]]);
      const state = {{}};
      const renders = [];
      const calls = [];
      let resolve;
      const response = new Promise(done => {{ resolve = done; }});
      const context = vm.createContext({{
        state, wfUI,
        $: id => {{ if (!fields.has(id)) fields.set(id, {{}}); return fields.get(id); }},
        api: (url, options) => {{ calls.push({{url, options}}); return response; }},
        renderRegistryPreservingFocus: () => renders.push(wfUI.pending.has(id)),
        renderWatch: (body, quiet) => {{
          assert.equal(quiet, true, 'a switch must not overwrite unsaved form fields');
          state.watchStatus = body.watch;
        }},
      }});
      vm.runInContext(source, context);
      const first = context.toggleWorkflow({{id, enabled: false}});
      assert.equal(wfUI.pending.has(id), true);
      assert.equal(wfUI.errors.has(id), false);
      await context.toggleWorkflow({{id, enabled: false}});
      assert.equal(calls.length, 1);
      assert.equal(calls[0].url, endpoint);
      assert.equal(JSON.parse(calls[0].options.body)[field], true);
      resolve(endpoint === '/api/watch' ? {{watch: {{[field]: true}}}} : {{[field]: true}});
      await first;
      assert.equal(wfUI.pending.has(id), false);
      assert.equal(wfUI.errors.has(id), false);
      assert.deepEqual(renders, [true, false]);
      assert.equal(fields.get(id + '-enabled').checked, true);
      assert.equal(fields.get('unsaved').value, 'An unfinished draft');
      const settings = endpoint === '/api/watch' ? state.watchStatus
        : id === 'promo' ? state.promoSettings : state.planSettings;
      assert.equal(settings[field], true);
    """)


def test_toggle_failure_is_attached_to_the_clicked_workflow():
    _node(["toggleWorkflow"], """
      const wfUI = {pending: new Set(), errors: new Map()};
      const context = vm.createContext({
        wfUI,
        api: async () => { throw new Error('Permission denied'); },
        renderRegistryPreservingFocus: () => {},
        $: () => { throw new Error('A hidden drawer must not receive this error'); },
      });
      vm.runInContext(source, context);
      await context.toggleWorkflow({id: 'corrections', enabled: false});
      assert.equal(wfUI.errors.get('corrections'), 'Permission denied');
      assert.equal(wfUI.errors.has('plan'), false);
      assert.equal(wfUI.pending.size, 0);
    """)


def test_registry_refresh_preserves_row_focus_and_leaves_drawer_focus_alone():
    _node(["renderRegistryPreservingFocus"], """
      const document = {activeElement: null};
      let current;
      let disabled = false;
      function newRow() {
        const row = {dataset: {wf: 'proof'}};
        const button = name => ({
          name, disabled: name === 'wf-toggle' && disabled,
          classList: {contains: value => value === name},
          closest: () => row,
          focus: () => { document.activeElement = row[name]; },
        });
        row['wf-toggle'] = button('wf-toggle');
        row['wf-open'] = button('wf-open');
        row.querySelector = selector => row[selector.slice(1)] || null;
        return row;
      }
      current = newRow();
      const rows = {
        contains: element => element === current['wf-toggle'] || element === current['wf-open'],
        querySelectorAll: () => [current],
      };
      const context = vm.createContext({
        document, $: () => rows,
        renderRegistry: () => { current = newRow(); },
      });
      vm.runInContext(source, context);
      document.activeElement = current['wf-toggle'];
      context.renderRegistryPreservingFocus();
      assert.equal(document.activeElement, current['wf-toggle']);
      disabled = true;
      context.renderRegistryPreservingFocus();
      assert.equal(document.activeElement, current['wf-open']);
      const input = {value: 'Still editing'};
      document.activeElement = input;
      context.renderRegistryPreservingFocus();
      assert.equal(document.activeElement, input);
    """)


def test_attention_includes_all_exposed_workflow_lifecycles_once_per_file():
    _node(["wfNeedsAttention"], """
      const context = vm.createContext({});
      vm.runInContext(source, context);
      const attention = context.wfNeedsAttention;
      const failures = [
        {error: 'Format could not finish'}, {marked: 'failed'},
        {marked: 'formatted', proof_marked: 'human'},
        {marked: 'formatted', proof_marked: 'failed'},
        {marked: 'formatted', proof_outcome: 'needs_human'},
        {corrections_marked: 'failed'},
      ];
      for (const file of failures) assert.equal(attention(file), true);
      const healthy = [
        {}, {marked: 'formatted'}, {proof_marked: 'awaiting'},
        {proof_marked: 'done', proof_outcome: 'done'},
        {corrections_marked: 'done'},
      ];
      for (const file of healthy) assert.equal(attention(file), false);
      const files = [{marked: 'failed', proof_marked: 'human',
                      proof_outcome: 'needs_human', corrections_marked: 'failed'},
                     ...healthy];
      assert.equal(files.filter(attention).length, 1);
    """)


def test_history_filters_processed_books_and_folds_author_accents():
    _node(["applyWatchFilesFilter"], """
      const rows = [{},
        {textContent: 'José Aragón Book 1 read', dataset: {processed: 'true', flagged: 'true'}},
        {textContent: 'Jane Doe failed', dataset: {processed: 'false', flagged: 'true'}},
        {textContent: 'Waiting book', dataset: {processed: 'false', flagged: 'false'}}];
      const fields = {
        'watch-files-filter': {value: 'jose aragon'},
        'watch-files-state': {value: 'processed'},
        'watch-files': {querySelectorAll: () => rows},
        'watch-files-empty': {},
      };
      const context = vm.createContext({$: id => fields[id]});
      vm.runInContext(source, context);
      context.applyWatchFilesFilter();
      assert.deepEqual(rows.slice(1).map(row => row.hidden), [false, true, true]);
      fields['watch-files-filter'].value = '';
      fields['watch-files-state'].value = 'flagged';
      context.applyWatchFilesFilter();
      assert.deepEqual(rows.slice(1).map(row => row.hidden), [false, false, true]);
      fields['watch-files-state'].value = 'all';
      context.applyWatchFilesFilter();
      assert.deepEqual(rows.slice(1).map(row => row.hidden), [false, false, false]);
    """)



def test_clear_flag_sends_only_the_selected_book_and_workflow():
    _node(["resetWatchFlag"], """
      const calls = [], notes = [], renders = [];
      const context = vm.createContext({
        confirm: () => true, $: () => ({}),
        api: async (url, options) => { calls.push([url, JSON.parse(options.body)]); return {name: 'Book'}; },
        renderWatch: body => renders.push(body),
        watchNote: (_, message, kind) => notes.push(kind),
      });
      vm.runInContext(source, context);
      const button = {};
      await context.resetWatchFlag({file_id: 'id', name: 'Book', updated_at: 'revision'},
                                  {stage: 'proof', label: 'Proofreading'}, button);
      assert.deepEqual(calls, [['/api/watch/flags/reset',
                               {file_id: 'id', stage: 'proof', updated_at: 'revision'}]]);
      assert.equal(button.disabled, true);
      assert.deepEqual(notes, ['ok']);
      assert.equal(renders.length, 1);
    """)
