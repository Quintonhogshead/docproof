"""docproof — LLM-assisted review and layout prep for Word and InDesign.

The version number lives here and nowhere else. `pyproject.toml` reads it,
`DocProof.spec` stamps it into the .app bundle, and the app shows it in
Settings — so releasing is editing one line, and nothing can drift out of step
with anything else.

Nothing is imported here on purpose: setuptools reads `__version__` out of this
file before the dependencies exist.
"""

__version__ = "0.155.0"
