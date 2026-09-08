"""The closed-class (function) word list, kept in a leaf module.

Both ``adjudicate`` (propagation seeding) and ``sweeps`` (the one-sided
hyphen guard) read this list, and ``adjudicate`` imports ``sweeps`` for
``sentence_window``; holding the list here instead of in ``adjudicate``
is what lets either module be imported first. ``adjudicate`` still
re-exports ``FUNCTION_WORDS`` for callers such as ``galley.settle``.
"""

# Closed-class words: a surface that IS one of these never seeds propagation
# (a swap of "and" or "them" is context, not a typo that recurs), and a
# one-sided hyphen beside one is never closed up as a broken compound.
FUNCTION_WORDS = frozenset("""
a an the this that these those my your his her its our their whose which what
who whom i me you he him she it we us they them one ones oneself myself
yourself himself herself itself ourselves themselves mine yours hers ours
theirs
and or but nor so yet for as if than then because although though while
whereas unless until till since when whenever where wherever whether after
before once
at by in into on onto of off to from with within without about above across
against along among around behind below beneath beside between beyond down
during except inside like near out outside over past through throughout
toward towards under underneath up upon via
is am are was were be been being do does did done doing have has had having
will would shall should can could may might must ought need dare
not no nor never none nothing any some all both each either neither every
few many much more most other another such very too quite rather also
just only even still already yet again ever here there
""".split())
