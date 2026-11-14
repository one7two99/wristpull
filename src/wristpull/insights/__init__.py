"""Deterministic analysis on top of the daily summaries: baselines and z-scores, a readiness index with
its components, sleep regularity, lagged correlations with a binned response curve (the "sweet spot"),
weekday patterns and tag effects. Everything is computed in Python; the coach only narrates the result."""

from .package import build_insights

__all__ = ["build_insights"]
