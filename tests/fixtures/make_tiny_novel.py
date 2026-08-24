"""Deterministic generator for the Galley fixture book.

Builds `tiny_novel.docx`: a ~3-chapter, ~2000-word tiny novel with a set of
PLANTED, CATALOGED errors — a repeated word, a comma splice, a missing serial
comma, an its/it's homophone, a Kathryn/Katherine name drift, and a "teh"
spelling typo. The catalog of what is planted where lives in
`tiny_novel.manifest.json` (the answer key later Galley tickets test recall
against); this module is the single source of truth for both files.

Determinism: the prose is literal text — no randomness, no timestamps, no
seeds — so extracting the paragraph texts yields byte-identical strings on
every run. The document's core properties are pinned to fixed values so the
package is as reproducible as python-docx allows.

Run directly (`python make_tiny_novel.py`) to (re)write the .docx and the
manifest next to this file, or import `paragraph_texts()` / `MANIFEST` /
`build()` for the text and the answer key without touching disk.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import docx

HERE = Path(__file__).parent
DOCX = HERE / "tiny_novel.docx"
MANIFEST_PATH = HERE / "tiny_novel.manifest.json"

HEADING_STYLE = "Heading 1"
BODY_STYLE = "Normal"

# A fixed instant for the package's core properties. Never "now" — a timestamp
# would make the build non-reproducible.
_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)


# --- The manuscript -------------------------------------------------------
#
# Each chapter is (heading_text, [body_paragraph, ...]). Planted-error
# sentences are embedded verbatim inside body paragraphs; the manifest quotes
# those exact substrings. All punctuation is plain ASCII so the quotes survive
# docproof normalization (which straightens quotes and dashes) with stable
# offsets.

CHAPTERS: list[tuple[str, list[str]]] = [
    (
        "Chapter One",
        [
            "Kathryn had lived in the valley her whole life, and she knew "
            "every bend of the river and every stubborn fence that leaned "
            "against the wind. The mornings came slowly there, unspooling "
            "gray light across the fields until the frost gave up its hold "
            "and the grass turned green again.",

            # E1 — repeated word "the the"
            "She walked to the the window and looked out at the rain, "
            "counting the drops as they gathered and slid down the glass. "
            "The orchard beyond the yard was a smudge of dark branches, "
            "patient and unmoving in the wet morning air.",

            "Her father had built the house with his own hands, board by "
            "board, in the years before she was born. He used to say that a "
            "house was only as honest as the people who slept in it, and "
            "Kathryn had carried that plain sentence with her for as long "
            "as she could remember.",

            # E2 — comma splice
            "The manuscript was finished, nobody wanted to read it. She had "
            "set the pages on the kitchen table three days ago and had not "
            "found the nerve to open them since, as though the ink itself "
            "might rise up and accuse her of some small dishonesty.",

            "By noon the rain had thinned to a mist, and the valley smelled "
            "of turned earth and woodsmoke. Kathryn pulled on her coat, "
            "laced her boots, and stepped out into a world washed clean, "
            "determined to walk until her thoughts settled into something "
            "she could use.",

            "The lane was soft underfoot and quiet, and she met no one on "
            "the long walk to the crossroads. A single crow watched her "
            "from the top of a fence post, unbothered, turning its head as "
            "she passed as if it had been expecting her all along.",

            "There was a particular stillness to the valley in the hour "
            "after rain, a held breath before the ordinary noise of the day "
            "resumed. Kathryn had come to depend on it over the years, the "
            "way a person comes to depend on the sound of a clock they no "
            "longer consciously hear. It steadied her when little else did.",

            "She thought about her father often on walks like this one. He "
            "had been a quiet man, given to long silences that other people "
            "mistook for coldness, and it had taken her most of her "
            "childhood to learn that his silences were a kind of "
            "attention rather than an absence of it. He noticed everything "
            "and said almost none of it aloud.",

            "The crossroads had not changed in her lifetime, four rough "
            "lanes meeting at a leaning signpost whose paint had long since "
            "weathered away. As a girl she had believed the signpost marked "
            "the center of the world, and some stubborn part of her had "
            "never entirely surrendered the idea, even now.",

            "She stood there for a while, letting the last of the mist "
            "settle on her shoulders, and considered whether to turn back "
            "or press on toward the town. In the end the decision made "
            "itself, the way such decisions usually did, and she found her "
            "feet already carrying her up the rising road before she had "
            "quite chosen to go.",

            "The years had taught her to distrust grand plans and to trust "
            "small motions instead, and so she did not try to picture the "
            "whole of the day ahead. She would walk to the town, and see "
            "what the town had to show her, and let the next thing follow "
            "from the last, and somewhere in all that ordinary forward "
            "motion the answer she needed might quietly present itself.",

            "Behind her the farmhouse grew small, and the smoke from its "
            "chimney stood almost straight in the settling air, and if she "
            "had turned to look she might have seen it as a stranger would, "
            "a snug and enviable place set down among the green. But she "
            "did not turn. She had learned long ago that a home is best "
            "loved from the inside, and she kept her eyes on the road.",
        ],
    ),
    (
        "Chapter Two",
        [
            "The road out of the valley climbed for a mile before it "
            "leveled, and from the top she could see the whole town laid "
            "out below her, its rooftops dark with rain. She had not been "
            "down there since the autumn, and the distance made it look "
            "smaller than she remembered.",

            # E3 — missing serial comma
            "He packed his boots, a lantern and a knife for the journey, "
            "moving through the cabin with the unhurried care of a man who "
            "had done this many times before. Everything he owned fit into "
            "a single canvas bag, and that was the way he liked it.",

            # E4 — homophone its/it's
            "The town had lost it's charm over the long winter, and the "
            "shops along the main street stood shuttered and gray. What had "
            "once been a lively market square was now a stretch of empty "
            "cobblestones, swept by a wind that carried the smell of the "
            "distant sea.",

            "Kathryn found the bakery still open, its windows warm with "
            "light, and she stepped inside to shake the rain from her coat. "
            "The baker nodded at her without a word, as though no time at "
            "all had passed since she last stood in his doorway.",

            "She bought a loaf and a small paper twist of sugared almonds, "
            "and she ate them slowly at the corner table while the ovens "
            "ticked and cooled behind her. It was the first thing that had "
            "felt ordinary in weeks, and she was grateful for the plainness "
            "of it.",

            "Outside, the clouds had begun to break, and a thin blade of "
            "sun cut across the wet stones. She lingered a while longer, "
            "watching the light move, and then she gathered her things and "
            "set off again toward the far edge of the town.",

            "The main street ran straight for a quarter mile before it bent "
            "toward the harbor, and she walked it slowly, reading the faded "
            "names above the shuttered doors. Each one belonged to someone "
            "she had known, or to the children of someone she had known, "
            "and the accumulation of all those absences pressed on her more "
            "heavily than she had expected.",

            "A dog trotted out from an alley and fell into step beside her "
            "for a block, companionable and unhurried, before it lost "
            "interest and wandered off after some private errand of its "
            "own. She missed its company at once, which struck her as a "
            "foolish thing to feel about a stray she had not even touched.",

            "At the end of the street the sea opened out flat and iron-gray "
            "under the breaking clouds, and the sight of it stopped her the "
            "way it always had. However much a person thought they had "
            "grown used to the sea, she decided, it kept a portion of "
            "itself in reserve, ready to surprise them all over again.",

            "She sat on the low harbor wall and watched the boats swing at "
            "their moorings, and for a long while she thought about nothing "
            "in particular, which was itself a rare and welcome thing. The "
            "cold came up slowly through the stone, and only when her hands "
            "began to ache did she rise and turn back toward the road home.",

            "A fisherman was mending a net on the far quay, his hands moving "
            "in the old patient rhythm, and she watched him for a minute "
            "from across the water. There was a dignity in work like that, "
            "she thought, work with a clear beginning and a clear end and "
            "nothing hidden in it, and she envied him it more than she "
            "would have cared to admit to anyone.",

            "The walk back out of the town felt shorter than the walk in, "
            "as return journeys always do, and by the time she reached the "
            "top of the rise the afternoon had opened into a pale and "
            "steady brightness. The valley lay ahead of her, green and "
            "washed and waiting, and she went down into it with a lighter "
            "step than she had climbed out.",
        ],
    ),
    (
        "Chapter Three",
        [
            # E5 — name drift Kathryn -> Katherine
            "Katherine smiled at the memory of that first morning, when the "
            "whole valley had seemed to hold its breath. The name her "
            "mother had chosen felt strange to her now, worn smooth by "
            "years of being called something shorter and easier by everyone "
            "she knew.",

            # E6 — spelling typo "teh"
            "He opened teh door and stepped into the cold, pulling his "
            "collar up against the wind that came knifing off the water. "
            "The lantern swung from his hand and threw long shadows that "
            "leaned and stretched across the frozen ground.",

            "They met at the old stone bridge, the way they always had, "
            "and for a moment neither of them spoke. The river ran black "
            "and fast beneath them, swollen with the season's rain, and its "
            "voice filled the silence they could not.",

            "Kathryn told him about the manuscript then, the whole tangled "
            "story of it, and he listened without interrupting until she "
            "had said everything she had come to say. When she finished, he "
            "was quiet for a long time, watching the water.",

            "In the end he told her only that the pages were hers, and that "
            "no one else could decide what they were worth. It was not the "
            "answer she had wanted, but it was the true one, and she found "
            "that she could live with it after all.",

            "The two of them walked back toward the valley as the light "
            "failed, their breath clouding in the dark, and the town fell "
            "away behind them like a thing already half forgotten. Whatever "
            "came next, it would come to them together.",

            "There is a moment, near the end of any long day of walking, "
            "when the body stops arguing and simply carries on, and they "
            "reached it somewhere on the rising road above the harbor. "
            "Their talk fell away into an easy quiet, and the only sounds "
            "were their footsteps and the wind combing through the winter "
            "grass on either side of them.",

            "She thought again of the pages on the kitchen table, and this "
            "time the thought did not frighten her. Whatever they were, "
            "whether anyone ever read them or not, she had made them, and "
            "the making had cost her something real, and that was a kind of "
            "worth no reader could add to or take away.",

            "The lights of the farmhouse came into view at last, small and "
            "yellow against the dark shoulder of the hill, and she felt the "
            "particular gladness of a person returning to a place that "
            "still, after everything, counted as home. She had not been "
            "sure she would feel it. She was glad, now, that she did.",

            "Later, with the fire banked and the house settling around her "
            "in its familiar creaks, Kathryn took up the manuscript again "
            "and read the first page through without flinching. It was not "
            "perfect, and it did not need to be. It only needed to be "
            "finished, and it was, and in the morning she would begin to "
            "decide what came after that.",
        ],
    ),
]


# --- The answer key -------------------------------------------------------

MANIFEST: list[dict] = [
    {
        "id": "E1-repeated-word",
        "error_type": "repeated_word",
        "chapter": 1,
        "quote": "She walked to the the window and looked out at the rain",
        "correction": "She walked to the window and looked out at the rain",
    },
    {
        "id": "E2-comma-splice",
        "error_type": "comma_splice",
        "chapter": 1,
        "quote": "The manuscript was finished, nobody wanted to read it.",
        "correction": "The manuscript was finished. Nobody wanted to read it.",
    },
    {
        "id": "E3-serial-comma",
        "error_type": "serial_comma",
        "chapter": 2,
        "quote": "He packed his boots, a lantern and a knife for the journey",
        "correction": "He packed his boots, a lantern, and a knife for the journey",
    },
    {
        "id": "E4-homophone-its",
        "error_type": "homophone",
        "chapter": 2,
        "quote": "The town had lost it's charm over the long winter",
        "correction": "The town had lost its charm over the long winter",
    },
    {
        "id": "E5-name-drift",
        "error_type": "name_drift",
        "chapter": 3,
        "quote": "Katherine smiled at the memory of that first morning",
        "correction": "Kathryn smiled at the memory of that first morning",
    },
    {
        "id": "E6-spelling-teh",
        "error_type": "spelling",
        "chapter": 3,
        "quote": "He opened teh door and stepped into the cold",
        "correction": "He opened the door and stepped into the cold",
    },
]


def paragraph_texts() -> list[tuple[str, str]]:
    """The manuscript as an ordered list of (text, style) pairs — headings and
    body paragraphs interleaved exactly as they are written to the .docx.

    This is the determinism surface: it is pure data, so two calls are always
    equal, and the built .docx carries this same text."""
    out: list[tuple[str, str]] = []
    for heading, paras in CHAPTERS:
        out.append((heading, HEADING_STYLE))
        for para in paras:
            out.append((para, BODY_STYLE))
    return out


def build() -> "docx.document.Document":
    """Build (in memory) the tiny-novel Document with pinned core properties."""
    d = docx.Document()
    for text, style in paragraph_texts():
        d.add_paragraph(text, style=style)
    props = d.core_properties
    props.author = "DocProof Galley Fixtures"
    props.title = "A Tiny Novel"
    props.created = _EPOCH
    props.modified = _EPOCH
    props.revision = 1
    return d


def write() -> None:
    """Write the .docx and the manifest to disk next to this file."""
    build().save(DOCX)
    MANIFEST_PATH.write_text(
        json.dumps(MANIFEST, indent=2, ensure_ascii=True) + "\n",
        encoding="ascii",
    )


if __name__ == "__main__":
    write()
    words = sum(len(t.split()) for t, _ in paragraph_texts())
    print(f"wrote {DOCX.name} ({words} words) and {MANIFEST_PATH.name} "
          f"({len(MANIFEST)} planted errors)")
