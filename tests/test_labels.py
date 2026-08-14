"""The free-form-label contract, and the guard that stops it rotting.

`docproof/labels.py` claims to know every finding label with no
`config/error_types/*.yaml` behind it. That claim is only worth anything if it
cannot quietly fall out of date, because the failure it protects against is
invisible: code that infers "not in `config.error_types`, so the pass never
ran" is *always* wrong about a free-form label, and being wrong looks exactly
like being correctly suppressed.

So the guard below reads the source rather than trusting the list. It walks
every `error_type=` assignment in `docproof/` and `app/`, resolves the ones it
can, and fails if a label appears that is neither a registry key nor declared
free-form. The ones it cannot resolve — a pass-through like `q.error_type` —
are pinned by an allowlist, so a NEW opaque expression also fails the test: an
unreadable call site is exactly where the next undeclared label would hide.
"""
from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest

from docproof.config import Config, load_config
from docproof.labels import (DID_NOT_RUN, FREE_FORM, RAN, SWEEP_PREFIX, UNKNOWN,
                             free_form_labels, is_free_form, produced_in,
                             will_produce)
from docproof.models import Finding
from docproof.sweeps import SWEEPS

ROOT = Path(__file__).parent.parent
ERROR_DIR = ROOT / "config" / "error_types"
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"

# `error_type=` expressions the scanner cannot resolve to a literal. Every one
# is a pass-through that carries a label decided somewhere else — the analyzer
# relaying what the model said, the sweeps relaying their own key, Sapling
# relaying ERRANT's category. Pinned as source text so that adding a new opaque
# call site fails this test and forces a look at what label it can mint.
OPAQUE = {
    "app/routes/sapling.py: pe.error_type or 'sapling'",
    "docproof/analyzer.py: (Literal[keys], ...)",
    "docproof/analyzer.py: error_type",
    "docproof/analyzer.py: rf.error_type",
    "docproof/editlayer.py: e.contributions[0].error_type",
    "docproof/eval/corpus.py: etype",
    "docproof/rewrite.py: error_type",
    "docproof/rounds.py: q.error_type",
    "docproof/sapling.py: str(item.get('error_type', ''))",
    "docproof/sweeps.py: sweep.key",
}

# Keys a findings.json row carries that are not Finding fields. `rejudge.read_run`
# drops anything outside the dataclass rather than matching this list, so this is
# a description of reporting.py, not a thing rejudge depends on staying current.
REPORTING_ONLY = {"applied"}


def _registry_keys() -> set[str]:
    return {p.stem for p in ERROR_DIR.glob("*.yaml")
            if not p.stem.startswith("_")}


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"`, so `error_type=CONSISTENCY_KEY`
    resolves instead of counting as opaque."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _scan() -> tuple[dict[str, set[str]], set[str]]:
    """Every `error_type=` in the shipped source: the labels it can resolve
    (mapped to the files that mint them), and the expressions it cannot."""
    literals: dict[str, set[str]] = {}
    opaque: set[str] = set()
    files = sorted(list((ROOT / "docproof").rglob("*.py"))
                   + list((ROOT / "app").rglob("*.py")))
    assert files, "found no source to scan — the paths above are wrong"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "error_type":
                    continue
                value = kw.value
                if isinstance(value, ast.Constant) \
                        and isinstance(value.value, str):
                    literals.setdefault(value.value, set()).add(rel)
                elif isinstance(value, ast.Name) and value.id in constants:
                    literals.setdefault(constants[value.id], set()).add(rel)
                else:
                    opaque.add(f"{rel}: {ast.unparse(value)}")
    return literals, opaque


# --- the rot guard -----------------------------------------------------------

def test_no_undeclared_free_form_label():
    """A label with no YAML behind it must be declared in FREE_FORM.

    This is the test that fails when someone adds a pass. It fails on purpose
    and it fails loudly, because the alternative is every "did this pass run?"
    check in the tree silently answering "no" about the new pass forever."""
    literals, _ = _scan()
    registry = _registry_keys()
    undeclared = {label: sorted(files) for label, files in literals.items()
                  if label not in registry and label not in free_form_labels()}
    assert not undeclared, (
        f"these findings labels have no config/error_types/<name>.yaml and are "
        f"not declared in docproof.labels: {undeclared}. Add each to "
        f"labels.FREE_FORM, then check every caller of will_produce/produced_in "
        f"— a label the config cannot name is a label config.error_types can "
        f"never contain, so anything reasoning from that list is wrong about it.")


def test_no_new_opaque_error_type_expression():
    """An `error_type=` the scanner cannot read is where the next undeclared
    label hides, so the set of them is pinned rather than skipped."""
    _, opaque = _scan()
    assert opaque == OPAQUE, (
        f"new: {sorted(opaque - OPAQUE)}; gone: {sorted(OPAQUE - opaque)}. "
        f"If the new one can mint a label with no YAML behind it, declare that "
        f"label in labels.FREE_FORM before adding it here.")


def test_free_form_labels_are_really_free_form():
    """No entry in FREE_FORM may have a YAML — an entry that gains one is a
    label the registry now speaks about, and leaving it here would keep the
    honest checks answering UNKNOWN about a type they could resolve."""
    assert not (FREE_FORM & _registry_keys())
    assert not ({s.key for s in SWEEPS} & _registry_keys())


def test_free_form_entries_are_all_minted():
    """And no dead entries: every declared label is one some pass still uses,
    so the list stays a description of the code rather than of its history."""
    literals, _ = _scan()
    assert FREE_FORM <= set(literals), (
        f"declared but never assigned: {sorted(FREE_FORM - set(literals))}")


def test_every_sweep_key_is_prefixed():
    """`is_free_form` recognises sweeps by prefix rather than by list, which
    holds only while sweeps.py is the one place that mints them."""
    for sweep in SWEEPS:
        assert sweep.key.startswith(SWEEP_PREFIX), sweep.key
        assert is_free_form(sweep.key)


def test_shipped_config_cannot_name_a_free_form_label():
    """The fault line itself, asserted: `error_type_keys` is what reporting.py
    writes into findings.json as `config.error_types`, and it can never contain
    a free-form label — so no check may read absence from it as a negative."""
    cfg = load_config(DEFAULT_CONFIG)
    assert not (set(cfg.error_type_keys) & free_form_labels())
    assert cfg.sweeps, "the shipped config runs sweeps whose labels are all free-form"


# --- will_produce ------------------------------------------------------------

def test_will_produce_reads_the_pass_switch_not_the_type_list():
    cfg = load_config(DEFAULT_CONFIG)
    cfg.error_types = ["spelling"]           # the narrowing that caused the bug
    cfg.sweeps = ["sweep_doubled_word"]
    cfg.consistency.enabled = True
    cfg.continuity.enabled = False
    assert will_produce(cfg, "sweep_doubled_word") == RAN
    assert will_produce(cfg, "sweep_dash") == DID_NOT_RUN
    assert will_produce(cfg, "term_consistency") == RAN
    assert will_produce(cfg, "continuity") == DID_NOT_RUN
    assert will_produce(cfg, "spelling") == RAN


def test_will_produce_says_unknown_for_a_label_it_cannot_place():
    """No positive evidence and no registry to check it against is not a no."""
    cfg = Config()
    assert will_produce(cfg, "brand_new_thing") == UNKNOWN
    assert will_produce(cfg, "brand_new_thing",
                        known_types=_registry_keys()) == UNKNOWN
    # With the registry in hand a configured-but-off type IS determinable.
    cfg.error_types = ["spelling"]
    assert will_produce(cfg, "comma_splice",
                        known_types=_registry_keys()) == DID_NOT_RUN


# --- produced_in -------------------------------------------------------------

def test_findings_outrank_the_config_heuristic():
    """The ordering the original bug got backwards: a count in the artifact is
    proof, and no inference from `config.error_types` may override it."""
    payload = {"config": {"error_types": ["spelling"], "sweeps": []},
               "stats_by_error_type": {"continuity": 3, "sapling": 1}}
    assert produced_in(payload, "continuity") == RAN
    assert produced_in(payload, "sapling") == RAN


@pytest.mark.parametrize("label", sorted(FREE_FORM - {"term_consistency",
                                                      "name_consistency"}))
def test_a_silent_free_form_pass_is_unknown_never_a_no(label):
    """A run that produced nothing under a free-form label, with no telemetry
    to say why. `config.error_types` is populated and excludes the label — as
    it always does — and that must not be read as an answer."""
    payload = {"config": {"error_types": ["spelling", "comma_splice"],
                          "sweeps": []},
               "stats_by_error_type": {"spelling": 4}}
    assert produced_in(payload, label) == UNKNOWN


def test_a_configured_type_is_determinable_from_config():
    payload = {"config": {"error_types": ["spelling"], "sweeps": []},
               "stats_by_error_type": {}}
    assert produced_in(payload, "spelling") == RAN        # ran, found nothing
    assert produced_in(payload, "comma_splice") == DID_NOT_RUN


def test_sweep_telemetry_answers_both_ways():
    payload = {"config": {"error_types": [], "sweeps": ["sweep_dash"]},
               "scripted_checks": [{"key": "sweep_dash", "flagged": 0}],
               "stats_by_error_type": {}}
    # A row at zero flagged is a sweep that ran and found nothing.
    assert produced_in(payload, "sweep_dash") == RAN
    assert produced_in(payload, "sweep_ellipsis") == DID_NOT_RUN


def test_consistency_telemetry_answers_both_ways():
    off = {"consistency": {"ran": False, "terms": [], "names": []},
           "config": {"error_types": [], "sweeps": []}}
    on = {"consistency": {"ran": True, "terms": [], "names": []},
          "config": {"error_types": [], "sweeps": []}}
    assert produced_in(off, "term_consistency") == DID_NOT_RUN
    assert produced_in(on, "name_consistency") == RAN


def test_sapling_bills_only_when_it_ran():
    billed = {"usage": {"sapling_chars": 12000},
              "config": {"error_types": ["spelling"], "sweeps": []}}
    assert produced_in(billed, "sapling") == RAN
    # Zero characters is not proof of absence, so it falls through to UNKNOWN
    # rather than to a no.
    quiet = {"usage": {"sapling_chars": 0},
             "config": {"error_types": ["spelling"], "sweeps": []}}
    assert produced_in(quiet, "sapling") == UNKNOWN


@pytest.mark.parametrize("payload", [
    {},                                                     # nothing at all
    {"config": {"error_types": "not-a-list"}},              # config unreadable
    {"stats_by_error_type": "not-a-mapping"},               # tally unreadable
    {"consistency": {"terms": []},                          # block renamed
     "config": {"error_types": ["spelling"], "sweeps": []}},
])
def test_unreadable_evidence_degrades_to_unknown_not_to_silence(payload):
    """A rename or a truncation upstream must cost the reader certainty, not
    make it confidently wrong — the failure that disabled the total-loss alarm
    was a check that stayed quiet when it should have said it did not know."""
    assert produced_in(payload, "term_consistency") == UNKNOWN


def test_scripted_checks_rows_we_cannot_read_are_unknown():
    payload = {"scripted_checks": [{"name": "Dashes"}],   # no "key"
               "config": {"error_types": [], "sweeps": ["sweep_dash"]}}
    assert produced_in(payload, "sweep_dash") == UNKNOWN


# --- the rejudge taxonomy ----------------------------------------------------

def test_findings_json_rows_carry_only_fields_or_declared_extras(tmp_path):
    """`rejudge.read_run` rebuilds a Finding from a findings.json row, so every
    key in a row must be a Finding field or a known reporting extra. Asserted
    against the writer's own output so a new reporting key shows up here rather
    than as a TypeError under someone's re-judge button."""
    from docproof.models import DocumentModel, ParagraphRef, Usage
    from docproof.reporting import write_findings_json

    finding = Finding(
        finding_id="f-0001", chunk_id="chunk-000", para_id="body-0001",
        error_type="continuity", original_text="A sentence.", occurrence=1,
        corrected_text="A sentence.", explanation="", confidence="high",
        status="query", force_query=True)
    doc = DocumentModel(
        source_path="book.docx",
        paragraphs=(ParagraphRef(para_id="body-0001",
                                 part="word/document.xml", location="body",
                                 text="A sentence.", style="Normal"),))
    out = tmp_path / "findings.json"
    write_findings_json(out, doc=doc, findings=[finding], usage=Usage(),
                        cfg=Config(), applied_ids=())

    rows = json.loads(out.read_text(encoding="utf-8"))["findings"]
    fields = {f.name for f in dataclasses.fields(Finding)}
    extra = {k for row in rows for k in row} - fields
    assert extra == REPORTING_ONLY, (
        f"findings.json rows now carry {sorted(extra)} on top of Finding's "
        f"fields. rejudge.read_run drops them already; update REPORTING_ONLY "
        f"so this test keeps describing the artifact.")


def test_rejudge_survives_an_unknown_reporting_key(tmp_path):
    """The rot this replaces: a key `read_run` had never heard of used to raise
    a TypeError on the next re-judge, for a key that was never a Finding field."""
    from docproof.rejudge import read_run

    row = {**dataclasses.asdict(Finding(
        finding_id="f-0001", chunk_id="c", para_id="p-0001",
        error_type="continuity", original_text="x", occurrence=1,
        corrected_text="x", explanation="", confidence="high")),
        "applied": False, "a_field_invented_next_year": {"nested": 1}}
    (tmp_path / "findings.json").write_text(
        json.dumps({"source": "book.docx", "findings": [row]}),
        encoding="utf-8")

    findings, source = read_run(tmp_path)
    assert source == "book.docx"
    assert [f.error_type for f in findings] == ["continuity"]
