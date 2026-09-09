"""Website automation entry point, independent of legacy flat-folder stages.

The WebsiteService worker calls this same tick behavior on its own bounded
queue, using explicitly linked HubSpot/Drive IDs. It does not share or alter
proofing/formatting/promo status properties or sequential workers.
"""
from docproof.website.service import WebsiteService


def tick(service: WebsiteService) -> None:
    service.tick()
