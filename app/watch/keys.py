"""The key that ties a manuscript in the folder to a record in HubSpot.

One shared folder, not a folder per book, so a file has to carry the thing that
says which book it is. That thing is in its name — an ISBN, an order number,
whatever the press already writes there — and this pulls it out.

Pure and testable on purpose: "why did DocProof think this file was book
9781234567890?" has a single answer that can be read and tested without a
network, the same discipline `stages.classify` keeps.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("docproof.app.watch.keys")


def key_from_name(name: str, pattern: str) -> str | None:
    """The HubSpot key carried in a filename, or `None` if it carries none.

    With no pattern the key is the filename stem — the whole name is the key,
    which is the simplest thing that can work when the press names files after
    the property already. With a pattern it is the first capture group, or the
    whole match when the pattern has no groups. A pattern that does not match is
    not an error: the file simply has no key this way, so the gate leaves it
    waiting and says so.
    """
    stem = Path(name).stem
    if not pattern:
        return stem or None
    try:
        match = re.search(pattern, name)
    except re.error as e:
        # A broken pattern is a settings mistake, not a fact about this file.
        # Said once per file rather than swallowed, so it is findable in the log.
        log.warning("The HubSpot key pattern %r will not compile (%s); "
                    "treating %s as having no key.", pattern, e, name)
        return None
    if match is None:
        return None
    key = match.group(1) if match.groups() else match.group(0)
    return key or None
