"""Exact cosine retrieval over the active snapshot.

The order of operations is the contract (PLAN sections 6.4 and 14.1):

1. metadata filters are applied FIRST, so scoring only ever touches candidates
   the caller asked for;
2. crop and frame are scored separately against the same query vector;
3. the two collapse to ONE event score, ``max(crop, frame)`` over whichever
   representations exist - so an event can never appear twice in a result list;
4. an optional ``min_score`` threshold and the final top-K are applied AFTER
   that aggregation, never per representation;
5. ties break deterministically by score DESC, crossed_at DESC, event_id ASC.

Search is exact: every candidate is scored. There is no approximate index, no
recall/latency trade-off, and no query rewriting of any kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

from app.domain.models import SearchFilters
from app.embeddings.manifest import EMBEDDING_DIM
from app.retrieval.snapshot import SearchSnapshot

__all__ = ["RankedResult", "RankingOutcome", "select_candidates", "rank_events"]

_MISSING: Final[int] = -1
#: Scores are rounded for presentation only; ranking uses full float32 values.
SCORE_DECIMALS: Final[int] = 6


@dataclass(frozen=True, slots=True)
class RankedResult:
    event_index: int
    event_id: str
    score: float
    crop_score: float | None
    frame_score: float | None


@dataclass(frozen=True, slots=True)
class RankingOutcome:
    results: tuple[RankedResult, ...]
    candidate_count: int


def select_candidates(snapshot: SearchSnapshot, filters: SearchFilters) -> np.ndarray:
    """Event indices matching every supplied filter, ANDed together."""
    total = snapshot.event_count
    if total == 0:
        return np.empty(0, dtype=np.int64)
    if filters.is_contradictory:
        # A class that cannot belong to the requested category matches nothing.
        # The filter is answered as empty rather than silently rewritten.
        return np.empty(0, dtype=np.int64)

    mask = np.ones(total, dtype=bool)
    if filters.camera_id is not None:
        mask &= snapshot.camera_ids == filters.camera_id
    if filters.object_category is not None:
        mask &= snapshot.object_categories == str(filters.object_category)
    if filters.object_class is not None:
        mask &= snapshot.object_classes == str(filters.object_class)
    if filters.direction is not None:
        mask &= snapshot.directions == str(filters.direction)
    if filters.start is not None:
        mask &= snapshot.crossed_at_ms >= int(filters.start.timestamp() * 1000)
    if filters.end is not None:
        # 'to' is exclusive.
        mask &= snapshot.crossed_at_ms < int(filters.end.timestamp() * 1000)
    return np.flatnonzero(mask)


def rank_events(
    snapshot: SearchSnapshot,
    query: np.ndarray,
    *,
    filters: SearchFilters | None = None,
    top_k: int = 20,
    min_score: float | None = None,
) -> RankingOutcome:
    """Score, fuse, threshold, and rank. One result per event, always."""
    if query.shape != (EMBEDDING_DIM,):
        raise ValueError(f"query vector must be ({EMBEDDING_DIM},), got {query.shape}")
    if not np.isfinite(query).all():
        raise ValueError("query vector is not finite")

    candidates = select_candidates(snapshot, filters or SearchFilters())
    if candidates.size == 0:
        return RankingOutcome(results=(), candidate_count=0)

    query32 = np.ascontiguousarray(query, dtype=np.float32)

    crop_rows = snapshot.crop_rows[candidates]
    has_crop = crop_rows != _MISSING
    crop_scores = np.full(candidates.size, np.nan, dtype=np.float32)
    if has_crop.any():
        crop_scores[has_crop] = snapshot.crop_matrix[crop_rows[has_crop]] @ query32

    frame_rows = snapshot.frame_rows[candidates]
    has_frame = frame_rows != _MISSING
    frame_scores = np.full(candidates.size, np.nan, dtype=np.float32)
    if has_frame.any():
        frame_scores[has_frame] = snapshot.frame_matrix[frame_rows[has_frame]] @ query32

    # One score per EVENT: the better of its available representations. An event
    # with a crop and a frame is still exactly one candidate and one result.
    fused = np.fmax(
        np.where(has_crop, crop_scores, -np.inf),
        np.where(has_frame, frame_scores, -np.inf),
    ).astype(np.float32)

    keep = np.isfinite(fused)
    if min_score is not None:
        keep &= fused >= np.float32(min_score)
    kept = np.flatnonzero(keep)
    if kept.size == 0:
        return RankingOutcome(results=(), candidate_count=int(candidates.size))

    # Deterministic order: score DESC, crossed_at DESC, event_id ASC. The event
    # index is the ascending-id rank, so it is the last (least significant) key.
    subset = candidates[kept]
    order = np.lexsort(
        (
            subset,
            -snapshot.crossed_at_ms[subset],
            -fused[kept].astype(np.float64),
        )
    )
    selected = order[: max(0, int(top_k))]

    results = tuple(
        RankedResult(
            event_index=int(subset[position]),
            event_id=snapshot.event_ids[int(subset[position])],
            score=float(fused[kept][position]),
            crop_score=(
                float(crop_scores[kept][position]) if has_crop[kept][position] else None
            ),
            frame_score=(
                float(frame_scores[kept][position]) if has_frame[kept][position] else None
            ),
        )
        for position in selected
    )
    return RankingOutcome(results=results, candidate_count=int(candidates.size))
