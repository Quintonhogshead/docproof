"""The names lane: catch a HubSpot Project whose author name is wrong or
blank before it breaks DocWatch's folder match or the native worker's
identity guarantee.

Four spellings of the same author, compared with `app.watch.names.name_key`
(accents and case flattened, same as the rest of the repo): HubSpot's own
first/last properties, the Drive author-subfolder name, the byline DocWatch
read off the manuscript's first pages, and the name on a corrections-form
submission. Pure — no I/O, reads only the already-collected snapshot.

Verdicts, from `docs/monitoring-agent-plan.md` ("The names lane"):
  agree     every source that has an opinion agrees. No Finding.
  fill      HubSpot is blank; every other present source agrees with each
            other. Tier 0 — safe to write.
  propose   HubSpot disagrees, but the folder and the byline agree with each
            other. Tier 1 — ask before writing.
  conflict  anything else: genuine disagreement, or nothing to compare
            HubSpot against at all. Tier 2 — report only, never write.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.warden.rules import Finding

AGREE, FILL, PROPOSE, CONFLICT = "agree", "fill", "propose", "conflict"


@dataclass(frozen=True)
class NameFinding:
    project_id: str
    hubspot_first: str
    hubspot_last: str
    folder_name: str
    byline: str
    form_name: str
    verdict: str
    proposal: dict = field(default_factory=dict)
    tier: int = 2
    evidence: dict = field(default_factory=dict)

    def as_finding(self) -> Finding | None:
        """The `Finding` a non-`agree` verdict produces, or `None`."""
        if self.verdict == AGREE:
            return None
        severity = {FILL: "low", PROPOSE: "medium", CONFLICT: "high"}.get(self.verdict, "medium")
        verbs = {
            FILL: ("hubspot-fill-name",),
            PROPOSE: ("hubspot-set-name",),
            CONFLICT: (),
        }.get(self.verdict, ())
        summary = _summary(self)
        return Finding(rule="hubspot-name-suspect", key=self.project_id,
                       severity=severity, summary=summary, evidence=self.evidence,
                       verbs=verbs, tier=self.tier)


def as_findings(name_findings: list[NameFinding]) -> list[Finding]:
    """Every non-`agree` `NameFinding`, as a `Finding` `rules.evaluate()`
    would have produced itself."""
    out = []
    for item in name_findings:
        finding = item.as_finding()
        if finding is not None:
            out.append(finding)
    return out


def _summary(item: NameFinding) -> str:
    who = (f"{item.hubspot_first} {item.hubspot_last}".strip()
          or f"Project {item.project_id}")
    if item.verdict == FILL:
        first, last = item.proposal.get("first", ""), item.proposal.get("last", "")
        return (f"HubSpot's name is blank for Project {item.project_id}; the "
                f"folder and byline agree on {first} {last}.".strip())
    if item.verdict == PROPOSE:
        first, last = item.proposal.get("first", ""), item.proposal.get("last", "")
        return (f"HubSpot says {who}, but the folder and byline agree on "
                f"{first} {last} instead.")
    return (f"The author name on Project {item.project_id} does not "
           f"resolve: HubSpot says {who!r}, the folder says "
           f"{item.folder_name!r}, the byline says {item.byline!r}.")


def _key_pair(first: str, last: str, name_key):
    first_key, last_key = name_key(first or ""), name_key(last or "")
    if not first_key and not last_key:
        return None
    return (first_key, last_key)


def _folder_name_parts(subfolder_name: str) -> tuple[str, str]:
    """"Last, F." (or a bare "Last") -> (first, last). Falls back to
    splitting on whitespace ("First Last") when there is no comma, since a
    flat install's subfolder may carry either convention."""
    name = (subfolder_name or "").strip()
    if not name:
        return "", ""
    if "," in name:
        last, _, rest = name.partition(",")
        return rest.strip().rstrip(".").strip(), last.strip()
    parts = name.split()
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return "", name


def _split_byline(byline: str) -> tuple[str, str]:
    """A manuscript byline ("Jordan Casey" or "Casey, Jordan") -> (first, last)."""
    text = (byline or "").strip()
    if not text:
        return "", ""
    if "," in text:
        last, _, rest = text.partition(",")
        return rest.strip(), last.strip()
    parts = text.split()
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return "", text


def compare(snapshot: dict) -> list[NameFinding]:
    """One `NameFinding` per Project touched this tick (or since the last
    cursor — whatever `hubspot.projects` already holds), including the
    `agree` ones, so a test or a weekly report can see the whole picture."""
    from app.watch.names import name_key

    hubspot = snapshot.get("hubspot")
    hubspot = hubspot if isinstance(hubspot, dict) else {}
    docwatch = snapshot.get("docwatch")
    docwatch = docwatch if isinstance(docwatch, dict) else {}

    projects = hubspot.get("projects") or []
    files = docwatch.get("files")
    if not isinstance(files, list):
        files = (docwatch.get("watch") or {}).get("files")
    files = files if isinstance(files, list) else []
    submissions = hubspot.get("form_submissions") or []

    files_by_project: dict[str, dict] = {}
    for rec in files:
        if not isinstance(rec, dict):
            continue
        pid = str(rec.get("hubspot_id") or "")
        if pid and pid not in files_by_project:
            files_by_project[pid] = rec

    forms_by_project: dict[str, dict] = {}
    for row in submissions:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("project_id") or "")
        if pid and pid not in forms_by_project:
            forms_by_project[pid] = row

    results: list[NameFinding] = []
    for project in projects:
        if not isinstance(project, dict):
            continue
        pid = str(project.get("id") or "")
        if not pid:
            continue

        hs_first = str(project.get("author_first") or "")
        hs_last = str(project.get("author_last") or "")
        hs_key = _key_pair(hs_first, hs_last, name_key)

        rec = files_by_project.get(pid, {})
        subfolder_name = str(rec.get("subfolder_name") or "")
        folder_first, folder_last = _folder_name_parts(subfolder_name)
        file_first = str(rec.get("author_first") or "") or folder_first
        file_last = str(rec.get("author_last") or "") or folder_last
        folder_key = _key_pair(file_first, file_last, name_key)

        byline = str(rec.get("byline") or "")
        byline_first, byline_last = _split_byline(byline)
        byline_key = _key_pair(byline_first, byline_last, name_key)

        form = forms_by_project.get(pid, {})
        form_first = str(form.get("firstname") or "")
        form_last = str(form.get("lastname") or "")
        form_key = _key_pair(form_first, form_last, name_key)

        other_keys = [k for k in (folder_key, byline_key, form_key) if k is not None]

        verdict, tier, proposal = AGREE, 0, {}
        if hs_key is None:
            if other_keys and all(k == other_keys[0] for k in other_keys):
                verdict, tier = FILL, 0
                proposal = {"first": file_first or byline_first or form_first,
                           "last": file_last or byline_last or form_last}
            elif other_keys:
                verdict, tier = CONFLICT, 2
            # else: nothing anywhere to compare — leave as agree (no data).
        elif not other_keys or all(k == hs_key for k in other_keys):
            verdict = AGREE
        elif (folder_key is not None and byline_key is not None
              and folder_key == byline_key and folder_key != hs_key):
            verdict, tier = PROPOSE, 1
            proposal = {"first": file_first or byline_first,
                       "last": file_last or byline_last}
        else:
            verdict, tier = CONFLICT, 2

        evidence = {
            "hubspot": {"first": hs_first, "last": hs_last},
            "folder": {"first": file_first, "last": file_last,
                      "subfolder_name": subfolder_name},
            "byline": {"first": byline_first, "last": byline_last, "raw": byline},
            "form": {"first": form_first, "last": form_last},
        }
        results.append(NameFinding(
            project_id=pid, hubspot_first=hs_first, hubspot_last=hs_last,
            folder_name=subfolder_name, byline=byline,
            form_name=f"{form_first} {form_last}".strip(), verdict=verdict,
            proposal=proposal, tier=tier, evidence=evidence))

    return results
