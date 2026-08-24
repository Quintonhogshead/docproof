#!/usr/bin/env python3
"""Generate ``config/consistency/varcon.tsv`` — the spelling-variant equivalence
table the consistency engine reads.

Each output line is one *cluster*: a tab-separated set of interchangeable
spellings of the exact same word and inflection, all lowercase, the American
(Merriam-Webster) form first as the cluster id. ``colour``/``color`` is one
cluster; ``colours``/``colors`` is a separate one, because a book can be
consistent on the singular and slip on the plural.

The engine only ever *asks* about a cluster the manuscript writes more than one
way — it never enforces one variant over another (that is the variant
respell-map's job). So the table's only correctness requirement is that the two
members really are the same word: a cluster that pairs two *different* words by
sense (``story``/``storey``, ``check``/``cheque``) would turn a real distinction
into noise, and those are excluded by name in ``_EXCLUDED`` below.

How the table is built, and why this is a script and not a hand-typed file:

  * The regular British/American classes (-our/-or, -ise/-ize, -re/-er,
    -ce/-se, -ogue/-og, doubled-l) are generated from a curated list of
    American base words by a per-class rule, then the American side of every
    generated form is checked against the en_US Hunspell dictionary. Real words
    survive (``colorful`` → ``colourful``); rule over-reach is pruned
    (``colorable`` is not a word, so no ``colourable`` cluster ships). The
    British side is derived, never dictionary-checked — it is by definition not
    American.
  * The irregular pairs (``fetus``/``foetus``, ``aluminum``/``aluminium``,
    ``toward``/``towards``) are listed explicitly.

Run it from the repo root with a Python that has ``spylls`` installed:

    python tools/build_varcon.py

Pass ``--varcon PATH`` to also fold in an upstream VarCon file (the SCOWL
project's, http://wordlist.aspell.net/varcon/) for fuller coverage; every folded
cluster is still run past ``_EXCLUDED`` and the dictionary prune. Without it, the
curated seed below ships — a few hundred of the highest-value clusters.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

OUT = pathlib.Path(__file__).resolve().parent.parent / "config" / "consistency" / "varcon.tsv"


# --- clusters that look like variants but are different words -----------------
# Never emit a cluster containing any of these forms. Each line is a real
# British/American pair a naive scan would fire on, where one member carries a
# sense the other does not; a deterministic check cannot tell the senses apart,
# so it must not ask. Grows with a corpus example in the commit that adds a row.
_EXCLUDED = frozenset("""
story storey stories storeys
check cheque checks cheques
curb kerb curbs kerbs
draft draught drafts draughts
tire tyre tires tyres
meter metre meters metres
program programme programs programmes
practice practise practices practises
license licence licenses licences
advice advise
prize prise prizes prises
device devise
ton tonne tons tonnes
whisky whiskey whiskies whiskeys
councilor councillor councilors councillors
dependent dependant dependents dependants
analyses paralyses catalyses
""".split())
# analyses/paralyses double as the noun plurals of analysis/paralysis — standard
# in American prose — so the -yze/-yse verb clusters for them are dropped whole.


# --- regular classes: American base words + a per-class British rule ----------

# -our / -or.  British keeps the u before these suffixes; it drops it before
# -ous/-ary/-ific/-ious, which are therefore NOT in the list.
_OUR_BASES = """
color honor favor flavor labor neighbor harbor rumor vapor odor savor valor
vigor armor endeavor splendor tumor clamor candor parlor rigor demeanor fervor
""".split()
_OUR_SUFFIXES = ["", "s", "ed", "ing", "ful", "less", "able", "er", "ers",
                 "ite", "hood", "ly", "ation", "ations"]


def _our(base: str):                      # color -> colour
    br = base[:-2] + "our"
    return [(base + s, br + s) for s in _OUR_SUFFIXES]


# -ize / -ise (and -izer, -ization).  British swaps the z for an s.
_IZE_BASES = """
organize realize recognize apologize criticize emphasize memorize minimize
maximize summarize categorize characterize civilize dramatize economize
energize fertilize generalize harmonize hospitalize hypnotize idealize immunize
industrialize legalize liberalize mobilize modernize nationalize normalize
optimize patronize penalize prioritize publicize randomize rationalize
revolutionize scrutinize specialize stabilize standardize sterilize symbolize
sympathize synchronize synthesize theorize urbanize utilize vandalize visualize
vaporize colonize apologize
""".split()
_IZE_SUFFIXES = ["ize", "izes", "ized", "izing", "izer", "izers",
                 "ization", "izations"]


def _ize(base: str):                      # organize -> organise
    stem = base[:-3]
    return [(stem + s, stem + s.replace("iz", "is")) for s in _IZE_SUFFIXES]


# -yze / -yse.
_YSE_BASES = "analyze paralyze catalyze".split()
_YSE_SUFFIXES = ["yze", "yzes", "yzed", "yzing"]


def _yse(base: str):
    stem = base[:-3]
    return [(stem + s, stem + s.replace("yz", "ys")) for s in _YSE_SUFFIXES]


# -er / -re.  Only the bare form and its plural: the -ed/-ing British inflections
# drop an e (centred, centring) and are left to the explicit list if wanted.
_RE_BASES = """
center fiber liter caliber saber somber specter scepter luster meager ocher
theater
""".split()


def _re(base: str):                       # center -> centre
    br = base[:-2] + "re"
    return [(base, br), (base + "s", br + "s")]


# -se / -ce (nouns only; the noun/verb pairs like practice/practise are excluded).
_CE_BASES = "defense offense pretense".split()


def _ce(base: str):                       # defense -> defence
    br = base[:-2] + "ce"
    return [(base, br), (base + "s", br + "s")]


# -og / -ogue.
_OGUE_BASES = "catalog dialog analog monolog epilog".split()


def _ogue(base: str):                     # catalog -> catalogue
    return [(base, base + "ue"), (base + "s", base + "ues")]


# Doubled-l: American single l before a vowel suffix, British double.
_LL_BASES = """
travel cancel label model marvel signal total fuel level jewel quarrel counsel
funnel channel shovel grovel panel pedal tunnel chisel gravel revel spiral
tinsel dial
""".split()
_LL_SUFFIXES = ["ed", "ing", "er", "ers", "or", "ors"]


def _ll(base: str):                       # travel -> travelled
    return [(base + s, base + "l" + s) for s in _LL_SUFFIXES]


_GENERATORS = [(_OUR_BASES, _our), (_IZE_BASES, _ize), (_YSE_BASES, _yse),
               (_RE_BASES, _re), (_CE_BASES, _ce), (_OGUE_BASES, _ogue),
               (_LL_BASES, _ll)]


# --- irregular pairs, listed in full -----------------------------------------
# (american, british).  Inflections that matter are their own rows.
_EXPLICIT: list[tuple[str, str]] = [
    # ae / oe
    ("encyclopedia", "encyclopaedia"), ("medieval", "mediaeval"),
    ("fetus", "foetus"), ("fetal", "foetal"), ("estrogen", "oestrogen"),
    ("anesthetic", "anaesthetic"), ("anesthesia", "anaesthesia"),
    ("pediatric", "paediatric"), ("pediatrics", "paediatrics"),
    ("archeology", "archaeology"), ("anemia", "anaemia"), ("anemic", "anaemic"),
    ("leukemia", "leukaemia"), ("orthopedic", "orthopaedic"),
    ("diarrhea", "diarrhoea"), ("esophagus", "oesophagus"),
    ("gynecology", "gynaecology"), ("hemorrhage", "haemorrhage"),
    ("fecal", "faecal"), ("feces", "faeces"), ("maneuver", "manoeuvre"),
    ("maneuvers", "manoeuvres"),
    # directional -ward(s) and other adverb pairs
    ("toward", "towards"), ("forward", "forwards"), ("backward", "backwards"),
    ("upward", "upwards"), ("downward", "downwards"),
    ("afterward", "afterwards"), ("inward", "inwards"), ("outward", "outwards"),
    ("among", "amongst"), ("amid", "amidst"), ("while", "whilst"),
    # -ed / -t irregular preterites
    ("dreamed", "dreamt"), ("burned", "burnt"), ("leaped", "leapt"),
    ("spilled", "spilt"), ("smelled", "smelt"), ("learned", "learnt"),
    ("spelled", "spelt"), ("leaned", "leant"), ("spoiled", "spoilt"),
    # single elemental / misc
    ("aluminum", "aluminium"), ("sulfur", "sulphur"), ("sulfuric", "sulphuric"),
    ("gray", "grey"), ("grays", "greys"), ("grayed", "greyed"),
    ("graying", "greying"), ("grayish", "greyish"),
    ("plow", "plough"), ("plows", "ploughs"), ("plowed", "ploughed"),
    ("mold", "mould"), ("molds", "moulds"), ("molded", "moulded"),
    ("molt", "moult"), ("smolder", "smoulder"), ("cozy", "cosy"),
    ("donut", "doughnut"), ("donuts", "doughnuts"), ("omelet", "omelette"),
    ("yogurt", "yoghurt"), ("ax", "axe"), ("jail", "gaol"),
    ("skeptic", "sceptic"), ("skeptical", "sceptical"),
    ("skepticism", "scepticism"), ("cozier", "cosier"),
    # -ize nouns/adjs the verb rule does not reach
    ("cozy", "cosy"),
    ("mustache", "moustache"), ("pajamas", "pyjamas"), ("airplane", "aeroplane"),
    ("theater", "theatre"), ("theaters", "theatres"),
    ("counselor", "counsellor"), ("counselors", "counsellors"),
    ("jewelry", "jewellery"),
]


def _load_dictionary():
    try:
        import spylls
        from spylls.hunspell import Dictionary
    except ImportError:
        sys.exit("spylls is required to build varcon.tsv: pip install spylls")
    path = pathlib.Path(spylls.__file__).parent / "hunspell" / "data" / "en" / "en_US"
    return Dictionary.from_files(str(path))


def _parse_upstream(path: pathlib.Path) -> list[tuple[str, ...]]:
    """Very small VarCon reader: each non-comment line is 'word: A-tag / B-tag'.
    We keep only the American (A) and British (B) variants of a cluster and drop
    the tags. Unknown formats are skipped rather than guessed at."""
    clusters: list[tuple[str, ...]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        forms: list[str] = []
        for chunk in line.split("/"):
            parts = chunk.split(":")
            word = (parts[-1] if len(parts) > 1 else parts[0]).strip()
            word = word.split()[0] if word else ""
            if word.isalpha():
                forms.append(word.lower())
        forms = list(dict.fromkeys(forms))
        if len(forms) >= 2:
            clusters.append(tuple(forms))
    return clusters


def build(upstream: pathlib.Path | None = None) -> list[tuple[str, ...]]:
    dic = _load_dictionary()
    clusters: list[tuple[str, ...]] = []

    def add(american: str, british: str) -> None:
        american, british = american.lower(), british.lower()
        if american == british:
            return
        clusters.append((american, british))

    for bases, gen in _GENERATORS:
        for base in bases:
            for american, british in gen(base):
                if dic.lookup(american):       # prune rule over-reach
                    add(american, british)
    for american, british in _EXPLICIT:
        add(american, british)
    if upstream is not None:
        for cluster in _parse_upstream(upstream):
            clusters.append(tuple(c.lower() for c in cluster))

    # Dedup by member set; drop any cluster touching an excluded homograph or
    # whose members are not all distinct; assert no form lands in two clusters.
    seen_sets: set[frozenset[str]] = set()
    by_form: dict[str, tuple[str, ...]] = {}
    final: list[tuple[str, ...]] = []
    for cluster in clusters:
        members = tuple(dict.fromkeys(cluster))
        if len(members) < 2:
            continue
        s = frozenset(members)
        if s & _EXCLUDED or s in seen_sets:
            continue
        clash = [m for m in members if m in by_form and by_form[m] != members]
        if clash:
            raise SystemExit(
                f"form {clash[0]!r} would land in two clusters: "
                f"{by_form[clash[0]]} and {members}")
        seen_sets.add(s)
        for m in members:
            by_form[m] = members
        final.append(members)

    final.sort(key=lambda c: (c[0], c[1:]))
    return final


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--varcon", type=pathlib.Path,
                    help="optional upstream VarCon file to fold in")
    ap.add_argument("--out", type=pathlib.Path, default=OUT)
    args = ap.parse_args()

    clusters = build(args.varcon)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        fh.write("# Generated by tools/build_varcon.py — do not edit by hand.\n")
        fh.write("# One cluster per line: interchangeable spellings of one "
                 "word, American form first.\n")
        for cluster in clusters:
            fh.write("\t".join(cluster) + "\n")
    print(f"wrote {len(clusters)} clusters to {args.out}")


if __name__ == "__main__":
    main()
