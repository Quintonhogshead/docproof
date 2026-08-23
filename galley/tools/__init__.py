"""Galley agent tools — pure, deterministic checks a proofreading agent calls.

Nothing here talks to a model, the network, or a vendor SDK. Each tool answers a
narrow factual question an LLM is weak at (calendar arithmetic, sums, unit
consistency) and returns a structured result that says both the true answer and
whether the claim in the manuscript checks out. A later ticket registers these
on the agent's bus via :data:`galley.tools.checkable.TOOLS`.
"""
