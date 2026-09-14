"""Deterministic reading aids for the extracted press method; never edits."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import re


# A hyphen between a capitalized-or-lowercase left part and a lowercase right
# part, standing alone: the shape of a line-break hyphen the export left
# inside a word (Cala-veras) and, equally, of an ordinary compound (well-known).
_SEAM = re.compile(r"(?<![\w-])([A-Za-z][a-z]+)-([a-z]{2,})(?![\w-])")


def focused_checks(paragraphs, *, knows=None):
    """`knows(word) -> bool | None` is the spelling dictionary; without it the
    seam-hyphen check is skipped and its count reads 0."""
    from docproof.tensecheck import profile
    from docproof.candidate_generators import _quote_candidates
    from docproof.sweeps import _dialogue_tag_re, REPORTING_VERBS

    sites, matrix = [], Counter()
    words = Counter(w.casefold() for p in paragraphs for w in re.findall(r"[A-Za-z]+", p.text))
    def site(kind, p, start, end, detail):
        body = {"check": kind, "para_id": p.para_id, "start": start, "end": end,
                "quote": p.text[start:end], "detail": detail}
        body["id"] = "press-" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:20]
        sites.append(body)

    for p in paragraphs:
        for m in _dialogue_tag_re('"”’\'').finditer(p.text):
            if m.group("subject").lower() not in {"he", "she", "they", "we", "it", "you"}:
                continue
            if m.group("verb").lower() not in REPORTING_VERBS:
                continue
            mark = m.group("inner") or m.group("outer") or "none"
            order = "quote_then_mark" if m.group("outer") else "mark_then_quote"
            case = "capitalized" if m.group("subject")[0].isupper() else "lowercase"
            matrix[f"{order}/{mark}/{case}"] += 1
            site("dialogue_matrix", p, m.start(), m.end(),
                 f"{order}; {mark}; {case}. Judge reporting verb versus independent action beat.")
        # Deliberately broad: judgment distinguishes a genuine final list item
        # from apposition, direct address, two verbs and already-correct commas.
        for m in re.finditer(r"\b(?:and|or|nor)\b", p.text, re.I):
            boundary = max(p.text.rfind(mark, 0, m.start()) for mark in ".!?;\n") + 1
            if "," in p.text[boundary:m.start()]:
                site("serial_comma", p, m.start(), m.end(),
                     "A comma precedes this conjunction in the clause; judge true list versus non-list.")
        for c in _quote_candidates(p):
            a = c.anchors[0]
            if a.start_offset is not None and a.end_offset is not None:
                site("quotation_integrity", p, a.start_offset, a.end_offset,
                     "Check neighbouring paragraphs and multi-paragraph speech before changing quotes.")
        if knows is not None:
            for m in _SEAM.finditer(p.text):
                left, right = m.group(1), m.group(2)
                left_known, right_known = knows(left), knows(right)
                if left_known and right_known:
                    continue                  # an ordinary compound of real words
                joined = left + right
                unhyphenated = words[joined.casefold()]
                if knows(joined) or unhyphenated:
                    why = (f"'{joined}' occurs {unhyphenated} time(s) unhyphenated in the book" if unhyphenated
                           else f"'{joined}' is a dictionary word") + f"; '{left if not left_known else right}' is not"
                elif not left_known and not right_known:
                    # Neither half is a word: the shape of a proper noun broken
                    # at a line end (Cala-veras), which no dictionary can vouch for.
                    why = f"neither '{left}' nor '{right}' is a dictionary word"
                else:
                    continue
                site("seam_hyphen", p, m.start(), m.end(),
                     f"Possible line-break hyphen: {why}. Judge against deliberate hyphenation, dialect and variant.")

    tense = profile(paragraphs).to_json()
    by_id = {p.para_id: p for p in paragraphs}
    for row in tense["paragraphs"]:
        p = by_id[row["para_id"]]
        site("narrative_tense", p, 0, min(120, len(p.text)),
             f"Narration-only heuristic: {row['verdict']}; past signals={row['past']}, present={row['present']}. Not a verdict on authorial intent.")
    counts = dict(Counter(s["check"] for s in sites))
    for name in ("dialogue_matrix", "serial_comma", "quotation_integrity", "narrative_tense", "seam_hyphen"):
        counts.setdefault(name, 0)
    # Explicit zero cells make an omitted punctuation/case combination visible.
    cells = {f"{order}/{mark}/{case}": matrix[f"{order}/{mark}/{case}"]
             for order in ("mark_then_quote", "quote_then_mark")
             for mark in (",", ".", "?", "!", "…", "none")
             for case in ("lowercase", "capitalized")}
    return {"paragraph_ids": [p.para_id for p in paragraphs], "sites": sites,
            "counts": counts, "dialogue_matrix": cells, "tense_profile": tense}


def book_map(paragraphs, is_heading_style):
    """A COMPLETE inventory of the current book's structure for the final
    readers: every heading (by style, by chapter-title shape, or a short
    all-capitals line) with the count of body paragraphs it governs, and every
    non-empty header/footer paragraph (running heads), so a running head
    CHAPTER ONE can be compared with body headings CHAPTER 2 to 18, and a
    TOP TEN heading with the nine paragraphs under it."""
    from docproof.continuity import looks_like_chapter_heading
    from docproof.headings import is_structural_heading

    def caps_line(p):
        t = p.text.strip()
        return (0 < len(t) <= 60 and t.upper() == t and any(c.isalpha() for c in t)
                and not t.endswith((".", "?", "!")))

    headings, before_first, total = [], 0, 0
    for p in paragraphs:
        if p.location != "body" or not p.text.strip():
            continue
        signal = ("style" if is_structural_heading(p, is_heading_style) else
                  "chapter_title" if looks_like_chapter_heading(p) else
                  "caps_line" if caps_line(p) else None)
        if signal:
            headings.append({"id": p.para_id, "text": p.text.strip(), "style": p.style,
                             "signal": signal, "body_paragraphs": 0})
        else:
            total += 1
            if headings:
                headings[-1]["body_paragraphs"] += 1
            else:
                before_first += 1
    running = [{"id": p.para_id, "part": p.part, "location": p.location, "text": p.text.strip()}
               for p in paragraphs if p.location in {"header", "footer"} and p.text.strip()]
    return {"complete_inventory": True, "headings": headings, "headers_footers": running,
            "body_paragraphs_before_first_heading": before_first, "total_body_paragraphs": total,
            "note": ("Every current heading and every non-empty header/footer paragraph, in order. "
                     "A caps_line heading is a shape guess; style and chapter_title are established.")}


def citation_context(paragraphs):
    """Current whole-book references/citation-bearing paragraphs, with locations.

    The regex inventory is explicitly heuristic. Readers may compare actual
    counterparts, but absence from this index cannot prove a missing reference.
    """
    from docproof.continuity import looks_like_chapter_heading
    reference_section = False
    rows = []
    for p in paragraphs:
        label = p.text.strip().casefold().rstrip(":")
        if label in {"references", "bibliography", "works cited", "reference list", "works consulted"}:
            reference_section = True
        elif looks_like_chapter_heading(p) or (p.style.startswith("Heading") and len(p.text) < 120):
            reference_section = False
        cited = bool(re.search(r"\([^)]*\b(?:1[5-9]|20)\d{2}[a-z]?\b[^)]*\)|\[\d+(?:[\s,–-]+\d+)*\]|\b(?:see\s+)?(?:chapter|figure|table)\s+\d+", p.text, re.I))
        if reference_section or cited or looks_like_chapter_heading(p):
            rows.append({"id": p.para_id, "text": p.text, "part": p.part,
                         "reference_section": reference_section, "citation_or_pointer": cited})
    return {"complete_reference_inventory": False,
            "limitation": "Pattern-selected current passages, not proof of bibliography completeness. Compare explicit counterparts; never infer absence solely from this index.",
            "paragraphs": rows}


def source_formatting(pkg):
    """Conservative effective-roman evidence, including inherited italic styles.

    Any inherited italic toggle is unknown, rather than guessed roman. Direct
    run formatting is unambiguous. Complex field/text containers remain unknown.
    """
    from docproof.utils.xml_helpers import qn, walk_package, paragraph_text
    styles = {}
    defaults = []
    if pkg.has("word/styles.xml"):
        root = pkg.tree("word/styles.xml")
        styles = {s.get(qn("w:styleId")): s for s in root.findall(qn("w:style"))}
        defaults = root.findall(f"{qn('w:docDefaults')}/{qn('w:rPrDefault')}/{qn('w:rPr')}/{qn('w:i')}")
    def enabled(node):
        return node is not None and node.get(qn("w:val"), "true") not in {"0", "false", "off"}
    def inherited_italic(key):
        seen = set()
        while key:
            if key in seen or key not in styles:
                return True
            seen.add(key)
            style = styles[key]
            if enabled(style.find(f"{qn('w:rPr')}/{qn('w:i')}")):
                return True
            parent = style.find(qn("w:basedOn"))
            key = parent.get(qn("w:val")) if parent is not None else None
        return False
    result = {}
    default_style = next((key for key, s in styles.items() if s.get(qn("w:type")) == "paragraph"
                          and s.get(qn("w:default")) == "1"), None)
    for p in walk_package(pkg):
        text = paragraph_text(p.element)
        marks, joined = [], ""
        pstyle = p.element.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
        pkey = pstyle.get(qn("w:val")) if pstyle is not None else default_style
        for run in p.element.findall(qn("w:r")):
            # Complex inline containers cannot be claimed as known roman text.
            if any(c.tag not in {qn("w:rPr"), qn("w:t"), qn("w:footnoteRef"), qn("w:endnoteRef"),
                                 qn("w:footnoteReference"), qn("w:endnoteReference")} for c in run):
                joined = "\0"
                break
            fragment = "".join(t.text or "" for t in run.findall(qn("w:t")))
            props = run.find(qn("w:rPr"))
            italic = props.find(qn("w:i")) if props is not None else None
            rstyle = props.find(qn("w:rStyle")) if props is not None else None
            state = enabled(italic) if italic is not None else (
                None if any(enabled(n) for n in defaults) or inherited_italic(pkey)
                or (rstyle is not None and inherited_italic(rstyle.get(qn("w:val")))) else False)
            marks.extend([state] * len(fragment))
            joined += fragment
        result[p.para_id] = marks if joined == text else [None] * len(text)
    return result


def current_formatting(original, current, source_marks, approved_formats):
    """Map unchanged text and accepted italic proposals onto a fresh snapshot."""
    from difflib import SequenceMatcher
    result = {}
    for pid, text in current.items():
        marks = [None] * len(text)
        for tag, i, j, a, b in SequenceMatcher(a=original[pid], b=text, autojunk=False).get_opcodes():
            if tag == "equal":
                marks[a:b] = source_marks.get(pid, [None] * len(original[pid]))[i:j]
        for f in approved_formats:
            if f["para_id"] != pid or f["format"] != "italic":
                continue
            for tag, i, j, a, b in SequenceMatcher(a=f["snapshot"], b=text, autojunk=False).get_opcodes():
                if tag == "equal" and i <= f["start"] < f["end"] <= j:
                    marks[a + f["start"] - i:a + f["end"] - i] = [True] * (f["end"] - f["start"])
        ranges = []
        start = 0
        for end in range(1, len(text) + 1):
            if end == len(text) or marks[end] is not marks[start]:
                ranges.append({"start": start, "end": end, "italic": marks[start]})
                start = end
        result[pid] = ranges
    return result


def final_audit(prepared, paragraphs, cfg, *, knows=None):
    """Raw signals on the actual final text, never virtual post-fix zeroes."""
    from galley.fixed_local import _house_findings, _normalize_and_structure
    from galley.fixed_policy import extract_numbers
    focused = focused_checks(paragraphs, knows=knows)
    findings, reports = _house_findings(paragraphs, prepared, cfg)
    counts = Counter(f.error_type for f in findings)
    for key in cfg.sweeps:
        counts.setdefault(key, 0)
    normalization = Counter(row["source"] for row in _normalize_and_structure(paragraphs, prepared))
    for key in ("local:normalization", "local:speaker_boundary"):
        counts[key] = normalization[key]
    counts["number_inventory"] = len(extract_numbers({p.para_id: p.text for p in paragraphs}))
    return {"paragraph_ids": [p.para_id for p in paragraphs],
            "raw_signal_counts": dict(counts), "focused_counts": focused["counts"],
            "dialogue_matrix": focused["dialogue_matrix"], "tense_profile": focused["tense_profile"],
            "source_parts": dict(Counter(p.part for p in paragraphs)),
            "interpretation": "Final text pattern counts, not remaining proven errors. Valid exceptions and deliberate text can still match. No final LanguageTool/model rerun is claimed."}
