"""Query normalization, retrieval orchestration, and response construction.

A search touches the active snapshot and nothing else: no SQLite read, no write
lock, and - critically - no Component 4 request. That is what keeps existing
search working while the upstream is down.

The query text is normalized and bounded, then handed to the frozen tokenizer
unchanged. There is no prompt prefix, template, rewriting, translation,
expansion, synonym list, or LLM anywhere in this path.
"""

from __future__ import annotations

import logging
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Final, Literal

import numpy as np

from app.config import Settings
from app.domain.models import (
    CameraFacet,
    FacetsResponse,
    RepresentationScores,
    SearchFilters,
    SearchResponse,
    SearchResultItem,
)
from app.embeddings.manifest import MANIFEST
from app.retrieval.ranking import SCORE_DECIMALS, rank_events
from app.retrieval.snapshot import SearchSnapshot, SnapshotStore
from app.services.inference import InferenceCoordinator

__all__ = ["SearchService", "QueryRejected", "normalize_query", "MAX_QUERY_LENGTH"]

logger = logging.getLogger("semantic.search")

MAX_QUERY_LENGTH: Final[int] = 256


class QueryRejected(ValueError):
    """The query text is empty, too long, or contains control characters."""


def normalize_query(raw: object) -> str:
    """NFKC-normalize, collapse Unicode whitespace, and bound the length.

    The rejected text is never echoed back to the caller or written to a log.
    """
    if not isinstance(raw, str):
        raise QueryRejected("query must be text")
    text = unicodedata.normalize("NFKC", raw)
    # str.split() splits on Unicode whitespace, so this trims and collapses.
    text = " ".join(text.split())
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        raise QueryRejected("query contains control characters")
    length = len(text)
    if length < 1:
        raise QueryRejected("query is empty")
    if length > MAX_QUERY_LENGTH:
        raise QueryRejected(f"query exceeds {MAX_QUERY_LENGTH} characters")
    return text


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    response: SearchResponse


class SearchService:
    """Turns a query into a ranked, presentable page of events."""

    def __init__(
        self,
        *,
        coordinator: InferenceCoordinator,
        store: SnapshotStore,
        settings: Settings,
    ):
        self._coordinator = coordinator
        self._store = store
        self._settings = settings

    @property
    def snapshot(self) -> SearchSnapshot:
        return self._store.current()

    @property
    def ready(self) -> bool:
        """Local search is usable when the model is loaded - an EMPTY snapshot
        is a valid, ready index, not a failure."""
        return self._coordinator.model_ready

    # ---- searches --------------------------------------------------------

    async def search_text(
        self,
        *,
        query: str,
        filters: SearchFilters,
        top_k: int,
        min_score: float | None,
    ) -> SearchResponse:
        started = time.perf_counter()
        vector = await self._coordinator.embed_text(query)
        return self._respond("text", vector, filters, top_k, min_score, started)

    async def search_image(
        self,
        *,
        image: Any,
        filters: SearchFilters,
        top_k: int,
        min_score: float | None,
    ) -> SearchResponse:
        started = time.perf_counter()
        vector = await self._coordinator.embed_query_image(image)
        return self._respond("image", vector, filters, top_k, min_score, started)

    def _respond(
        self,
        mode: Literal["text", "image"],
        vector: np.ndarray,
        filters: SearchFilters,
        top_k: int,
        min_score: float | None,
        started: float,
    ) -> SearchResponse:
        snapshot = self._store.current()
        outcome = rank_events(
            snapshot, vector, filters=filters, top_k=top_k, min_score=min_score
        )
        results = [
            self._item(snapshot, rank, result)
            for rank, result in enumerate(outcome.results, start=1)
        ]
        elapsed = (time.perf_counter() - started) * 1000
        logger.info(
            "search",
            extra={
                "mode": mode,
                "index_revision": snapshot.index_revision,
                "candidates": outcome.candidate_count,
                "returned": len(results),
                "top_k": top_k,
                "filtered": not filters.is_empty,
                "duration_ms": round(elapsed, 2),
            },
        )
        return SearchResponse(
            mode=mode,
            model_id=MANIFEST.model_id,
            index_revision=snapshot.index_revision,
            candidate_count=outcome.candidate_count,
            elapsed_ms=round(elapsed, 1),
            results=results,
        )

    @staticmethod
    def _item(snapshot: SearchSnapshot, rank: int, result) -> SearchResultItem:
        index = result.event_index
        event_id = result.event_id
        return SearchResultItem(
            rank=rank,
            event_id=event_id,
            # Rounded for stable presentation; ranking used the full float32.
            score=round(result.score, SCORE_DECIMALS),
            representation_scores=RepresentationScores(
                crop=None if result.crop_score is None
                else round(result.crop_score, SCORE_DECIMALS),
                frame=None if result.frame_score is None
                else round(result.frame_score, SCORE_DECIMALS),
            ),
            camera_id=str(snapshot.camera_ids[index]),
            vms_camera_id=snapshot.vms_camera_ids[index],
            camera_name=snapshot.camera_names[index],
            crossed_at=snapshot.crossed_at_text[index],
            object_category=str(snapshot.object_categories[index]),
            object_class=str(snapshot.object_classes[index]),
            direction=str(snapshot.directions[index]),
            crop_url=f"/api/events/{event_id}/crop",
            frame_url=f"/api/events/{event_id}/frame",
            detail_url=f"/api/events/{event_id}",
            recording_url=f"/api/events/{event_id}/recording",
        )

    # ---- facets ----------------------------------------------------------

    def facets(self) -> FacetsResponse:
        """Filter values drawn from the local snapshot, so they survive an outage.

        A renamed camera is presented under the name on its newest indexed
        event; a camera deleted upstream still appears, because its historical
        events are still searchable here.
        """
        snapshot = self._store.current()
        newest: dict[str, tuple[int, str]] = {}
        counts: dict[str, int] = {}
        for index in range(snapshot.event_count):
            camera_id = str(snapshot.camera_ids[index])
            counts[camera_id] = counts.get(camera_id, 0) + 1
            moment = int(snapshot.crossed_at_ms[index])
            known = newest.get(camera_id)
            if known is None or moment > known[0]:
                newest[camera_id] = (moment, snapshot.camera_names[index])
        cameras = [
            CameraFacet(camera_id=camera_id, camera_name=newest[camera_id][1],
                        count=counts[camera_id])
            for camera_id in sorted(counts, key=lambda c: (-counts[c], c))
        ]
        times = snapshot.crossed_at_text
        earliest = min(times) if times else None
        latest = max(times) if times else None
        return FacetsResponse(
            cameras=cameras,
            object_categories=sorted({str(v) for v in snapshot.object_categories}),
            object_classes=sorted({str(v) for v in snapshot.object_classes}),
            directions=sorted({str(v) for v in snapshot.directions}),
            earliest_crossed_at=earliest,
            latest_crossed_at=latest,
            searchable_events=snapshot.event_count,
        )
