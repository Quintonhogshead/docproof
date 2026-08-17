"""Applying a list of corrections to an InDesign document, via IDML.

The corrections counterpart to manuscript prep. Prep turns a Word manuscript
into an InDesign-ready IDML; this turns a *finished* InDesign book — exported to
IDML on the designer's Mac — plus a list of corrections into a corrected IDML
the designer opens back up.

Three stages, each its own module and each with one boundary:

  * `apply`   — anchor every edit to the exact text it changes and replace that
                span, deterministically. No model writes code here; a match may
                never cross a paragraph boundary, so the paragraph-merge and
                deleted-page failures the hand-written scripts produced are not
                expressible.
  * `verify`  — compare the IDML before and after word for word, reconcile every
                change against the edit list, and surface anything that changed
                without being asked for. This is subtraction, not a second
                reading: every change is in the diff by construction, so an
                unrequested one cannot hide.
  * parsing   — turn a corrections source (a typed list, or a tracked-changes
                PDF) into the edit list `apply` consumes. Its own future module;
                the deterministic core here does not depend on it.

Anchoring reuses the review pipeline's `validator` helpers (find_nth, the
punctuation fold, shrink) so the two never disagree about what counts as the
same text. IDML reading reuses `prep.writers.indesign_idml`'s primitives.
"""
