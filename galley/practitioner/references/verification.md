# Independent verification and coverage

Run `docproof galley verify RUN --config CONFIG --engine subagent` with logs
redirected. Use voice notes through `--context` where present; optional `--dry-run`
reports counts. Preserve the approved model routing and the full read design.

The change verifier re-reads EVERY applied edit in the WHOLE source and accepted
paragraph; never clip its packet to a sentence. The finished-text walk reads ALL
accepted paragraphs in two passes: mechanics then slow type-and-compare for
omissions, duplication, and sense. Rotate reader assignments so nobody verifies
its own edits. These are independent gates, not optional extras to certification.
When legacy copyediting is authorized, verify the mechanical build before adding
copyediting, then verify that build again to preserve damage attribution.

Read summary and WARNING/ERROR lines and bounded coverage counts. If
`finished_walk.json` contains unread_paragraphs, re-run those exact ids via
`--paragraphs @FILE` to a side directory, preserve the coverage evidence, and
record the result. A truncated/missing response is an unread region, never an
empty clean finding list. Do not silently skip paragraphs or applied edits.

Do not hand-fix what Verify raises: settlement owns those decisions. Keep internal
read/anchor/tool failures internal, with unresolved evidence. A new build needs
fresh hash-bound verification; artifacts from an older build do not prove it.
Advance state only when the required evidence supports it, always with source and
config hashes. In enrolled workspaces leave all evidence and a valid tracked
snapshot for the driver's final Astra review; do not invent an author query or
editorial verdict to work around a failure.

## Independent-pass identity and crash resume

`--verification-pass` identifies the independent read being performed;
`--verification-policy` identifies its coverage policy;
`--required-verification-passes` declares that policy's complete required pass set.
These flags **do not schedule additional reads**. Perform the mandated mechanics
and slow type-and-compare passes explicitly, with distinct pass identities; never
declare only `primary` for work that actually requires two independent reads.
The standalone CLI's default `primary` invocation still performs its existing
executable read count; metadata does not turn one read into two.

An incomplete interrupted invocation may resume validated windows only for the
same snapshot, policy, and pass identity. A completed repeat is always fresh;
changing the pass ID forces an independent read instead of reusing the previous
pass. A multipass policy conservatively disables clean-entry reuse while the full
required pass set cannot be represented/proven. Do not weaken the declared policy
to enable reuse; missing independent coverage remains work to complete.
