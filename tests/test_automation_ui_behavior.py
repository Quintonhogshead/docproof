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
        renderWatchNextRun: noop, renderCorrectionsWaiting: noop, renderCorrectionsRehearsal: noop,
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


# --- the practitioner machine readout ------------------------------------------
#
# Everything on this readout is either an elapsed time between two clocks or a
# claim about whether a machine is working. Both have been wrong in ways that
# are invisible from the code alone, so they are pinned here against a DOM stub.

AGENT_DOM = """
  const AGENT_QUIET_S = 900, AGENT_FETCH_STALE_S = 30;
  const AGENT_STATE_WORD = {running: 'Reading', stopping: 'Wrapping up',
                            finishing: 'Finishing'};
  const PROOF_VERDICT_LABEL = {done: 'Clean', needs_human: 'Needs a human',
    held: 'Held, untouched', blocked: 'Stopped, still claimed'};
  const agentClock = {server: 0, client: 0};
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      id, hidden: false, textContent: '', innerHTML: '', children: [],
      style: {}, classList: {values: new Set(),
        add(v) { this.values.add(v); }, remove(v) { this.values.delete(v); },
        toggle(v, on) { on ? this.values.add(v) : this.values.delete(v); },
        contains(v) { return this.values.has(v); }},
      append(...kids) {
        for (const kid of kids) {
          this.children.push(kid);
          this.textContent += (kid.textContent === undefined ? kid : kid.textContent);
        }
      },
      querySelector(sel) { return node(this.id + sel); },
    });
    const found = nodes.get(id);
    if (found.innerHTML === '' && found.__cleared) { found.__cleared = false; }
    return found;
  }
  function make(tag) {
    const el = {tag, textContent: '', children: [], style: {}, className: '',
      classList: {values: new Set(), add(v) {this.values.add(v);},
                  toggle(v, on) {on ? this.values.add(v) : this.values.delete(v);},
                  contains(v) {return this.values.has(v);}},
      append(...kids) { for (const k of kids) { this.children.push(k);
        this.textContent += (k.textContent === undefined ? k : k.textContent); } },
      querySelector: () => null};
    return el;
  }
  const document = {createElement: make,
                    createTextNode: text => ({textContent: String(text)})};
  const $ = id => {
    const el = node(id);
    // Assigning innerHTML = '' is how the renderer empties a block.
    Object.defineProperty(el, 'innerHTML', {configurable: true,
      get: () => el.__html || '',
      set: v => { el.__html = v; if (v === '') { el.textContent = '';
                                                 el.children.length = 0; } }});
    return el;
  };
"""


def _agent_node(script: str) -> None:
    _node(["anchorAgentClock", "agentNow", "agentSince", "agentAgo", "agentFor",
           "agentElapsed", "agentBooks", "agentModel", "agentMoney",
           "agentFact", "renderAgentReadout"], AGENT_DOM + script)


def test_elapsed_times_follow_the_servers_clock_not_the_browsers():
    _agent_node("""
      const context = vm.createContext({document, $, agentClock,
        AGENT_QUIET_S, AGENT_FETCH_STALE_S, AGENT_STATE_WORD,
        PROOF_VERDICT_LABEL, console});
      vm.runInContext(source, context);
      // The browser's clock is five minutes behind the server's. A phase that
      // started one minute ago must read as one minute, not as the future.
      const serverNow = Date.now() + 5 * 60000;
      const agent = {received_at: new Date(serverNow - 10000).toISOString(),
                     age_s: 10, state: 'running', book: 'A Book',
                     phase: 'typed', phase_index: 4, phase_total: 11,
                     phase_started_at: new Date(serverNow - 60000).toISOString()};
      context.anchorAgentClock(agent);
      context.renderAgentReadout({agent, proof_runner: 'external'});
      const facts = nodes.get('proof-agent-facts');
      const values = facts.children.map(c => c.children[1].textContent);
      assert.ok(values.includes('60s'),
                'the phase clock must be read against the server: ' + values);
      assert.match(nodes.get('proof-agent-line').textContent, /Reporting 10s ago/);
    """)


def test_a_finished_verdict_is_a_note_not_an_error():
    _agent_node("""
      const context = vm.createContext({document, $, agentClock,
        AGENT_QUIET_S, AGENT_FETCH_STALE_S, AGENT_STATE_WORD,
        PROOF_VERDICT_LABEL, console});
      vm.runInContext(source, context);
      const agent = {received_at: new Date().toISOString(), age_s: 4,
                     state: 'idle', awaiting: 1, handled_here: 1,
                     last_book: 'Wilder - Book 1.docx',
                     last_outcome: 'needs_human',
                     last_reason: 'Fixed proofreading complete; every required '
                                + 'reading and output check passed.'};
      context.anchorAgentClock(agent);
      context.renderAgentReadout({agent, proof_runner: 'external'});
      assert.equal(nodes.get('proof-agent-error').hidden, true,
                   'a clean verdict must never print as a machine fault');
      assert.equal(nodes.get('proof-agent-note').hidden, false);
      assert.match(nodes.get('proof-agent-note').textContent,
                   /Last verdict \\(Needs a human\\)/);
      // And the confusing "Idle + 1 awaiting" is explained rather than shown raw.
      assert.match(nodes.get('proof-agent-headline').textContent,
                   /already finished on this machine/);
    """)


def test_a_real_fault_still_prints_as_one():
    _agent_node("""
      const context = vm.createContext({document, $, agentClock,
        AGENT_QUIET_S, AGENT_FETCH_STALE_S, AGENT_STATE_WORD,
        PROOF_VERDICT_LABEL, console});
      vm.runInContext(source, context);
      const agent = {received_at: new Date().toISOString(), age_s: 2,
                     state: 'halted', credentials_error: 'the token was rejected',
                     held_book: 'Wilder - Book 1.docx'};
      context.anchorAgentClock(agent);
      context.renderAgentReadout({agent, proof_runner: 'external'});
      assert.equal(nodes.get('proof-agent-error').hidden, false);
      assert.equal(nodes.get('proof-agent-error').textContent,
                   'the token was rejected');
      assert.match(nodes.get('proof-agent-headline').textContent,
                   /claimed and untouched/);
    """)


def test_a_page_that_lost_the_server_says_so_instead_of_freezing():
    _agent_node("""
      const context = vm.createContext({document, $, agentClock,
        AGENT_QUIET_S, AGENT_FETCH_STALE_S, AGENT_STATE_WORD,
        PROOF_VERDICT_LABEL, console});
      vm.runInContext(source, context);
      const agent = {received_at: new Date().toISOString(), age_s: 5,
                     state: 'running', book: 'A Book', phase: 'typed'};
      context.anchorAgentClock(agent);
      // No further fetch has landed for two minutes; the tick redraws anyway.
      agentClock.client -= 120000;
      context.renderAgentReadout({agent, proof_runner: 'external'});
      assert.match(nodes.get('proof-agent-line').textContent,
                   /this page last reached DocProof/);
    """)


def test_a_stage_bar_reports_position_and_stall():
    _agent_node("""
      const context = vm.createContext({document, $, agentClock,
        AGENT_QUIET_S, AGENT_FETCH_STALE_S, AGENT_STATE_WORD,
        PROOF_VERDICT_LABEL, console});
      vm.runInContext(source, context);
      const now = Date.now();
      const agent = {received_at: new Date(now).toISOString(), age_s: 1,
                     state: 'running', book: 'A Book', phase: 'typed',
                     phase_index: 4, phase_total: 11, phase_note: 'Local checks',
                     step: 'LanguageTool', step_done: 500, step_total: 1000,
                     last_activity_at: new Date(now - 3600000).toISOString()};
      context.anchorAgentClock(agent);
      context.renderAgentReadout({agent, proof_runner: 'external'});
      assert.equal(nodes.get('proof-agent-progress').hidden, false);
      // Three whole stages plus half of the fourth, out of eleven.
      assert.equal(nodes.get('proof-agent-bar-fill').style.width, '31.8%');
      assert.match(nodes.get('proof-agent-stage').textContent,
                   /Stage 4 of 11 — typed: Local checks/);
      assert.equal(nodes.get('proof-agent-progress.wf-agent-bar')
                        .classList.contains('stalled'), true);
      assert.match(nodes.get('proof-agent-note').textContent,
                   /No session output for 1 h 0 min/);
      assert.match(nodes.get('proof-agent-detail').textContent,
                   /LanguageTool: 500 of 1000/);
    """)


def test_a_machine_that_stopped_reporting_is_not_narrated_as_running():
    _agent_node("""
      const context = vm.createContext({document, $, agentClock,
        AGENT_QUIET_S, AGENT_FETCH_STALE_S, AGENT_STATE_WORD,
        PROOF_VERDICT_LABEL, console});
      vm.runInContext(source, context);
      const now = Date.now();
      const agent = {received_at: new Date(now - 95 * 60000).toISOString(),
                     age_s: 5700, stale: true, poll_interval_s: 300,
                     state: 'running', book: 'Wilder - Book 2.docx',
                     phase: 'astra_review', phase_index: 11, phase_total: 13};
      context.anchorAgentClock(agent);
      context.renderAgentReadout({agent, proof_runner: 'external'});
      const headline = nodes.get('proof-agent-headline').textContent;
      assert.match(headline, /Stopped reporting while reading/);
      assert.doesNotMatch(headline, /^Reading/,
                          'the last thing it said is not what it is doing now');
      assert.match(nodes.get('proof-agent-line').textContent, /Silent for 1 h 35 min/);
      assert.equal(nodes.get('proof-agent-progress.wf-agent-bar')
                        .classList.contains('stalled'), true);
    """)
