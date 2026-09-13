"""Content identities for the data used by Galley's local proofreading checks.

This is a read-only preflight. It never loads Java, downloads a dictionary, or
lets an enabled check silently substitute an empty data set.
"""
from __future__ import annotations

import hashlib
from importlib import metadata
import json
from pathlib import Path
from threading import Lock


class LocalAssetError(RuntimeError):
    """A required local proofreading data file is missing or unusable."""


_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENTATIONS = (
    "galley/local_assets.py", "docproof/toccheck.py", "docproof/headings.py",
    "docproof/variants.py", "docproof/spellscan.py", "docproof/function_words.py",
    "docproof/agreement.py", "docproof/candidate_models.py",
)
_WORKER_ASSET_IDENTITY: str | None = None
_WORKER_ASSET_LOCK = Lock()


def _file(path: Path, *, required: bool = True) -> tuple[dict, bytes | None]:
    path = path.resolve()
    try:
        data = path.read_bytes()
    except OSError as exc:
        if required:
            raise LocalAssetError(f"Required local proofreading asset is unavailable: {path}") from exc
        return {"path": str(path), "present": False}, None
    if required and not data:
        raise LocalAssetError(f"Required local proofreading asset is empty: {path}")
    return {"path": str(path), "present": True, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}, data


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError as exc:
        raise LocalAssetError(f"Required local proofreading package is unavailable: {name}") from exc


def _dictionary_base(cfg, prepared) -> Path:
    """Mirror spellscan._dictionary's actual resolution, without its cache."""
    try:
        import spylls
    except ImportError as exc:
        raise LocalAssetError("The local spylls dictionary package is unavailable") from exc
    name = cfg.spellcheck.dictionary or prepared.variant.dictionary
    candidate = Path(name)
    return (candidate if candidate.parent != Path(".") else
            Path(spylls.__file__).parent / "hunspell/data/en" / name)


def _english_wordfreq_data() -> Path:
    try:
        import wordfreq
        path = wordfreq.available_languages("best").get("en")
    except ImportError as exc:
        raise LocalAssetError("The local English wordfreq package is unavailable") from exc
    if path is None:
        raise LocalAssetError("The required local English wordfreq data is unavailable")
    return Path(path)


def local_asset_identity(cfg, prepared) -> dict:
    """Fingerprint the installed files actually used by the fixed local pass.

    Re-read content on every request so replacing a dictionary, rule table or
    package cannot reuse a certificate for its previous data. A running worker
    keeps its initial asset identity: legacy prepared evidence and parser caches
    must never be relabelled after hot-swapping their source data. Paths and
    bytes are included explicitly; mtimes and process-local object IDs are not.
    """
    from docproof import consistency, variants

    result = {"packages": {name: _package_version(name) for name in ("spylls", "wordfreq")},
              "dictionary": {}, "consistency": {}, "variants": {}, "implementations": {}}
    dictionary = _dictionary_base(cfg, prepared)
    for extension in ("aff", "dic"):
        result["dictionary"][extension], _ = _file(Path(str(dictionary) + "." + extension))
    result["wordfreq_english"], _ = _file(_english_wordfreq_data())

    variant_rules = bool(cfg.consistency.enabled and (
        cfg.consistency.spelling_variants or cfg.consistency.variant_policy != "off"))
    varcon, raw = _file(consistency._CONSISTENCY_DIR / "varcon.tsv", required=variant_rules)
    if variant_rules:
        try:
            usable = any(len([field for field in line.split("\t") if field.strip()]) >= 2
                         for line in raw.decode("utf-8").splitlines()
                         if line.strip() and not line.lstrip().startswith("#"))
        except UnicodeError as exc:
            raise LocalAssetError("The required local varcon.tsv is not valid UTF-8") from exc
        if not usable:
            raise LocalAssetError("The required local varcon.tsv contains no spelling-variant pairs")
    result["consistency"]["varcon.tsv"] = varcon
    notes_required = variant_rules and cfg.consistency.chicago_notes
    notes, raw = _file(consistency._CONSISTENCY_DIR / "chicago.yaml", required=notes_required)
    if notes_required:
        import yaml
        try:
            parsed = yaml.safe_load(raw)
        except (yaml.YAMLError, UnicodeError) as exc:
            raise LocalAssetError("The required local Chicago consistency notes are malformed") from exc
        if not isinstance(parsed, dict) or not any(isinstance(parsed.get(key), dict) and parsed[key]
                                                 for key in ("classes", "forms")):
            raise LocalAssetError("The required local Chicago consistency notes contain no rule notes")
    result["consistency"]["chicago.yaml"] = notes

    # Auto-variant detection reads both US/UK maps; retain all shipped variants
    # as well as the concrete variant's already-bound runtime value.
    for key in variants.VARIANT_KEYS:
        result["variants"][key], _ = _file(variants._dir() / f"{key}.yaml")
    for name in _IMPLEMENTATIONS:
        result["implementations"][name], _ = _file(_ROOT / name)
    identity = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    global _WORKER_ASSET_IDENTITY
    with _WORKER_ASSET_LOCK:
        if _WORKER_ASSET_IDENTITY is None:
            _WORKER_ASSET_IDENTITY = identity
        elif _WORKER_ASSET_IDENTITY != identity:
            raise LocalAssetError(
                "Local proofreading assets changed inside this worker. Restart the "
                "worker before beginning or resuming a proofread; prepared evidence "
                "and cached dictionaries cannot be reused after an asset change.")
    return result
