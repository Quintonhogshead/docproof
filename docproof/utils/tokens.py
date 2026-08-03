def estimate_tokens(text: str) -> int:
    """Budgeting heuristic (~3.5 chars/token for English prose). Exact counts
    come back in API response metadata and land in the log; this only decides
    where chunk boundaries fall, so precision is not required."""
    return max(1, round(len(text) / 3.5))