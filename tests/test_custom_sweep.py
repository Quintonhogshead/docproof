"""Bespoke sweep rules (docproof/custom_sweep.py) and the run_sweep_objects seam.

A rule the agent writes for one book must load, run through the same machinery as
the shipped sweeps, and — the safety half — be rejected up front when it is
malformed or would fire forever.
"""

from __future__ import annotations

import json

import pytest

from docproof.custom_sweep import RuleError, load_rule
from docproof.models import ParagraphRef
from docproof.sweeps import Sweep, run_sweep_objects, run_sweeps


def _doc(*texts: str) -> list[ParagraphRef]:
    return [ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t,
                         "Normal") for i, t in enumerate(texts)]


def _rule(tmp_path, body: str, name: str = "rule.yaml"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# --- declarative regex rules --------------------------------------------------

def test_regex_rule_loads_and_scans(tmp_path):
    rule = _rule(tmp_path,
                 "key: doubled_quotes\n"
                 "name: Doubled quotes\n"
                 "pattern: '([\u201c\u201d])\\1+'\n"
                 "replacement: '\\1'\n")
    sweep = load_rule(rule)
    assert isinstance(sweep, Sweep)
    assert sweep.key == "doubled_quotes"
    hits = sweep.scan("\u201c\u201cHello,\u201d\u201d she said.")
    # Two runs collapsed: the opening \u201c\u201c and the closing \u201d\u201d.
    assert len(hits) == 2
    assert all(h.replacement in "\u201c\u201d" for h in hits)


def test_json_rule_parses(tmp_path):
    rule = _rule(tmp_path, json.dumps({
        "key": "brk", "name": "Scene break",
        "pattern": r"~{3,}", "replacement": "* * *",
    }), name="rule.json")
    sweep = load_rule(rule)
    hits = sweep.scan("Some text.\n~~~~\nMore text.")
    assert len(hits) == 1
    assert hits[0].replacement == "* * *"


def test_zero_width_and_identity_matches_are_dropped(tmp_path):
    # A replacement equal to what it matched is a no-op and must not be filed.
    rule = _rule(tmp_path,
                 "key: noop\npattern: 'the'\nreplacement: 'the'\n")
    sweep = load_rule(rule)
    assert sweep.scan("the cat sat on the mat") == []


def test_empty_match_pattern_is_rejected(tmp_path):
    rule = _rule(tmp_path, "key: bad\npattern: 'a*'\nreplacement: 'x'\n")
    with pytest.raises(RuleError, match="empty string"):
        load_rule(rule)


def test_numeric_backref_out_of_range_is_rejected(tmp_path):
    rule = _rule(tmp_path,
                 "key: bad\npattern: '(a)'\nreplacement: '\\2'\n")
    with pytest.raises(RuleError, match=r"group \\2"):
        load_rule(rule)


def test_named_backref_unknown_is_rejected(tmp_path):
    rule = _rule(tmp_path,
                 "key: bad\npattern: '(?P<x>a)'\nreplacement: '\\g<y>'\n")
    with pytest.raises(RuleError, match="does not define"):
        load_rule(rule)


def test_named_backref_known_is_accepted(tmp_path):
    rule = _rule(tmp_path,
                 "key: ok\npattern: '(?P<x>a)'\nreplacement: '[\\g<x>]'\n")
    sweep = load_rule(rule)
    assert sweep.scan("banana")  # matches the a's, wraps each


def test_unknown_flag_is_rejected(tmp_path):
    rule = _rule(tmp_path,
                 "key: bad\npattern: 'a'\nreplacement: 'b'\nflags: [wobble]\n")
    with pytest.raises(RuleError, match="unknown flag"):
        load_rule(rule)


def test_ignorecase_flag_applies(tmp_path):
    rule = _rule(tmp_path,
                 "key: ok\npattern: 'cat'\nreplacement: 'dog'\n"
                 "flags: [ignorecase]\n")
    sweep = load_rule(rule)
    assert len(sweep.scan("Cat CAT cat")) == 3


def test_bad_key_is_rejected(tmp_path):
    rule = _rule(tmp_path,
                 "key: 'Not A Key'\npattern: 'a'\nreplacement: 'b'\n")
    with pytest.raises(RuleError, match="key"):
        load_rule(rule)


def test_uncompilable_pattern_is_rejected(tmp_path):
    rule = _rule(tmp_path, "key: bad\npattern: '('\nreplacement: 'x'\n")
    with pytest.raises(RuleError, match="does not compile"):
        load_rule(rule)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(RuleError, match="no such rule"):
        load_rule(tmp_path / "nope.yaml")


# --- python escape hatch ------------------------------------------------------

def test_python_rule_with_sweep_object(tmp_path):
    mod = tmp_path / "r.py"
    mod.write_text(
        "from docproof.sweeps import Hit, Sweep\n"
        "def scan(text, variant=None):\n"
        "    return [Hit(0, 1, 'X', 'why')] if text else []\n"
        "SWEEP = Sweep('pykey', 'Py rule', scan)\n",
        encoding="utf-8")
    sweep = load_rule(mod)
    assert sweep.key == "pykey"
    assert sweep.scan("abc")[0].replacement == "X"


def test_python_rule_with_scan_and_metadata(tmp_path):
    mod = tmp_path / "r.py"
    mod.write_text(
        "from docproof.sweeps import Hit\n"
        "KEY = 'pk'\nNAME = 'P'\n"
        "def scan(text, variant=None):\n"
        "    return []\n",
        encoding="utf-8")
    sweep = load_rule(mod)
    assert sweep.key == "pk" and sweep.name == "P"


def test_python_rule_without_sweep_or_scan_is_rejected(tmp_path):
    mod = tmp_path / "r.py"
    mod.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(RuleError, match="neither SWEEP nor a scan"):
        load_rule(mod)


def test_python_rule_import_error_is_a_rule_error(tmp_path):
    mod = tmp_path / "r.py"
    mod.write_text("import does_not_exist_anywhere\n", encoding="utf-8")
    with pytest.raises(RuleError, match="failed to import"):
        load_rule(mod)


# --- the engine seam ----------------------------------------------------------

def test_run_sweep_objects_produces_s_series_findings(tmp_path):
    sweep = load_rule(_rule(tmp_path,
                            "key: brk\npattern: '~{3,}'\nreplacement: '* * *'\n"))
    paras = _doc("A scene.\n~~~\nAnother scene.", "No break here.")
    findings, reports = run_sweep_objects(paras, [sweep])
    assert len(findings) == 1
    assert findings[0].finding_id.startswith("s-")
    assert findings[0].error_type == "brk"
    assert reports[0].flagged == 1 and reports[0].remaining == 0


def test_run_sweep_objects_measures_non_idempotence(tmp_path):
    # 'o' -> 'oo' still matches 'o' afterwards, so remaining must be non-zero.
    sweep = load_rule(_rule(tmp_path,
                            "key: bloat\npattern: 'o'\nreplacement: 'oo'\n"))
    _, reports = run_sweep_objects(_doc("room too soon"), [sweep])
    assert reports[0].flagged > 0
    assert reports[0].remaining > 0


def test_run_sweeps_still_delegates_identically(tmp_path):
    # The built-in path must be unchanged: run_sweeps == run_sweep_objects(resolve).
    paras = _doc("He went to to the door... What?!")
    a, ra = run_sweeps(paras, ["sweep_doubled_word", "sweep_ellipsis"])
    from docproof.sweeps import resolve
    b, rb = run_sweep_objects(paras, resolve(["sweep_doubled_word",
                                              "sweep_ellipsis"]))

    def _proj(f):
        return (f.finding_id, f.para_id, f.error_type, f.original_text,
                f.corrected_text, f.occurrence)

    assert [_proj(f) for f in a] == [_proj(f) for f in b]
    assert [(r.key, r.flagged, r.remaining) for r in ra] == \
           [(r.key, r.flagged, r.remaining) for r in rb]
