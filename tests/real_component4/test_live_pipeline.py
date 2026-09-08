"""The whole pipeline, end to end, with the REAL model and the REAL Component 4.

Discovery -> artifact fetch -> SigLIP embedding -> SQLite -> snapshot -> search,
with nothing faked. Read-only against Component 4 and bounded to a small slice
so the tier stays runnable.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
import pytest_asyncio

from app.config import Settings
from app.domain.models import RepresentationKind, SearchFilters
from app.embeddings.runtime import EmbeddingRuntime
from app.integrations.component4 import Component4Client, build_client
from app.persistence.database import Database
from app.persistence.repositories import (
    EventRepository,
    RepresentationRepository,
    SyncRepository,
)
from app.retrieval.ranking import rank_events
from app.retrieval.snapshot import SnapshotStore
from app.security.images import decode_query_image
from app.services.discovery import DiscoveryService
from app.services.indexer import IndexerService, SnapshotRebuilder
from app.services.inference import InferenceCoordinator

BASE = os.environ.get("COMPONENT4_API_BASE_URL", "http://host.docker.internal:8100")
MODEL_DIR = Path(os.environ.get("SEMANTIC_MODEL_DIR", "/opt/models/siglip"))
SLICE = int(os.environ.get("REAL_C4_EVENT_SLICE", "24"))


def _reachable() -> bool:
    try:
        return httpx.get(f"{BASE}/health", timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = [
    pytest.mark.skipif(not _reachable(), reason=f"no live Component 4 at {BASE}"),
    pytest.mark.skipif(
        not (MODEL_DIR / "model.safetensors").is_file(), reason="no model snapshot"
    ),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def indexed(tmp_path_factory):
    """Discover a slice of real events and index them with the real model."""
    tmp_path = tmp_path_factory.mktemp("real-c4")
    settings = Settings(
        semantic_data_dir=tmp_path,
        semantic_db_path=tmp_path / "semantic.db",
        semantic_model_dir=MODEL_DIR,
        component4_api_base_url=BASE,
        semantic_index_batch_size=4,
    )
    database = Database(settings.semantic_db_path)
    database.ensure_parent()
    database.initialize()
    events = EventRepository(database)
    representations = RepresentationRepository(database)
    sync = SyncRepository(database)
    http = build_client()
    client = Component4Client(base_url=BASE, client=http)

    runtime = EmbeddingRuntime(model_dir=MODEL_DIR, torch_threads=2)
    started = time.monotonic()
    runtime.load()
    load_seconds = time.monotonic() - started

    coordinator = InferenceCoordinator(runtime)
    store = SnapshotStore()
    rebuilder = SnapshotRebuilder(
        events=events, representations=representations, store=store
    )
    discovery = DiscoveryService(
        client=client, events=events, sync=sync, settings=settings
    )
    indexer = IndexerService(
        client=client, events=events, representations=representations,
        coordinator=coordinator, rebuilder=rebuilder, settings=settings,
    )

    # One page of real events, then trim to the configured slice so the tier
    # stays bounded regardless of how much history Component 4 holds.
    page = await client.list_events()
    slice_events = page.events[:SLICE]
    events.upsert_page(slice_events)

    started = time.monotonic()
    batches = 0
    while batches < 200:
        outcome = await indexer.run_batch()
        if outcome.claimed == 0 and not outcome.deferred:
            break
        batches += 1
    index_seconds = time.monotonic() - started

    try:
        yield {
            "settings": settings, "database": database, "events": events,
            "representations": representations, "store": store,
            "runtime": runtime, "coordinator": coordinator, "client": client,
            "discovery": discovery, "indexer": indexer, "rebuilder": rebuilder,
            "slice": slice_events, "load_seconds": load_seconds,
            "index_seconds": index_seconds, "batches": batches,
        }
    finally:
        await http.aclose()
        database.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_real_events_become_searchable(indexed):
    counts = indexed["representations"].event_counts()
    vectors = indexed["representations"].counts()
    snapshot = indexed["store"].current()
    embedded = vectors["crop_indexed"] + vectors["frame_indexed"]
    per_image = indexed["index_seconds"] / max(1, embedded)
    print(
        f"\nreal pipeline: events={counts['known']} searchable={counts['searchable']} "
        f"complete={counts['complete']} model_load={indexed['load_seconds']:.1f}s "
        f"index={indexed['index_seconds']:.1f}s over {indexed['batches']} batches "
        f"for {embedded} images "
        f"({per_image * 1000:.0f} ms/image incl. fetch)"
    )
    assert counts["known"] == len(indexed["slice"])
    assert counts["searchable"] > 0
    assert snapshot.event_count == counts["searchable"]
    assert snapshot.crop_matrix.shape[1] == 768
    assert np.isfinite(snapshot.crop_matrix).all()
    norms = np.linalg.norm(snapshot.crop_matrix, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-3)


@pytest.mark.asyncio(loop_scope="module")
async def test_real_text_search_ranks_events_and_returns_one_card_each(indexed):
    snapshot = indexed["store"].current()
    if snapshot.is_empty:
        pytest.skip("no searchable events")
    for query in ("person", "a bus on the street", "a car", "a truck"):
        vector = await indexed["coordinator"].embed_text(query)
        outcome = rank_events(snapshot, vector, top_k=5)
        assert outcome.results, f"'{query}' returned nothing"
        ids = [result.event_id for result in outcome.results]
        assert len(ids) == len(set(ids))
        scores = [result.score for result in outcome.results]
        assert scores == sorted(scores, reverse=True)
        top = outcome.results[0]
        index = snapshot.index_of(top.event_id)
        print(
            f"\n  '{query}' -> {snapshot.object_classes[index]} "
            f"score={top.score:.4f} crop={top.crop_score} frame={top.frame_score}"
        )


@pytest.mark.asyncio(loop_scope="module")
async def test_a_real_crop_used_as_a_query_ranks_its_own_event_first(indexed):
    snapshot = indexed["store"].current()
    if snapshot.is_empty:
        pytest.skip("no searchable events")
    event_id = snapshot.event_ids[0]
    payload = await indexed["client"].fetch_artifact(event_id, RepresentationKind.CROP)
    image = decode_query_image(payload, max_bytes=8 * 1024 * 1024, max_pixels=16_777_216)
    try:
        vector = await indexed["coordinator"].embed_query_image(image)
    finally:
        image.close()
    outcome = rank_events(snapshot, vector, top_k=5)
    assert outcome.results[0].event_id == event_id
    assert outcome.results[0].score > 0.99, "an image must match itself almost exactly"


@pytest.mark.asyncio(loop_scope="module")
async def test_metadata_filters_apply_to_real_events(indexed):
    snapshot = indexed["store"].current()
    if snapshot.is_empty:
        pytest.skip("no searchable events")
    camera_id = str(snapshot.camera_ids[0])
    vector = await indexed["coordinator"].embed_text("anything at all")
    filtered = rank_events(
        snapshot, vector, filters=SearchFilters(camera_id=camera_id), top_k=100
    )
    expected = int((snapshot.camera_ids == camera_id).sum())
    assert filtered.candidate_count == expected
    assert all(
        str(snapshot.camera_ids[result.event_index]) == camera_id
        for result in filtered.results
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_restarting_reuses_the_committed_index_without_re_embedding(indexed):
    """A second process over the same volume rebuilds, it does not re-index."""
    settings = indexed["settings"]
    reopened = Database(settings.semantic_db_path)
    try:
        reopened.initialize()
        events = EventRepository(reopened)
        representations = RepresentationRepository(reopened)
        store = SnapshotStore()
        SnapshotRebuilder(
            events=events, representations=representations, store=store
        ).rebuild()

        before = indexed["store"].current()
        after = store.current()
        assert after.event_ids == before.event_ids
        assert after.index_revision == before.index_revision
        assert np.array_equal(after.crop_matrix, before.crop_matrix)
        assert np.array_equal(after.frame_matrix, before.frame_matrix)
        # No representation was reset, so nothing would be embedded again.
        assert representations.counts()["indexed"] == (
            before.crop_matrix.shape[0] + before.frame_matrix.shape[0]
        )
    finally:
        reopened.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_new_upstream_events_are_discovered_by_the_overlap_poll(indexed):
    """Polling finds whatever Component 4 has that this slice did not."""
    before = indexed["events"].count()
    outcome = await indexed["discovery"].poll_overlap()
    assert outcome.completed
    after = indexed["events"].count()
    assert after >= before
    print(f"\n  overlap poll: {before} -> {after} known events")
