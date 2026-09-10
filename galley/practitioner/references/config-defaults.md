# Full config lists — load only when needed

These are the exact shipped lists as of v0.131.0. If a config wants the typed
passes or the built-in sweeps, paste these blocks in **whole**. The ensemble
fires **only through `error_types`** — no `error_types`, no ensemble, no typed
recall (log reads `0 error type(s) in 0 pass(es)`).

```yaml
# The typed LLM passes — the recall engine. Groups = one read each; keep all 9.
error_types:
  - [repeated_word, spelling, homophone_confusion, apostrophe_error, capitalization]
  - [serial_comma, complex_list_semicolon, introductory_comma,
     direct_address_comma, tag_question_comma]
  - [dialogue_tag, speaker_change]
  - [number_style, currency_style, ly_adverb_hyphen, title_italics]
  - [comma_splice, run_on_sentence, compound_sentence_comma,
     subject_verb_agreement, that_which, that_who]
  - [tense_shift, pronoun_agreement, missing_word, preposition_error]
  - [try_and, list_intro_colon]
  - terminal_mark        # query-channel: asks, never edits
  - unnecessary_comma    # the one comma rule that DELETES; isolated on purpose

# The 16 deterministic $0 sweeps. Omitting the section turns ALL of them off.
sweeps:
  - sweep_ellipsis
  - sweep_dash
  - sweep_stacked_punctuation
  - sweep_doubled_word
  - sweep_century
  - sweep_compound_number
  - sweep_dialogue_tag
  - sweep_terminal_period
  - sweep_quote_punctuation
  - sweep_nested_quote
  - sweep_time_of_day
  - sweep_deity_capital
  - sweep_dialogue_splice
  - sweep_initialism
  - sweep_decade_apostrophe
  - sweep_trailing_space
```

To ADD a per-category repeat read or a bespoke sweep, paste the block above and
append/modify — don't hand-pick a subset unless you mean to drop the rest.
**Named knobs are documented in `references/knobs.md`; use that contract
instead of source searches.** (`languagetool.picky` is real — config.py:679 — and was once
wrongly dropped on a bad grep.)
