"""Deterministic observations and explicitly provisional SEO opportunities.

Keep package import side-effect free.  In particular, evaluator-side protocol
code imports the aggregate attestation schema from this package and must not
thereby load the production detector into the truth-holding process.
"""

from __future__ import annotations

from typing import Any

__all__ = ["AnalysisContext", "analyze"]


def __getattr__(name: str) -> Any:
    """Lazily preserve the small public API without eagerly loading analysis."""
    if name in __all__:
        from .analysis import AnalysisContext, analyze

        return {"AnalysisContext": AnalysisContext, "analyze": analyze}[name]
    raise AttributeError(name)
