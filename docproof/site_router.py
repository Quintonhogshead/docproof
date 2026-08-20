"""Pure routing decisions for uncertain or high-risk site verdicts."""
from __future__ import annotations

from .site_models import ExaminationSite, Verdict


def route(verdict: Verdict, site: ExaminationSite, *,
          stronger_judge_available: bool = True) -> str:
    """Return ``accept``, ``escalate``, or ``query`` without mutating state."""
    if verdict.decision in {"pass", "error"}:
        if (verdict.decision == "error"
                and site.meaning_change_risk == "high"
                and verdict.confidence != "high"):
            return "escalate" if stronger_judge_available else "query"
        return "accept"
    if verdict.decision == "defer" or site.meaning_change_risk != "low":
        return "escalate" if stronger_judge_available else "query"
    return "query"
