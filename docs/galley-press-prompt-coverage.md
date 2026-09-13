# Atmosphere prompt coverage in fixed Galley

All 120 indexed rules and all accompanying prose/table instructions are accounted for: **149 source items**. The original JSON is archived under `config/galley/proofreading_source.json`; the machine-checked, item-level map is `config/galley/proofreading_coverage.json`. The substantive editorial brief is shared by Fable, Astra, Opus adjudication and Luna checks. It is less than half the length of the original pasted prompt.

## Explicit adaptations

- Clear proofreading errors only; no speculative rewrites, correction quotas, or automatic conversion of an intentionally present-tense scene.
- Galley’s existing clock/number/currency policy wins over the source’s lowercase a.m./am examples. Established regional quotation, spelling and punctuation conventions are protected; the Story Sheet records evidence and uncertainty. Local US-configured rules remain proposals, not proof that a regional form is wrong.
- Smart-quote and internal-space corrections remain tracked. Rejecting all changes restores the exact original; there are no silent-edit audit exceptions.
- Poetry remains spelling-only. Ambiguity, model disagreement and unsupported structural operations do not become author comments.
- The fixed sequence supplies three successive whole-book reading stages (Opus/Sol, Fable, Astra), dedicated typed/local/number work and Luna edit checks. The source does not schedule extra agents, a supervising Brain, or recursive passes.
- Native Galley keeps its tracked/clean Word outputs, existing filenames, Markdown report and certified evidence rather than adopting the pasted-chat two-file/Word-log format. The report adds recorded assumptions, focused-site acknowledgments, final raw pattern counts, note coverage and limits.
- Final counts describe actual signals on the final text, not automatic errors or imaginary zeroes. The final audit does not repeat LanguageTool or buy another model sweep.
- Citation and structure indexes are explicitly partial aids. Readers compare actual counterparts and cannot infer a missing reference from index absence. No external fact-check or invented authority citation is claimed.
- Formatting is conservative: only confirmed roman title spans can receive an italic proposal. Unknown/inherited formatting and text changed inside a title are not guessed to be roman. All supported proposals receive the normal correction check.

## Executable coverage

Both final readers must acknowledge every supplied focused-site ID. Scripted sites include all dialogue punctuation/case combinations in both orders, serial-comma candidates, quotation-integrity signals and narration-only tense profiles. Profiles are recomputed from each reader’s current text; prose footnotes/endnotes remain in scope. Known formatting and applicable whole-book citation passages accompany the owned text. Existing local punctuation, consistency, spelling, number and LanguageTool checks remain active.

## Source-by-source accounting

| Source item | Extracted sections | Owners | Handling |
|---|---|---|---|
| preamble/paragraph-1 | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| style-basis/paragraph-1 | authority | story_sheet, fable, astra, luna_checks | adapted_precision |
| style-basis/u-s-manuscripts | authority | story_sheet, fable, astra, luna_checks | adapted_precision |
| style-basis/u-k-manuscripts | authority | story_sheet, fable, astra, luna_checks | adapted_precision |
| style-basis/canadian-manuscripts | authority | story_sheet, fable, astra, luna_checks | adapted_precision |
| style-basis/australian-manuscripts | authority | story_sheet, fable, astra, luna_checks | adapted_precision |
| style-basis/house-supplement-applies-to-all-variants | authority | story_sheet, fable, astra, luna_checks | adapted_precision |
| operating-principles/author-voice-always-wins | scope | fixed_workflow, fixed_documents, fable, astra | adapted_precision |
| operating-principles/two-channels-never-blurred | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| operating-principles/tracked-changes | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| operating-principles/comments-queries | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| operating-principles/silent-exceptions-neither-channel | scope | fixed_workflow, fixed_documents, fable, astra | explicit_override |
| operating-principles/do-not-change-formatting-spacing-indentation | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| operating-principles/set-the-tracked-change-author-name-to | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| operating-principles/final-audit-mandatory | scope | fixed_workflow, fixed_documents, fable, astra | explicit_override |
| operating-principles/three-full-reads-mandatory-for-full-passes | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| operating-principles/pattern-rules-are-executed-not-read-for | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| universal-house-style-rules-apply-to-every-variant/paragraph-1 | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/serial-comma-always | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/comma-after-introductory-phrases | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/comma-in-direct-address | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/comma-before-tag-questions | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/semicolons-in-complex-lists | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/em-dash-unspaced | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/en-dash | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/hyphen | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/ellipsis | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/no-stacked-punctuation-zero-tolerance-always-a | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/numbers | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/centuries | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/smart-quotes-throughout | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | explicit_override |
| universal-house-style-rules-apply-to-every-variant/currency | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/compound-modifiers-with-ly-adverbs | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/suspensive-hyphenation | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/long-work-titles-in-italics | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/units-and-measurements | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| universal-house-style-rules-apply-to-every-variant/percent-symbol | punctuation, numbers_compounds, capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| house-style-guide/proper-nouns-and-official-names-always | capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| house-style-guide/family-terms-capitalized-only-when-used-as-a | capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| house-style-guide/job-titles-capitalized-before-a-name-lowercase | capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| house-style-guide/official-names-capitalized-generic-references | capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| house-style-guide/compass-directions-lowercase-when-generic | capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| house-style-guide/days-months-holidays-nationalities-and | capitalization_titles | fable, astra, opus_adjudication, luna_checks | editorial |
| variant-specific-rules/paragraph-1 | authority | story_sheet, fable, astra, luna_checks | adapted_precision |
| u-s-cmos-and-canadian-cmos-punctuation/quotation-marks | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-s-cmos-and-canadian-cmos-punctuation/short-work-titles | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-s-cmos-and-canadian-cmos-punctuation/times | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | explicit_override |
| u-s-cmos-and-canadian-cmos-punctuation/dates | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-s-cmos-and-canadian-cmos-punctuation/decades | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-s-cmos-and-canadian-cmos-punctuation/that-which | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-s-cmos-and-canadian-cmos-punctuation/percent | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-s-cmos-and-canadian-cmos-punctuation/punctuation-with-quotes | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-s-cmos-and-canadian-cmos-punctuation/spelling | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-k-oxford-and-australian-oxford-macquarie/quotation-marks | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-k-oxford-and-australian-oxford-macquarie/short-work-titles | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-k-oxford-and-australian-oxford-macquarie/times | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | explicit_override |
| u-k-oxford-and-australian-oxford-macquarie/dates | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-k-oxford-and-australian-oxford-macquarie/decades | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-k-oxford-and-australian-oxford-macquarie/that-which | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-k-oxford-and-australian-oxford-macquarie/percent | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-k-oxford-and-australian-oxford-macquarie/punctuation-with-quotes | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| u-k-oxford-and-australian-oxford-macquarie/spelling | authority, numbers_compounds | fable, astra, opus_adjudication, luna_checks | editorial |
| canada-and-australia/paragraph-1 | authority | story_sheet, fable, astra, luna_checks | adapted_precision |
| dialogue-mechanics/paragraph-1 | dialogue | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| dialogue-mechanics/paragraph-2 | dialogue | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| dialogue-mechanics/one-speaker-per-paragraph | dialogue | local_checks, typed_detectors, focused_checks, fable, astra | adapted_precision |
| dialogue-mechanics/multi-paragraph-speech-by-one-character | dialogue | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| dialogue-mechanics/punctuation-around-quotes | dialogue | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| dialogue-mechanics/stutters-and-interruptions | dialogue | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| dialogue-mechanics/watch-carefully-for | dialogue | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| dialogue-mechanics/table-1/row-1 | dialogue | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| dialogue-mechanics/table-1/row-2 | dialogue | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| dialogue-mechanics/table-1/row-3 | dialogue | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| dialogue-mechanics/table-1/row-4 | dialogue | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| dialogue-mechanics/table-1/row-5 | dialogue | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| common-error-patterns-to-actively-hunt/possessive-vs-contraction | word_errors, consistency | fable, astra, opus_adjudication, luna_checks | editorial |
| common-error-patterns-to-actively-hunt/subject-verb-agreement | word_errors, consistency | fable, astra, opus_adjudication, luna_checks | editorial |
| common-error-patterns-to-actively-hunt/doubled-small-words-and-missing-small-words | word_errors, consistency | fable, astra, opus_adjudication, luna_checks | editorial |
| common-error-patterns-to-actively-hunt/wrong-prepositions | word_errors, consistency | fable, astra, opus_adjudication, luna_checks | editorial |
| common-error-patterns-to-actively-hunt/missing-word-break | word_errors, consistency | fable, astra, opus_adjudication, luna_checks | editorial |
| common-error-patterns-to-actively-hunt/compound-word-consistency | word_errors, consistency | fable, astra, opus_adjudication, luna_checks | editorial |
| common-error-patterns-to-actively-hunt/one-word-vs-two-word-constructions | word_errors, consistency | fable, astra, opus_adjudication, luna_checks | editorial |
| common-error-patterns-to-actively-hunt/coined-vocabulary-consistency | word_errors, consistency | fable, astra, opus_adjudication, luna_checks | editorial |
| footnotes-and-endnotes/paragraph-1 | citations_notes | fable, astra, opus_adjudication, luna_checks | editorial |
| footnotes-and-endnotes/paragraph-2 | citations_notes | fable, astra, opus_adjudication, luna_checks | editorial |
| dashes/paragraph-1 | punctuation | fable, astra, opus_adjudication, luna_checks | editorial |
| serial-comma/paragraph-1 | punctuation | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| serial-comma/paragraph-2 | punctuation | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| serial-comma/surface-every-candidate-site | punctuation | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| serial-comma/judge-each-one | punctuation | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| serial-comma/report-the-count-in-the-change-log | punctuation | local_checks, typed_detectors, focused_checks, fable, astra | editorial |
| narrative-tense/paragraph-1 | tense | story_sheet, focused_checks, fable, astra, luna_checks | adapted_precision |
| narrative-tense/paragraph-2 | tense | story_sheet, focused_checks, fable, astra, luna_checks | adapted_precision |
| narrative-tense/paragraph-3 | tense | story_sheet, focused_checks, fable, astra, luna_checks | adapted_precision |
| narrative-tense/establish-the-baseline-before-you-read-a-word | tense | story_sheet, focused_checks, fable, astra, luna_checks | adapted_precision |
| narrative-tense/surface-every-candidate-site | tense | story_sheet, focused_checks, fable, astra, luna_checks | adapted_precision |
| narrative-tense/working-by-eye-instead-do-not-read-forward | tense | story_sheet, focused_checks, fable, astra, luna_checks | adapted_precision |
| narrative-tense/judge-each-one-against-the-baseline-not-against | tense | story_sheet, focused_checks, fable, astra, luna_checks | adapted_precision |
| narrative-tense/protect-what-is-legitimately-present | tense | story_sheet, focused_checks, fable, astra, luna_checks | adapted_precision |
| narrative-tense/report-the-count-in-the-change-log | tense | story_sheet, focused_checks, fable, astra, luna_checks | adapted_precision |
| additional-dedicated-and-focused-passes/paragraph-1 | consistency, word_errors, dialogue, tense, citations_notes | fable, astra, opus_adjudication, luna_checks | editorial |
| additional-dedicated-and-focused-passes/paragraph-2 | consistency, word_errors, dialogue, tense, citations_notes | fable, astra, opus_adjudication, luna_checks | editorial |
| additional-dedicated-and-focused-passes/paragraph-3 | consistency, word_errors, dialogue, tense, citations_notes | fable, astra, opus_adjudication, luna_checks | editorial |
| additional-dedicated-and-focused-passes/paragraph-4 | consistency, word_errors, dialogue, tense, citations_notes | fable, astra, opus_adjudication, luna_checks | editorial |
| additional-dedicated-and-focused-passes/paragraph-5 | consistency, word_errors, dialogue, tense, citations_notes | fable, astra, opus_adjudication, luna_checks | editorial |
| additional-dedicated-and-focused-passes/paragraph-6 | consistency, word_errors, dialogue, tense, citations_notes | fable, astra, opus_adjudication, luna_checks | editorial |
| additional-dedicated-and-focused-passes/paragraph-7 | consistency, word_errors, dialogue, tense, citations_notes | fable, astra, opus_adjudication, luna_checks | editorial |
| additional-dedicated-and-focused-passes/paragraph-8 | consistency, word_errors, dialogue, tense, citations_notes | fable, astra, opus_adjudication, luna_checks | editorial |
| process/read-the-whole-manuscript-before-editing | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| process/mechanical-sweep-across-the-full-document | punctuation | local_checks, fable, astra | adapted_workflow |
| process/stacked-punctuation-sweep | punctuation | local_checks, fable, astra | adapted_workflow |
| process/numbers-sweep | numbers_compounds | number_sweep, fable, astra | existing_number_policy |
| process/centuries-sweep | numbers_compounds | number_sweep, fable, astra | existing_number_policy |
| process/title-italics-sweep | capitalization_titles | typed_detectors, fable, astra, fixed_documents | adapted_workflow |
| process/ellipsis-sweep | punctuation | local_checks, fable, astra | adapted_workflow |
| process/double-space-sweep | punctuation | local_checks, fable, astra | explicit_override |
| process/must-include-the-dialogue-tag-exhaustive-check | dialogue, tense | local_checks, focused_checks, fable, astra | adapted_workflow |
| process/first-careful-read-chapter-by-chapter | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| process/consistency-style-sheet-pass | consistency | story_sheet, local_checks, fable, astra | adapted_workflow |
| process/confusables-pass | word_errors | typed_detectors, fable, astra | adapted_workflow |
| process/agreement-pass | word_errors | typed_detectors, fable, astra | adapted_workflow |
| process/narrative-tense-dedicated-pass | tense | story_sheet, focused_checks, fable, astra | adapted_workflow |
| process/serial-comma-dedicated-pass | punctuation | typed_detectors, focused_checks, fable, astra | adapted_workflow |
| process/quotation-integrity-pass | dialogue, tense | local_checks, focused_checks, fable, astra | adapted_workflow |
| process/citation-cross-reference-pass | citations_notes | story_sheet, focused_checks, fable, astra | adapted_workflow |
| process/mandatory-second-full-read | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| process/mandatory-third-full-read | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| process/independent-verification-pass | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| process/edit-integrity-pass | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| process/final-audit | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| time-expectation/paragraph-1 | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| deliverables-two-files/the-manuscript-docx | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| deliverables-two-files/a-separate-change-log-docx | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| deliverables-two-files/style-basis-and-english-variant-assumption | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| deliverables-two-files/a-table-of-every-correction-location-original | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| deliverables-two-files/a-scripted-check-report | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| deliverables-two-files/a-list-of-comment-queries | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| deliverables-two-files/a-deliberately-left-unchanged-section-anything | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| deliverables-two-files/a-pass-log | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| deliverables-two-files/a-notes-coverage-statement | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| deliverables-two-files/an-honest-limits-of-this-pass-note-what-was | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| deliverables-two-files/confirmation-that-the-final-audit-passed-body | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| deliverables-two-files/filename | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| honesty-constraints/never-overstate-the-depth-of-the-pass | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| honesty-constraints/if-a-manuscript-is-too-long-to-read-every-word | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| honesty-constraints/the-goal-is-to-do-a-thorough-job-every-time | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| honesty-constraints/where-a-suspected-error-has-a-clear-best-fix | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| honesty-constraints/if-you-encounter-something-you-genuinely-cannot | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
| honesty-constraints/if-you-discover-mid-pass-that-an-earlier | scope | fixed_workflow, fixed_documents, fable, astra | adapted_workflow |
