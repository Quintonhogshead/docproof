"""The accuracy scorecard.

A held-out corpus of labeled cases, run through the real pipeline, scored for
precision and recall per error type. See docs/accuracy-eval-plan.md. Nothing in
here ships in the wheel or runs during a review — it exists to measure whether a
prompt, model, or verification change made the corrections better or merely
different.
"""
