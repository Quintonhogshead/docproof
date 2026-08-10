# Scorecard — `claude-haiku-4-5`

48,484 in / 15,181 out tokens · $0.12 · 172 findings

## Headline

- **Trap false-positive rate** (deliberate prose wrongly flagged, at `medium`): **1%**
- **Anchor-failure rate** (`rejected_no_anchor` ÷ findings): 29%

## Precision / recall by confidence gate

| gate | micro P | micro R | micro F1 | macro F1 | trap FP |
|---|---|---|---|---|---|
| low | 97% | 50% | 66% | 87% | 1% |
| medium ◄ ships | 97% | 49% | 65% | 86% | 1% |
| high | 98% | 49% | 66% | 87% | 0% |

## By error type (at `medium`)

| type | seeded | caught | R | traps | trap-flagged | P | F1 | fix-exact |
|---|---|---|---|---|---|---|---|---|
| comma_splice | 4 | 0 | 0% | 4 | 0 | — | — | 0/0 |
| currency_style | 5 | 0 | 0% | 4 | 0 | — | — | 0/0 |
| dialogue_tag | 5 | 0 | 0% | 5 | 0 | — | — | 0/0 |
| direct_address_comma | 5 | 0 | 0% | 5 | 0 | — | — | 0/0 |
| preposition_error | 5 | 0 | 0% | 5 | 0 | — | — | 0/0 |
| pronoun_agreement | 5 | 0 | 0% | 5 | 0 | — | — | 0/0 |
| repeated_word | 5 | 0 | 0% | 5 | 0 | — | — | 0/0 |
| speaker_change | 4 | 0 | 0% | 4 | 0 | — | — | 0/0 |
| tag_question_comma | 5 | 0 | 0% | 4 | 0 | — | — | 0/0 |
| tense_shift | 5 | 0 | 0% | 5 | 0 | — | — | 0/0 |
| missing_word | 5 | 1 | 20% | 5 | 0 | 100% | 33% | 1/1 |
| run_on_sentence | 5 | 2 | 40% | 4 | 0 | 100% | 57% | 2/2 |
| subject_verb_agreement | 5 | 3 | 60% | 5 | 0 | 75% | 67% | 3/3 |
| number_style | 5 | 4 | 80% | 5 | 0 | 100% | 89% | 4/4 |
| serial_comma | 5 | 4 | 80% | 5 | 0 | 100% | 89% | 4/4 |
| apostrophe_error | 6 | 5 | 83% | 5 | 0 | 100% | 91% | 5/5 |
| homophone_confusion | 6 | 5 | 83% | 5 | 0 | 100% | 91% | 5/5 |
| introductory_comma | 5 | 5 | 100% | 5 | 1 | 83% | 91% | 5/5 |
| capitalization | 6 | 6 | 100% | 5 | 0 | 100% | 100% | 6/6 |
| complex_list_semicolon | 4 | 4 | 100% | 4 | 0 | 100% | 100% | 4/4 |
| ly_adverb_hyphen | 5 | 5 | 100% | 5 | 0 | 100% | 100% | 5/5 |
| spelling | 6 | 6 | 100% | 5 | 0 | 100% | 100% | 5/6 |
| that_which | 4 | 4 | 100% | 4 | 0 | 100% | 100% | 4/4 |
| title_italics | 5 | 5 | 100% | 4 | 0 | 100% | 100% | 5/5 |
