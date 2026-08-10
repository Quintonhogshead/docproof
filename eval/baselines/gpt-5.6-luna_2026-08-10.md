# Scorecard — `gpt-5.6-luna`

61,095 in / 12,582 out tokens · $0.03 · 133 findings

## Headline

- **Trap false-positive rate** (deliberate prose wrongly flagged, at `medium`): **0%**
- **Anchor-failure rate** (`rejected_no_anchor` ÷ findings): 0%

## Precision / recall by confidence gate

| gate | micro P | micro R | micro F1 | macro F1 | trap FP |
|---|---|---|---|---|---|
| low | 98% | 89% | 93% | 94% | 1% |
| medium ◄ ships | 99% | 89% | 94% | 95% | 0% |
| high | 99% | 86% | 92% | 93% | 0% |

## By error type (at `medium`)

| type | seeded | caught | R | traps | trap-flagged | P | F1 | fix-exact |
|---|---|---|---|---|---|---|---|---|
| repeated_word | 5 | 0 | 0% | 5 | 0 | — | — | 0/0 |
| dialogue_tag | 5 | 1 | 20% | 5 | 0 | 100% | 33% | 0/1 |
| subject_verb_agreement | 5 | 4 | 80% | 5 | 0 | 80% | 80% | 4/4 |
| direct_address_comma | 5 | 4 | 80% | 5 | 0 | 100% | 89% | 4/4 |
| number_style | 5 | 4 | 80% | 5 | 0 | 100% | 89% | 4/4 |
| serial_comma | 5 | 4 | 80% | 5 | 0 | 100% | 89% | 4/4 |
| apostrophe_error | 6 | 6 | 100% | 5 | 0 | 100% | 100% | 6/6 |
| capitalization | 6 | 6 | 100% | 5 | 0 | 100% | 100% | 6/6 |
| comma_splice | 4 | 4 | 100% | 4 | 0 | 100% | 100% | 4/4 |
| complex_list_semicolon | 4 | 4 | 100% | 4 | 0 | 100% | 100% | 4/4 |
| currency_style | 5 | 5 | 100% | 4 | 0 | 100% | 100% | 5/5 |
| homophone_confusion | 6 | 6 | 100% | 5 | 0 | 100% | 100% | 6/6 |
| introductory_comma | 5 | 5 | 100% | 5 | 0 | 100% | 100% | 5/5 |
| ly_adverb_hyphen | 5 | 5 | 100% | 5 | 0 | 100% | 100% | 5/5 |
| missing_word | 5 | 5 | 100% | 5 | 0 | 100% | 100% | 4/5 |
| preposition_error | 5 | 5 | 100% | 5 | 0 | 100% | 100% | 5/5 |
| pronoun_agreement | 5 | 5 | 100% | 5 | 0 | 100% | 100% | 5/5 |
| run_on_sentence | 5 | 5 | 100% | 4 | 0 | 100% | 100% | 5/5 |
| speaker_change | 4 | 4 | 100% | 4 | 0 | 100% | 100% | 0/4 |
| spelling | 6 | 6 | 100% | 5 | 0 | 100% | 100% | 6/6 |
| tag_question_comma | 5 | 5 | 100% | 4 | 0 | 100% | 100% | 3/5 |
| tense_shift | 5 | 5 | 100% | 5 | 0 | 100% | 100% | 5/5 |
| that_which | 4 | 4 | 100% | 4 | 0 | 100% | 100% | 4/4 |
| title_italics | 5 | 5 | 100% | 4 | 0 | 100% | 100% | 5/5 |
