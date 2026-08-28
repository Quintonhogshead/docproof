"""P2-8: `--stage/--genre` must not re-apply over an already-materialized config.

A genre-pack config bakes its stage+genre into the YAML and stamps a
`# galley: genre=… stage=…` header. Re-applying the flag composes a DIFFERENT
effective config than the pack materialized, so `approve` and `review` disagree
on the config hash and the paid run is refused (exit 5). The provenance-aware
resolver drops a stamped axis identically for every consumer, so the hashes match.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from docproof.__main__ import _effective_cfg, _resolve_stage_genre
from galley.manifest import config_hash

DEFAULT = Path("config/default.yaml").read_text(encoding="utf-8")


def _stamped(tmp_path, header: str) -> str:
    p = tmp_path / "pack.yaml"
    p.write_text(f"# galley: {header}\n" + DEFAULT, encoding="utf-8")
    return str(p)


def test_resolver_drops_a_stamped_axis():
    # matching flag → dropped (already materialized)
    cfg = "config/default.yaml"
    # a hand-written config with no header applies flags unchanged
    assert _resolve_stage_genre(cfg, "mechanical-wave", "literary_memoir") == \
        ("mechanical-wave", "literary_memoir")


def test_resolver_no_ops_the_flag_the_pack_already_baked(tmp_path):
    pack = _stamped(tmp_path, "genre=literary_memoir stage=mechanical-wave")
    # both axes stamped → both flags dropped, whether they match or conflict
    assert _resolve_stage_genre(pack, "mechanical-wave", "literary_memoir") == \
        (None, None)
    assert _resolve_stage_genre(pack, "copyedit-wave", "fantasy_sf") == (None, None)
    # an un-stamped axis still applies
    only_genre = _stamped(tmp_path, "genre=literary_memoir")
    assert _resolve_stage_genre(only_genre, "mechanical-wave", "literary_memoir") \
        == ("mechanical-wave", None)


def test_approve_and_review_hash_match_on_a_materialized_pack(tmp_path):
    pack = _stamped(tmp_path, "genre=literary_memoir")
    # approve passes the flag; a later review might not (or vice-versa) — both
    # must land on the same effective config now that the stamped axis no-ops.
    with_flag = _effective_cfg(SimpleNamespace(
        config=pack, stage=None, genre="literary_memoir"))
    without_flag = _effective_cfg(SimpleNamespace(
        config=pack, stage=None, genre=None))
    assert config_hash(with_flag) == config_hash(without_flag)
