"""Master Data Management — multi-source entity resolution into a single golden record."""

from .survivorship import build_golden, load_policy, match_only, run_match_and_merge

__all__ = ["build_golden", "load_policy", "match_only", "run_match_and_merge"]
