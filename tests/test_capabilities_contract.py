"""P2-9: the `capabilities` manifest must not drift from the real CLI.

The manifest is generated from the argparse objects, so it cannot drift the way
a hand-maintained doc table does — but only as long as it keeps advertising the
real flag STRINGS (not the dests), positionals as positional, and never a flag a
verb does not have. This locks that contract, and the three specific drifts the
Purpura beta hit: `--para-map` (not `para_map`), genre-pack's positional genre,
and `galley state` having no `--note`.
"""
from __future__ import annotations

import json

from docproof.__main__ import main


def _manifest(capsys) -> dict:
    assert main(["capabilities"]) == 0
    return json.loads(capsys.readouterr().out)


def _verb(commands, *path):
    """Descend a command tree by name (e.g. 'galley', 'state')."""
    cur = commands
    node = None
    for name in path:
        node = next((c for c in cur if c["name"] == name), None)
        assert node is not None, f"no such verb: {path}"
        cur = node.get("subcommands", [])
    return node


def _flags(node) -> set[str]:
    out: set[str] = set()
    for a in node.get("args", []):
        out.update(a.get("flags", []))
    return out


def _positionals(node) -> set[str]:
    return {a["name"] for a in node.get("args", []) if a.get("positional")}


def test_inventory_advertises_the_flag_string_not_the_dest(capsys):
    inv = _verb(_manifest(capsys)["commands"], "inventory")
    assert "--para-map" in _flags(inv)           # the flag, verbatim
    # the dest ("para_map") must never appear as an advertised invocation token
    assert "para_map" not in _flags(inv)


def test_genre_pack_genre_is_positional(capsys):
    gp = _verb(_manifest(capsys)["commands"], "galley", "genre-pack")
    assert "genre" in _positionals(gp)
    assert "--genre" not in _flags(gp)            # it is NOT a flag here


def test_galley_state_has_no_note_flag(capsys):
    st = _verb(_manifest(capsys)["commands"], "galley", "state")
    assert "--note" not in _flags(st)
    # --note lives on approve, where it really exists
    ap = _verb(_manifest(capsys)["commands"], "galley", "approve")
    assert "--note" in _flags(ap)


def test_every_advertised_flag_starts_with_a_dash(capsys):
    # A flag string that is really a dest (no leading dash) is the classic drift;
    # every advertised flag across the whole tree must be a real option string.
    def walk(commands):
        for c in commands:
            for a in c.get("args", []):
                for f in a.get("flags", []):
                    assert f.startswith("-"), f"{c['name']}: {f!r} is not a flag"
            walk(c.get("subcommands", []))
    walk(_manifest(capsys)["commands"])
