#!/usr/bin/env python3
"""Generate injected-error eval cases by mechanically corrupting clean prose.

DEV TOOLING ONLY. This script is not shipped in the wheel and is never imported
by the pipeline; it exists so the *injected* tier of the accuracy corpus
(docs/accuracy-eval-plan.md, the three tiers table) can be mass-produced instead
of hand-written. The five injected types are the mechanical ones whose ground
truth is exact: spelling, repeated_word, homophone_confusion, apostrophe_error,
capitalization.

The idea (accuracy-eval-plan.md:57): take a clean, correct sentence, apply a
KNOWN corruption, and the expected correction is the *exact inverse* of that
corruption. Because the source is clean, restoring the original is provably the
right fix — that is what makes injected ground truth trustworthy without a human
grading every case. The one assumption is that the source prose is genuinely
clean and correct; feed it clean public-domain text and that holds.

Out of the box it runs on eval/tools/sample_public_domain.txt (a small committed
corpus of genuine public-domain lines plus freshly-composed clean sentences, all
IP-clean and shareable) so the tool is runnable with no network and no arguments.
To scale the corpus to plan size, download real Project Gutenberg plain-text
files, strip the header/footer boilerplate, break them into one clean sentence
or short paragraph per line (>= min_paragraph_chars, no embedded newlines — see
docproof/eval/docbuilder.py), and point --input at that file. Nothing here
reaches the network; fetching Gutenberg is left to the operator on purpose.

Determinism: this is an ordinary offline Python script, so there is no
Date.now()/random() reproducibility concern — but we still avoid randomness
entirely. Selection is purely positional: sentences are processed in file order
and the first N that admit a given corruption become that type's cases. Re-running
on the same input yields byte-identical output. --start offsets the scan
(index-based) if you want a different, still-deterministic slice.

Typical use:

    # regenerate the committed sample output for all five injected types
    python eval/tools/generate_injected.py

    # a real Gutenberg file, 30 spelling cases + 8 clean traps, into eval/cases/
    python eval/tools/generate_injected.py \
        --input pride_and_prejudice.txt --type spelling \
        --limit 30 --clean 8 --out-dir eval/cases

Output files are named <error_type>.yaml and match the corpus schema
(docproof/eval/corpus.py) exactly, so load_corpus/check_no_leakage accept them.
By default they land in eval/cases/injected/, a subdirectory the default
load_corpus glob does NOT pick up (it is non-recursive), which keeps the
generated tier from colliding with the hand-curated top-level cases. Point
--out-dir at eval/cases to fold a generated batch into the live corpus; the ids
are namespaced (<prefix>-inj-NNN, <prefix>-clean-NNN) so they never clash with
the authored <prefix>-NNN / <prefix>-1NN ids.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

# One short id prefix per injected type, matching the authored files' scheme.
PREFIX = {
    "spelling": "sp",
    "repeated_word": "rw",
    "homophone_confusion": "hp",
    "apostrophe_error": "ap",
    "capitalization": "cp",
}
INJECTED_TYPES = tuple(PREFIX)

# --- corruption ingredients --------------------------------------------------

# correct spelling -> a canonical misspelling. The misspellings are all
# non-words, so the injected token is unambiguously wrong and the inverse (the
# key) is the only correct restoration. Inflected forms are listed so common
# classic-prose words actually get hit.
MISSPELL = {
    "beginning": "begining", "always": "allways", "believe": "beleive",
    "believed": "beleived", "government": "goverment", "business": "buisness",
    "necessary": "neccessary", "different": "diferent", "beautiful": "beautifull",
    "surprise": "suprise", "receive": "recieve", "received": "recieved",
    "until": "untill", "occasion": "ocasion", "which": "wich", "friend": "freind",
    "address": "adress", "argument": "arguement", "calendar": "calender",
    "independent": "independant", "tomorrow": "tommorow", "separate": "seperate",
    "definitely": "definately", "immediately": "imediately", "really": "realy",
}

# Function words that are safe to double as an accidental repeat. Deliberately
# excludes words whose doubling can be grammatical ("that that", "had had",
# "will will"), which are traps, not errors.
SAFE_DOUBLE = {"the", "a", "of", "to", "and", "in", "on", "for", "with",
               "she", "he", "they", "from", "by"}

# The correct member (in clean prose) -> the wrong homophone it is swapped for.
# Each swap yields a genuine error given a correct source.
HOMOPHONE = {
    "its": "it's", "it's": "its", "their": "there", "they're": "their",
    "you're": "your", "than": "then", "too": "to", "lose": "loose",
}

# Contractions -> the same word with the apostrophe dropped. Restricted to ones
# that become clear non-words (no "wont"/"cant"/"lets", which are real words,
# and no it's/you're, which homophone_confusion owns).
CONTRACTION = {
    "don't": "dont", "isn't": "isnt", "didn't": "didnt", "doesn't": "doesnt",
    "wasn't": "wasnt", "weren't": "werent", "couldn't": "couldnt",
    "wouldn't": "wouldnt", "shouldn't": "shouldnt", "haven't": "havent",
    "hasn't": "hasnt", "hadn't": "hadnt",
}

MONTHDAY = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}

_TOKEN = re.compile(r"^([^A-Za-z]*)([A-Za-z](?:[A-Za-z']*[A-Za-z])?)([^A-Za-z]*)$")


def _split(tok: str) -> tuple[str, str, str] | None:
    """A token into (leading punctuation, alphabetic core, trailing). Returns
    None for tokens with no alphabetic core (e.g. "2/3")."""
    m = _TOKEN.match(tok)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def _match_case(template: str, word: str) -> str:
    """Give `word` the leading-capitalization of `template`."""
    if template[:1].isupper():
        return word[:1].upper() + word[1:]
    return word


def _window(tokens: list[str], i: int) -> tuple[int, int]:
    """A 2-token span window around index i, so a one-word change is locatable
    even when the changed word (its/to/then/i) recurs in the sentence."""
    return (i - 1, i) if i > 0 else (i, i + 1)


# --- per-type corruptions ----------------------------------------------------
# Each returns (corrupted_text, span, correction) or None if inapplicable. The
# span is always a verbatim substring of corrupted_text; the correction is the
# local corrected fragment.

def corrupt_spelling(tokens: list[str]):
    for i, tok in enumerate(tokens):
        parts = _split(tok)
        if not parts:
            continue
        pre, core, post = parts
        bad = MISSPELL.get(core.lower())
        if bad is None:
            continue
        bad = _match_case(core, bad)
        new = tokens[:]
        new[i] = pre + bad + post
        return " ".join(new), bad, core
    return None


def corrupt_repeated_word(tokens: list[str]):
    for i, tok in enumerate(tokens):
        if i >= len(tokens) - 1:
            break
        parts = _split(tok)
        if not parts:
            continue
        pre, core, post = parts
        if pre or post:            # only double a clean, punctuation-free word
            continue
        if core.lower() not in SAFE_DOUBLE:
            continue
        new = tokens[:i + 1] + [tok] + tokens[i + 1:]
        return " ".join(new), f"{core} {core}", core
    return None


def corrupt_homophone(tokens: list[str]):
    for i, tok in enumerate(tokens):
        parts = _split(tok)
        if not parts:
            continue
        pre, core, post = parts
        wrong = HOMOPHONE.get(core.lower())
        if wrong is None:
            continue
        wrong = _match_case(core, wrong)
        new = tokens[:]
        new[i] = pre + wrong + post
        lo, hi = _window(new, i)
        span = " ".join(new[lo:hi + 1])
        corr = tokens[:]           # the original window, with the right word
        correction = " ".join(corr[lo:hi + 1])
        return " ".join(new), span, correction
    return None


def corrupt_apostrophe(tokens: list[str]):
    for i, tok in enumerate(tokens):
        parts = _split(tok)
        if not parts:
            continue
        pre, core, post = parts
        key = core.lower().replace("’", "'")
        bad = CONTRACTION.get(key)
        if bad is None:
            continue
        bad = _match_case(core, bad)
        new = tokens[:]
        new[i] = pre + bad + post
        return " ".join(new), bad, _match_case(core, key)
    return None


def corrupt_capitalization(tokens: list[str]):
    # Rule A: a standalone pronoun "I" lowercased.
    for i, tok in enumerate(tokens):
        parts = _split(tok)
        if parts and parts[1] == "I":
            pre, _, post = parts
            new = tokens[:]
            new[i] = pre + "i" + post
            lo, hi = _window(new, i)
            corr = tokens[:]
            return (" ".join(new), " ".join(new[lo:hi + 1]),
                    " ".join(corr[lo:hi + 1]))
    # Rule B: a month or weekday lowercased.
    for i, tok in enumerate(tokens):
        parts = _split(tok)
        if not parts:
            continue
        pre, core, post = parts
        if core.lower() in MONTHDAY and core[:1].isupper():
            new = tokens[:]
            new[i] = pre + core.lower() + post
            lo, hi = _window(new, i)
            corr = tokens[:]
            return (" ".join(new), " ".join(new[lo:hi + 1]),
                    " ".join(corr[lo:hi + 1]))
    # Rule C: the sentence-initial word lowercased.
    parts = _split(tokens[0])
    if parts and parts[1] != "I" and parts[1][:1].isupper() and len(parts[1]) > 1:
        pre, core, post = parts
        new = tokens[:]
        new[0] = pre + core[0].lower() + core[1:] + post
        lo, hi = _window(new, 0)
        corr = tokens[:]
        return (" ".join(new), " ".join(new[lo:hi + 1]),
                " ".join(corr[lo:hi + 1]))
    return None


CORRUPTORS = {
    "spelling": corrupt_spelling,
    "repeated_word": corrupt_repeated_word,
    "homophone_confusion": corrupt_homophone,
    "apostrophe_error": corrupt_apostrophe,
    "capitalization": corrupt_capitalization,
}


# --- driver ------------------------------------------------------------------

def read_sentences(path: Path, min_chars: int) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if len(s) < min_chars or "\n" in s:
            continue
        lines.append(s)
    return lines


def _yq(s: str) -> str:
    """Double-quote a scalar for YAML."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_cases(etype: str, sentences: list[str], *, limit: int, clean: int,
                start: int) -> list[dict]:
    corrupt = CORRUPTORS[etype]
    prefix = PREFIX[etype]
    ordered = sentences[start:] + sentences[:start]

    cases: list[dict] = []
    used: set[int] = set()
    n = 0
    for idx, sent in enumerate(ordered):
        if n >= limit:
            break
        tokens = sent.split()
        result = corrupt(tokens)
        if result is None:
            continue
        text, span, correction = result
        if span not in text or len(text) < 20:
            continue                      # never emit a case the loader rejects
        n += 1
        used.add(idx)
        cases.append({
            "id": f"{prefix}-inj-{n:03d}", "text": text,
            "span": span, "correction": correction, "confidence": "high",
        })

    c = 0
    for idx, sent in enumerate(ordered):
        if c >= clean:
            break
        if idx in used or len(sent) < 20:
            continue
        c += 1
        cases.append({"id": f"{prefix}-clean-{c:03d}", "text": sent,
                      "clean": True})
    return cases


def write_file(etype: str, cases: list[dict], out_dir: Path) -> Path:
    out = out_dir / f"{etype}.yaml"
    lines = [
        "# GENERATED by eval/tools/generate_injected.py — do not hand-edit.",
        "# Injected tier (docs/accuracy-eval-plan.md): clean prose mechanically",
        "# corrupted; each correction is the exact inverse of the injected edit.",
        "# Hand-curated adversarial traps for this type live in the top-level",
        f"# eval/cases/{etype}.yaml, not here.",
        f"error_type: {etype}",
        "cases:",
    ]
    for c in cases:
        lines.append(f"  - id: {c['id']}")
        lines.append(f"    text: {_yq(c['text'])}")
        if c.get("clean"):
            lines.append("    expect: clean")
        else:
            lines.append(
                f"    expect: {{ span: {_yq(c['span'])}, "
                f"correction: {_yq(c['correction'])}, "
                f"confidence: {c['confidence']} }}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv=None) -> int:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path,
                    default=here / "sample_public_domain.txt",
                    help="clean prose, one sentence/paragraph per line")
    ap.add_argument("--type", choices=("all",) + INJECTED_TYPES, default="all",
                    help="which injected type to generate (default: all five)")
    ap.add_argument("--out-dir", type=Path,
                    default=here.parent / "cases" / "injected",
                    help="where the <type>.yaml files are written")
    ap.add_argument("--limit", type=int, default=6,
                    help="max seeded-error cases per type (default: 6)")
    ap.add_argument("--clean", type=int, default=3,
                    help="clean trap cases per type (default: 3)")
    ap.add_argument("--start", type=int, default=0,
                    help="positional offset into the sentence list (deterministic)")
    ap.add_argument("--min-chars", type=int, default=25,
                    help="skip source lines shorter than this (default: 25)")
    args = ap.parse_args(argv)

    sentences = read_sentences(args.input, args.min_chars)
    if not sentences:
        ap.error(f"no usable source lines in {args.input}")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    types = INJECTED_TYPES if args.type == "all" else (args.type,)
    for etype in types:
        cases = build_cases(etype, sentences, limit=args.limit,
                            clean=args.clean, start=args.start)
        seeded = sum(1 for c in cases if not c.get("clean"))
        traps = sum(1 for c in cases if c.get("clean"))
        out = write_file(etype, cases, args.out_dir)
        print(f"{etype}: {seeded} seeded + {traps} clean -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
