# Scorecard — `gpt-5.6-luna`

66,290 in / 36,455 out tokens · $0.06 · 140 findings

## Headline

- **Trap false-positive rate** (deliberate prose wrongly flagged, at `medium`): **1%**
- **Anchor-failure rate** (`rejected_no_anchor` ÷ findings): 0%

## Precision / recall by confidence gate

| gate | micro P | micro R | micro F1 | macro F1 | trap FP |
|---|---|---|---|---|---|
| low | 97% | 78% | 87% | 94% | 2% |
| medium ◄ ships | 98% | 78% | 87% | 94% | 1% |
| high | 98% | 76% | 85% | 92% | 1% |

## By error type (at `medium`)

| type | seeded | caught | R | traps | trap-flagged | P | F1 | fix-exact |
|---|---|---|---|---|---|---|---|---|
| comma_splice | 4 | 0 | 0% | 4 | 0 | — | — | 0/0 |
| missing_word | 5 | 0 | 0% | 5 | 0 | — | — | 0/0 |
| run_on_sentence | 5 | 0 | 0% | 4 | 0 | — | — | 0/0 |
| tense_shift | 5 | 0 | 0% | 5 | 0 | — | — | 0/0 |
| preposition_error | 5 | 1 | 20% | 5 | 0 | 100% | 33% | 1/1 |
| subject_verb_agreement | 5 | 4 | 80% | 5 | 0 | 80% | 80% | 4/4 |
| direct_address_comma | 5 | 4 | 80% | 5 | 0 | 100% | 89% | 4/4 |
| number_style | 5 | 4 | 80% | 5 | 0 | 100% | 89% | 4/4 |
| repeated_word | 5 | 5 | 100% | 5 | 1 | 83% | 91% | 5/5 |
| apostrophe_error | 6 | 6 | 100% | 5 | 0 | 100% | 100% | 6/6 |
| capitalization | 6 | 6 | 100% | 5 | 0 | 100% | 100% | 6/6 |
| complex_list_semicolon | 4 | 4 | 100% | 4 | 0 | 100% | 100% | 4/4 |
| currency_style | 5 | 5 | 100% | 4 | 0 | 100% | 100% | 5/5 |
| dialogue_tag | 5 | 5 | 100% | 5 | 0 | 100% | 100% | 0/5 |
| homophone_confusion | 6 | 6 | 100% | 5 | 0 | 100% | 100% | 6/6 |
| introductory_comma | 5 | 5 | 100% | 5 | 0 | 100% | 100% | 5/5 |
| ly_adverb_hyphen | 5 | 5 | 100% | 5 | 0 | 100% | 100% | 5/5 |
| pronoun_agreement | 5 | 5 | 100% | 5 | 0 | 100% | 100% | 5/5 |
| serial_comma | 5 | 5 | 100% | 5 | 0 | 100% | 100% | 5/5 |
| speaker_change | 4 | 4 | 100% | 4 | 0 | 100% | 100% | 0/4 |
| spelling | 6 | 6 | 100% | 5 | 0 | 100% | 100% | 6/6 |
| tag_question_comma | 5 | 5 | 100% | 4 | 0 | 100% | 100% | 3/5 |
| that_which | 4 | 4 | 100% | 4 | 0 | 100% | 100% | 4/4 |
| title_italics | 5 | 5 | 100% | 4 | 0 | 100% | 100% | 5/5 |
