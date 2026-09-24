"""Independent text and formatting checks; model confidence is not evidence."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from difflib import SequenceMatcher


class VerificationError(ValueError):
    pass


def stories(snapshot: dict) -> dict[str, dict]:
    rows = snapshot.get("stories", [])
    if not isinstance(rows, list) or not rows:
        raise VerificationError("InDesign returned no story inventory.")
    result = {str(row["id"]): row for row in rows}
    if len(result) != len(rows):
        raise VerificationError("Duplicate story IDs in the InDesign inventory.")
    return result


def prepare_edits(snapshot: dict, edits: list[dict]) -> tuple[dict, list[dict]]:
    """Resolve every exact occurrence before any mutation and simulate its text."""
    original = stories(snapshot)
    expected = {key: row["text"] for key, row in original.items()}
    resolved: list[dict] = []
    ids: set[str] = set()
    for edit in edits:
        eid = edit.get("id")
        if not isinstance(eid, str) or not eid or eid in ids:
            raise VerificationError("Each edit needs a unique nonempty ID.")
        ids.add(eid)
        sid = str(edit.get("story_id"))
        if sid not in original:
            raise VerificationError(f"{eid}: story does not exist.")
        find = edit.get("find")
        replacement = edit.get("replacement")
        count = edit.get("expected_count", 1)
        if not isinstance(find, str) or not find or not isinstance(replacement, str):
            raise VerificationError(f"{eid}: a nonempty exact anchor and replacement are required.")
        if type(count) is not int or count < 1:
            raise VerificationError(f"{eid}: expected count must be a positive integer.")
        text = original[sid]["text"]
        starts, cursor = [], 0
        while (pos := text.find(find, cursor)) >= 0:
            starts.append(pos)
            cursor = pos + len(find)
        if len(starts) != count:
            raise VerificationError(f"{eid}: expected {count} exact matches, found {len(starts)}.")
        for style in edit.get("style_ranges", []):
            a, b = style.get("start"), style.get("end")
            if (type(a) is not int or type(b) is not int or not 0 <= a < b <= len(replacement)
                    or not style.get("font_style")):
                raise VerificationError(f"{eid}: invalid replacement style range.")
        for pos in starts:
            resolved.append({**deepcopy(edit), "story_id": sid, "start": pos, "end": pos + len(find)})
    for sid in original:
        rows = sorted((r for r in resolved if r["story_id"] == sid), key=lambda r: r["start"])
        for left, right in zip(rows, rows[1:]):
            if left["end"] > right["start"]:
                raise VerificationError(f"Overlapping instructions {left['id']} and {right['id']}.")
        for row in reversed(rows):
            text = expected[sid]
            expected[sid] = text[:row["start"]] + row["replacement"] + text[row["end"]:]
    return expected, resolved


def validate_plan(plan: dict, packet: dict) -> None:
    instructions = plan.get("instructions", [])
    if not instructions:
        raise VerificationError("Astra did not account for any instructions.")
    ids = [r.get("id") for r in instructions]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise VerificationError("Instruction IDs must be unique and nonempty.")
    edits = plan.get("edits", [])
    known_edits = {r.get("id") for r in edits}
    owned = Counter(eid for row in instructions for eid in row.get("edit_ids", []))
    if set(owned) != known_edits or any(count != 1 for count in owned.values()):
        raise VerificationError("Each edit must belong to exactly one source instruction.")
    sources = {str(r["id"]) for r in packet.get("sources", [])}
    used = set()
    for row in instructions:
        refs = row.get("source_ids", [])
        if not refs or not set(refs) <= sources:
            raise VerificationError(f"{row['id']}: missing or unknown source evidence.")
        used.update(refs)
        if row.get("disposition") not in {"edit", "already_correct", "clarification", "designer"}:
            raise VerificationError(f"{row['id']}: invalid disposition.")
        if (row["disposition"] == "edit") != bool(row.get("edit_ids")):
            raise VerificationError(f"{row['id']}: edit disposition disagrees with its edit list.")
    # Packet records explicitly indicate which sources carry instructions;
    # contextual book pages are evidence but do not each require a correction.
    required = {str(r["id"]) for r in packet.get("sources", []) if r.get("requires_disposition", True)}
    if not required <= used:
        raise VerificationError("The correction plan omits submitted evidence: " + ", ".join(sorted(required - used)))
    if packet.get("evidence"):
        from .astra import _required_evidence_ids
        evidence_ids = {r["id"] for r in packet["evidence"] if isinstance(r, dict) and "id" in r}
        covered = Counter(eid for row in instructions for eid in row.get("covered_evidence_ids", []))
        if (not set(covered) <= evidence_ids or any(n != 1 for n in covered.values())
                or not _required_evidence_ids(packet) <= set(covered)):
            raise VerificationError("Every required evidence entry must be accounted for exactly once.")


def _fonts(row: dict) -> list[str | None]:
    result = [None] * len(row["text"])
    for run in row.get("style_ranges", []):
        start, end = run["start"], run["end"]
        result[start:end] = [run.get("font_style")] * (end - start)
    return result


def _formats(row: dict) -> list[tuple | None]:
    result = [None] * len(row["text"])
    for run in row.get("style_ranges", []):
        signature = tuple((key, run[key]) for key in ("font_style", "applied_font", "point_size", "tracking", "paragraph_style") if key in run)
        result[run["start"]:run["end"]] = [signature] * (run["end"] - run["start"])
    return result


def check_saved(baseline: dict, final: dict, edits: list[dict]) -> dict:
    expected, resolved = prepare_edits(baseline, edits)
    after = stories(final)
    failures = []
    if set(expected) != set(after):
        failures.append("The story inventory changed unexpectedly.")
    for sid, text in expected.items():
        if sid not in after or text != after[sid]["text"]:
            failures.append(f"Story {sid} differs from the intended corrected text.")
    integrity_passed = not failures
    before = stories(baseline)
    for sid, old in before.items():
        if sid not in after:
            continue
        rows = sorted((r for r in resolved if r["story_id"] == sid), key=lambda r: r["start"])
        old_fonts, new_fonts = _fonts(old), _fonts(after[sid])
        old_formats, new_formats = _formats(old), _formats(after[sid])
        if len(new_fonts) != len(expected[sid]):
            continue
        delta, cursor = 0, 0
        for row in rows:
            # Check all untouched text formatting, including later shifted text.
            for pos in range(cursor, row["start"]):
                old_style = old_fonts[pos]
                if old_style is not None and old_formats[pos] != new_formats[pos + delta]:
                    failures.append(f"Story {sid}: formatting changed outside correction {row['id']}.")
                    break
            start = row["start"] + delta
            explicit = list(row.get("style_ranges", []))
            if row.get("font_style"):
                explicit = [{"start": 0, "end": len(row["replacement"]), "font_style": row["font_style"]}] + explicit
            # Equal substrings within a replacement must also retain their styles
            # unless the instruction explicitly assigns a new style to them.
            covered = {i for run in explicit for i in range(run["start"], run["end"])}
            for block in SequenceMatcher(None, row["find"], row["replacement"], autojunk=False).get_matching_blocks():
                for offset in range(block.size):
                    old_offset = row["start"] + block.a + offset
                    new_offset = block.b + offset
                    if new_offset not in covered and old_fonts[old_offset] is not None and old_formats[old_offset] != new_formats[start + new_offset]:
                        failures.append(f"{row['id']}: existing formatting was lost inside the replacement.")
                        break
            for style in explicit:
                if any(new_fonts[start + offset] != style["font_style"]
                       for offset in range(style["start"], style["end"])):
                    failures.append(f"{row['id']}: requested {style['font_style']} formatting was not saved.")
            delta += len(row["replacement"]) - (row["end"] - row["start"])
            cursor = row["end"]
        for pos in range(cursor, len(old["text"])):
            old_style = old_fonts[pos]
            if old_style is not None and old_formats[pos] != new_formats[pos + delta]:
                failures.append(f"Story {sid}: formatting changed in unedited text.")
                break
    for font in final.get("fonts", []):
        if str(font.get("status", "")).upper() not in {"INSTALLED", "FONTSTATUS.INSTALLED"}:
            failures.append(f"Unavailable font: {font.get('name', 'unknown')}.")
    for link in final.get("links", []):
        if str(link.get("status", "")).upper() not in {"NORMAL", "LINK_EMBEDDED", "EMBEDDED", "LINKSTATUS.NORMAL", "LINKSTATUS.LINK_EMBEDDED"}:
            failures.append(f"Unresolved artwork: {link.get('name', 'unknown')}.")
    if final.get("overset"):
        failures.append("The corrected document contains overflowing text.")
    return {"passed": not failures, "integrity_passed": integrity_passed, "failures": list(dict.fromkeys(failures)),
            "stories_checked": len(expected), "edits_checked": len(resolved),
            "pages_before": baseline.get("page_count", baseline.get("pages")),
            "pages_after": final.get("page_count", final.get("pages"))}


def _final_offset(resolved: list[dict], story_id: str, offset: int) -> int:
    """Where a baseline story offset sits in the corrected story's text."""
    shift = 0
    for row in resolved:
        if row["story_id"] == story_id and row["end"] <= offset:
            shift += len(row["replacement"]) - (row["end"] - row["start"])
    return offset + shift


def _page_at(story: dict | None, offset: int) -> dict | None:
    """The page of the frame holding `offset` in a saved story, or None when
    the snapshot has no frame map or the text sits in overset."""
    frames = [f for f in (story or {}).get("frames") or [] if isinstance(f, dict)]
    for index, frame in enumerate(frames):
        start, end = frame.get("start"), frame.get("end")
        if type(start) is not int or type(end) is not int:
            continue
        last = index == len(frames) - 1
        if start <= offset < end or (last and offset == end):
            if type(frame.get("page")) is int:
                return {"page": frame["page"], "page_name": str(frame.get("page_name") or frame["page"])}
            return None
    return None


def new_pages(baseline: dict, final: dict, edits: list[dict], plan: dict | None = None) -> dict:
    """Pages of the corrected edition for each edit and each located note.

    Returns ``{"edits": {edit_id: [page, ...]}, "instructions": {id: [page, ...]}}``
    where a page is ``{"page": <1-based PDF page>, "page_name": <InDesign page
    name>}``. Only meaningful once the saved text has been verified against the
    same edits; callers show nothing when it has not.
    """
    _, resolved = prepare_edits(baseline, edits)
    before, after = stories(baseline), stories(final)
    found: dict[str, list[dict]] = {}
    for row in resolved:
        start = _final_offset(resolved, row["story_id"], row["start"])
        page = _page_at(after.get(row["story_id"]), start)
        if page and page not in found.setdefault(row["id"], []):
            found[row["id"]].append(page)
    notes: dict[str, list[dict]] = {}
    for instruction in (plan or {}).get("instructions", []) or []:
        if not isinstance(instruction, dict):
            continue
        iid = str(instruction.get("id", ""))
        pages = []
        for eid in instruction.get("edit_ids", []) or []:
            pages += [p for p in found.get(eid, []) if p not in pages]
        sid, text = str(instruction.get("locate_story_id") or ""), instruction.get("locate_text")
        if not pages and sid in before and isinstance(text, str) and text:
            at = before[sid]["text"].find(text)
            if at >= 0:
                page = _page_at(after.get(sid), _final_offset(resolved, sid, at))
                if page:
                    pages.append(page)
        notes[iid] = pages
    return {"edits": found, "instructions": notes}
