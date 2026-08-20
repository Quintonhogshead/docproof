"""Adapters that turn DocProof's existing detectors into examination sites."""
from .legacy import (finding_generator, site_from_candidate, site_from_finding,
                     site_type_for, sites_from_model_obligations,
                     sites_from_spell_scan, sites_from_sweep_obligations)

__all__ = [
    "finding_generator", "site_from_candidate", "site_from_finding",
    "site_type_for", "sites_from_model_obligations", "sites_from_spell_scan",
    "sites_from_sweep_obligations",
]
