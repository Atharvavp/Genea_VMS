"""Index status and facet routes. Both are answered from local state only."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.domain.models import FacetsResponse, IndexStatusResponse
from app.embeddings.manifest import MANIFEST

__all__ = ["router", "build_status"]

router = APIRouter(prefix="/api/index", tags=["index"])


@router.get("/facets", response_model=FacetsResponse)
async def facets(request: Request) -> FacetsResponse:
    import asyncio

    return await asyncio.to_thread(request.app.state.search_service.facets)


@router.get("/status", response_model=IndexStatusResponse)
async def status(request: Request) -> IndexStatusResponse:
    import asyncio

    return await asyncio.to_thread(build_status, request.app.state)


def build_status(state) -> IndexStatusResponse:
    """One consistent read of every local counter and the safe upstream state."""
    representations = state.representations
    counts = representations.counts()
    events = representations.event_counts()
    sync = state.sync.read()
    snapshot = state.snapshot_store.current()
    oldest_pending = representations.oldest_pending_at()

    from app.domain.models import format_utc

    indexing = "running"
    if getattr(state, "indexer", None) is not None and state.indexer.paused_reason:
        indexing = "paused"
    elif sync.upstream_state.value == "unavailable":
        indexing = "paused"
    elif not representations.pending_work_exists():
        indexing = "idle"

    search_ready = bool(getattr(state, "search_service", None)) and state.search_service.ready
    return IndexStatusResponse(
        model_id=MANIFEST.model_id,
        model_revision=MANIFEST.revision,
        model_sha256=MANIFEST.weights_sha256,
        embedding_dim=MANIFEST.dimension,
        embedding_dtype=MANIFEST.dtype,
        index_revision=snapshot.index_revision,
        known_events=events["known"],
        searchable_events=events["searchable"],
        complete_events=events["complete"],
        partial_events=events["partial"],
        failed_events=events["failed"],
        crop_indexed=counts["crop_indexed"],
        frame_indexed=counts["frame_indexed"],
        pending_representations=counts["pending"],
        retryable_representations=counts["retryable"],
        permanent_error_representations=counts["permanent_error"],
        oldest_pending_at=format_utc(oldest_pending) if oldest_pending else None,
        backfill_complete=sync.backfill_complete,
        backfill_phase="complete" if sync.backfill_complete else "backfilling",
        high_water_crossed_at=(
            format_utc(sync.high_water_crossed_at) if sync.high_water_crossed_at else None
        ),
        last_successful_poll_at=(
            format_utc(sync.last_successful_poll_at)
            if sync.last_successful_poll_at else None
        ),
        last_full_reconcile_at=(
            format_utc(sync.last_full_reconcile_at)
            if sync.last_full_reconcile_at else None
        ),
        upstream_state=sync.upstream_state,
        upstream_error_code=sync.last_error_code,
        indexing=indexing,
        search="ok" if search_ready else "unavailable",
    )
