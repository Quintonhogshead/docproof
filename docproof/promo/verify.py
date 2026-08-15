"""Grounding checks for generated promo copy — that the copy says only what the
book bears out.

Two of them, cheap to dear. `verify_grounding` is deterministic and free: a
capitalised term in the copy that appears NOWHERE in the manuscript is almost
always invented (a whole novel is a large enough sample of English that nearly
every ordinary word appears in it somewhere), so those terms are flagged.
`verify_claims` is the stronger, opt-in pass: a second model call that reads the
book and the copy and reports which factual claims the manuscript does not
support. Neither edits and neither blocks — both surface for a human to check.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ValidationError

from ..config import Config
from ..models import Usage
from ..providers import Provider, strict_json_schema
from .ingest import Manuscript
from .model import PromoResult

if TYPE_CHECKING:                     # avoids a cycle: pipeline imports verify
    from .pipeline import PreparedPromo

log = logging.getLogger("docproof.promo.verify")


def _resolve(config_dir: str | Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(config_dir) / path

# A word that could be a name: starts with a letter, at least three long. The
# apostrophe and hyphen keep "O'Rourke" and "Marie-Claire" in one piece.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’-]{2,}")

# Capitalised openers and pronouns that are not names. Most are caught anyway by
# appearing lowercased somewhere in a full novel; this is a small belt-and-braces
# list for the handful that might not ("Meanwhile" opening the only sentence
# that uses it), so they never reach the flag list.
_STOPWORDS = frozenset("""
The A An And But Or Nor For Yet So As At By In If It Of On To Up We You He She
They This That These Those Then There Here When Where While Because Although
Though After Before Once Now Never Always Meanwhile However Suddenly Finally
Perhaps Maybe Yes No Not Will Would Could Should Their His Her Its Our Your My
""".split())


def verify_grounding(manuscript: Manuscript,
                     result: PromoResult) -> list[str]:
    """Capitalised terms in the teaser and post bodies that appear nowhere in
    the manuscript, de-duplicated, in first-seen order. Hashtags are excluded —
    they are coined on purpose."""
    vocab = {m.group(0).lower() for m in _TOKEN.finditer(manuscript.text)}
    generated = [result.teaser] + [p.text for p in result.posts]
    flagged: list[str] = []
    seen: set[str] = set()
    for text in generated:
        for m in _TOKEN.finditer(text):
            word = m.group(0)
            if not word[0].isupper() or word in _STOPWORDS:
                continue
            key = word.lower()
            if key in vocab or key in seen:
                continue
            seen.add(key)
            flagged.append(word)
    return flagged


# --- the model-based entailment pass ------------------------------------------

class ClaimCheck(BaseModel):
    claim: str
    supported: bool
    note: str = ""


class _ClaimReport(BaseModel):
    claims: list[ClaimCheck]


def _copy_text(result: PromoResult) -> str:
    lines = [f"Teaser: {result.teaser}"]
    for i, post in enumerate(result.posts, start=1):
        lines.append(f"Post {i}: {post.text}")
    return "\n".join(lines)


def _load_prompt(path: Path) -> tuple[str, str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not raw.get("system_prompt") or not raw.get("user_template"):
        raise ValueError(f"{path}: system_prompt and user_template are required.")
    return str(raw["system_prompt"]).strip(), str(raw["user_template"])


def verify_claims(cfg: Config, prepared: "PreparedPromo", result: PromoResult,
                  provider: Provider, usage: Usage, *, config_dir: str | Path,
                  override_dir: str | Path | None = None) -> list[ClaimCheck]:
    """Ask a model which of the copy's claims the manuscript does not support.

    A second large-context call — the whole book again — so it is only run when
    `promo.verify_claims` is on. It never blocks: a call that refuses or comes
    back malformed is logged and treated as "nothing to flag", because a
    grounding check that fails must not fail the copy it was checking. Token cost
    is folded into `usage` so the run's bill is whole."""
    source = _resolve(config_dir, cfg.promo.verify_prompt)
    if override_dir and (Path(override_dir) / source.name).is_file():
        source = Path(override_dir) / source.name
    try:
        system, template = _load_prompt(source)
    except (OSError, ValueError) as e:
        log.warning("Skipping the claim check: %s", e)
        return []

    user = template.replace("{copy}", _copy_text(result))
    user = user.replace("{manuscript}", prepared.manuscript.text)
    response = provider.complete_structured(
        model=cfg.api.model, system=system, user=user,
        schema=strict_json_schema(_ClaimReport), schema_name="promo_grounding",
        max_tokens=cfg.api.max_output_tokens)
    usage.add(response.usage, model=cfg.api.model)
    if response.stop_reason != "ok" or response.parsed is None:
        log.warning("The claim check did not answer (%s); nothing flagged.",
                    response.error or response.stop_reason)
        return []
    try:
        report = _ClaimReport.model_validate(response.parsed)
    except ValidationError as e:
        log.warning("The claim check's answer did not parse (%s); nothing "
                    "flagged.", e)
        return []
    unsupported = [c for c in report.claims if not c.supported]
    if unsupported:
        log.warning("Grounding: %d claim(s) the manuscript does not support.",
                    len(unsupported))
    return report.claims
