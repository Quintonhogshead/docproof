"""Manuscript prep: tag a Word manuscript into a house InDesign style set.

The review pipeline finds errors and writes tracked changes. Prep answers a
different question — what IS each paragraph — and writes either a clean
style-tagged .docx that InDesign maps on Place, or the same tagging as tracked
changes for someone to walk in Word.

The house style set itself is data: config/prep/house_styles.yaml. Nothing here
knows an Atmosphere Press style name.
"""
from __future__ import annotations

from .model import Flag, ParagraphPlan, PrepPlan, Structure, Tag
from .pipeline import (OUTPUT_KINDS, PrepOutputs, PreparedPrep, finish,
                       prepare, run, run_mock)
from .styles import StyleSheet, StyleSheetError, load_style_sheet
from .verify import VerificationFailed

__all__ = ["Flag", "ParagraphPlan", "PrepPlan", "Structure", "Tag",
           "OUTPUT_KINDS", "PrepOutputs", "PreparedPrep", "finish", "prepare",
           "run", "run_mock", "StyleSheet", "StyleSheetError",
           "load_style_sheet", "VerificationFailed"]
