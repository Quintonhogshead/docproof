"""Pack a corpus of cases into one .docx the pipeline can review.

One case becomes one body paragraph, in order, with nothing between them. The
walker numbers body paragraphs `body-0000`, `body-0001`, … by position
(`docproof/utils/xml_helpers.py`), so case index *i* lands at `body-{i:04d}` and
the join back to the case is exact. Any blank paragraph would consume an index
and break that, so this writer emits none.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import docx

from .corpus import Case


@dataclass(frozen=True)
class BuiltCorpus:
    docx_path: Path
    para_to_case: dict[str, str]     # "body-0000" -> case id
    cases_by_id: dict[str, Case]

    def case_for(self, para_id: str) -> Case | None:
        cid = self.para_to_case.get(para_id)
        return self.cases_by_id.get(cid) if cid else None


class DocBuildError(ValueError):
    pass


def build_docx(cases: list[Case], out_path: str | Path,
               *, min_paragraph_chars: int = 20) -> BuiltCorpus:
    """Write `cases` to `out_path` and return the para_id→case map.

    Raises DocBuildError on a case the pipeline would silently drop: a paragraph
    shorter than `min_paragraph_chars` is marked non-reviewable at ingest
    (`docproof/ingest.py`), so the model never sees it and it would score as a
    false negative for a reason that has nothing to do with the model. A newline
    inside a case would split it into two paragraphs and desync every id after
    it, so that is refused too."""
    out_path = Path(out_path)
    too_short = [c for c in cases if len(c.text) < min_paragraph_chars]
    if too_short:
        ids = ", ".join(c.id for c in too_short[:8])
        raise DocBuildError(
            f"{len(too_short)} case(s) are shorter than "
            f"min_paragraph_chars={min_paragraph_chars} and the model would "
            f"skip them: {ids}. Lengthen them or the corpus will under-report "
            f"recall.")
    newlined = [c for c in cases if "\n" in c.text]
    if newlined:
        ids = ", ".join(c.id for c in newlined[:8])
        raise DocBuildError(
            f"{len(newlined)} case(s) contain a newline, which splits one case "
            f"across paragraphs and desyncs every id after it: {ids}")

    d = docx.Document()
    para_to_case: dict[str, str] = {}
    for i, case in enumerate(cases):
        d.add_paragraph(case.text)
        para_to_case[f"body-{i:04d}"] = case.id
    out_path.parent.mkdir(parents=True, exist_ok=True)
    d.save(out_path)
    return BuiltCorpus(docx_path=out_path, para_to_case=para_to_case,
                       cases_by_id={c.id: c for c in cases})
