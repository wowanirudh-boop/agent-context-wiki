from __future__ import annotations

from core.conflicts.detect import (
    ComparisonBlock,
    ConflictDetectionResult,
    JudgeResult,
    RecommendationBasis,
    detect_candidate_conflict,
    retrieve_comparison_candidates,
)

__all__ = [
    "ComparisonBlock",
    "ConflictDetectionResult",
    "JudgeResult",
    "RecommendationBasis",
    "detect_candidate_conflict",
    "retrieve_comparison_candidates",
]
