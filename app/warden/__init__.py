"""The Warden: a monitoring agent for DocProof, Galley and DocWatch.

Reads the state of every DocProof service, decides what is stuck, fixes what
it may fix on its own, asks about the rest, and keeps the team informed. See
docs/monitoring-agent-plan.md for the full design and docs/warden.md for
day-to-day operation.

``WARDEN_VERSION`` mirrors the app's own version rather than keeping a second
number to drift out of step with it — one line to bump on a user-facing
change, same discipline every other release in this repo already follows.
"""
from __future__ import annotations

from docproof import __version__ as WARDEN_VERSION

__all__ = ["WARDEN_VERSION"]
