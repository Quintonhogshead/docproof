"""How Warden talks to a person: iMessage for the quick back-and-forth,
Gmail for the team-wide record.

Two channels because they do different jobs. iMessage is where a "#3 yes"
comes back in ten seconds and a phone buzzes on Quinton's belt at 2am for
the one finding that can't wait; Gmail is where a team distribution list
gets the same alert without needing his number, and where a reply thread
survives longer than a text thread does. Both modules are opener/run
injected like the rest of the watch stack, so no test here ever touches a
real phone or a real inbox.
"""

from app.warden.messaging.imessage import Inbound, rate_ok, read_since, send
from app.warden.messaging.gmail import inbox, recent, replies, send_team

__all__ = [
    "Inbound",
    "rate_ok",
    "read_since",
    "send",
    "inbox",
    "recent",
    "replies",
    "send_team",
]
