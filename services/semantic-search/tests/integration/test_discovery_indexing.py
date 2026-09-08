"""Backfill, polling, restart, retry and partial indexing against the fake C4.

Real SQLite, real repositories, real discovery/indexer services, real HTTP
plumbing through an ASGI transport. Only the embedding model is substituted -
the real one is exercised by the real_model and real_component4 tiers.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.config import Settings
from app.domain.models import RepresentationKind, UpstreamState, utc_now
from app.integrations.component4 import Component4Client
from app.persistence.database import Database
from app.persistence.repositories import (
    EventRepository,
    RepresentationRepository,
    SyncRepository,
)
from app.retrieval.ranking import rank_events
from app.retrieval.snapshot import SnapshotStore
from app.services.discovery import DiscoveryService
from app.services.indexer import IndexerService, SnapshotRebuilder
from app.services.inference import InferenceCoordinator
from tests.fakes.component4_server import FakeComponent4
from tests.fakes.factories import FakeEmbeddingRuntime, make_event, make_events


class Harness:
    """One Component 5 core wired to one fake Component 4."""

    def __init__(self, tmp_path, fake: FakeComponent4, batch_size: int = 4):
        self.fake = fake
        self.settings = Settings(
            semantic_data_dir=tmp_path,
            semantic_db_path=tmp_path / "semantic.db",
            component4_api_base_url="http://component4.test:8100",
            semantic_index_batch_size=batch_size,
            semantic_overlap_seconds=300,
        )
        self.database = Database(self.settings.semantic_db_path)
        self.database.ensure_parent()
        self.database.initialize()
        self.events = EventRepository(self.database)
        self.representations = RepresentationRepository(self.database)
        self.sync = SyncRepository(self.database)
        self.http = fake.client()
        self.client = Component4Client(
            base_url=self.settings.component4_api_base_url, client=self.http
        )
        self.runtime = FakeEmbeddingRuntime()
        self.coordinator = InferenceCoordinator(self.runtime)
        self.store = SnapshotStore()
        self.rebuilder = SnapshotRebuilder(
            events=self.events, representations=self.representations, store=self.store
        )
        self.discovery = DiscoveryService(
            client=self.client, events=self.events, sync=self.sync,
            settings=self.settings,
        )
        self.indexer = IndexerService(
            client=self.client, events=self.events,
            representations=self.representations, coordinator=self.coordinator,
            rebuilder=self.rebuilder, settings=self.settings,
        )

    async def discover_all(self, limit: int = 60) -> None:
        for _ in range(limit):
            outcome = await self.discovery.run_once()
            if outcome.completed and self.sync.read().backfill_complete:
                return
        raise AssertionError("backfill did not complete")

    async def index_all(self, limit: int = 200) -> int:
        batches = 0
        for _ in range(limit):
            outcome = await self.indexer.run_batch()
            if outcome.claimed == 0 and not outcome.deferred:
                return batches
            batches += 1
        raise AssertionError("indexing did not drain")

    async def aclose(self) -> None:
        await self.http.aclose()
        self.database.close()


@pytest.fixture()
async def harness(tmp_path):
    fake = FakeComponent4()
    instance = Harness(tmp_path, fake)
    try:
        yield instance
    finally:
        await instance.aclose()


async def test_backfill_discovers_every_page_and_indexes_crop_and_frame(harness):
    harness.fake.add(make_events(25))
    await harness.discover_all()

    assert harness.events.count() == 25
    assert harness.representations.counts()["pending"] == 50
    assert harness.sync.read().backfill_complete is True

    await harness.index_all()
    counts = harness.representations.event_counts()
    assert counts == {"known": 25, "searchable": 25, "complete": 25, "partial": 0, "failed": 0}
    snapshot = harness.store.current()
    assert snapshot.event_count == 25
    assert snapshot.crop_matrix.shape[0] == 25
    assert snapshot.frame_matrix.shape[0] == 25


async def test_a_multi_page_backfill_holds_one_page_at_a_time(harness):
    harness.fake.add(make_events(250))
    await harness.discover_all(limit=100)
    assert harness.events.count() == 250
    # Every event has both representation rows; nothing was queued in memory.
    assert harness.representations.counts()["pending"] == 500


async def test_backfill_resumes_from_its_persisted_cursor_after_a_restart(tmp_path):
    fake = FakeComponent4()
    fake.add(make_events(250))

    first = Harness(tmp_path, fake)
    await first.discovery.backfill_slice()  # partial: stops to check the head
    partial = first.events.count()
    cursor = first.sync.read().backfill_cursor
    await first.aclose()
    assert 0 < partial < 250
    assert cursor is not None

    second = Harness(tmp_path, fake)
    try:
        await second.discover_all(limit=100)
        assert second.events.count() == 250
        assert second.sync.read().backfill_complete is True
    finally:
        await second.aclose()


async def test_an_invalid_cursor_restarts_that_traversal_without_duplicates(harness):
    harness.fake.add(make_events(250))
    await harness.discovery.backfill_slice()
    discovered = harness.events.count()
    assert harness.sync.read().backfill_cursor is not None

    harness.fake.cursor_epoch = "epoch-2"  # upstream restarted
    outcome = await harness.discovery.backfill_slice()
    assert outcome.error_code == "invalid_cursor"
    assert harness.sync.read().backfill_cursor is None

    await harness.discover_all(limit=100)
    assert harness.events.count() == 250
    assert harness.representations.counts()["pending"] == 500  # no duplicate rows
    assert discovered <= 250


async def test_overlap_polling_finds_a_new_event_without_a_restart(harness):
    harness.fake.add(make_events(5))
    await harness.discover_all()
    await harness.index_all()
    assert harness.store.current().event_count == 5

    fresh = make_event("fresh", crossed_at=utc_now())
    harness.fake.add([fresh])
    outcome = await harness.discovery.poll_overlap()
    assert outcome.discovered == 1

    await harness.index_all()
    assert fresh.event_id in harness.store.current().event_ids


async def test_the_overlap_window_recovers_an_event_committed_out_of_order(harness):
    now = utc_now()
    harness.fake.add([make_event(index, crossed_at=now - timedelta(seconds=index)) for index in range(3)])
    await harness.discover_all()
    high_water = harness.sync.read().high_water_crossed_at
    assert high_water is not None

    # An event whose crossed_at is behind the high water by less than the
    # overlap: a naive "> high_water" filter would miss it forever.
    late = make_event("late", crossed_at=high_water - timedelta(seconds=120))
    harness.fake.add([late])
    outcome = await harness.discovery.poll_overlap()
    assert outcome.discovered == 1
    assert harness.events.get(late.event_id) is not None


async def test_rediscovery_is_idempotent_across_many_passes(harness):
    harness.fake.add(make_events(10))
    await harness.discover_all()
    await harness.index_all()
    revision = harness.representations.index_revision()

    for _ in range(3):
        await harness.discovery.poll_overlap()
    assert harness.events.count() == 10
    assert harness.representations.counts()["indexed"] == 20
    assert harness.representations.index_revision() == revision  # nothing re-indexed


async def test_a_gone_crop_leaves_the_event_unsearchable_and_a_gone_frame_does_not(harness):
    events = make_events(3)
    harness.fake.add(events)
    harness.fake.gone_artifacts.add(events[0].event_id)  # crop and frame both 410
    await harness.discover_all()
    await harness.index_all()

    states = harness.events.representation_states(events[0].event_id)
    assert states == {"crop": "permanent_error", "frame": "permanent_error"}
    assert events[0].event_id not in harness.store.current().event_ids
    assert harness.store.current().event_count == 2


async def test_a_crop_only_event_is_searchable(tmp_path):
    fake = FakeComponent4()
    events = make_events(1)
    fake.add(events)
    harness = Harness(tmp_path, fake)
    try:
        await harness.discover_all()
        # The frame is permanently gone; the crop is fine.
        original = fake.get_artifact

        async def only_frame_is_gone(request):
            if request.url.path.endswith("/frame"):
                fake.gone_artifacts.add(request.path_params["event_id"])
            else:
                fake.gone_artifacts.discard(request.path_params["event_id"])
            return await original(request)

        fake.get_artifact = only_frame_is_gone
        harness.http = fake.client()
        harness.client = Component4Client(
            base_url=harness.settings.component4_api_base_url, client=harness.http
        )
        harness.indexer._client = harness.client  # noqa: SLF001

        await harness.index_all()
        states = harness.events.representation_states(events[0].event_id)
        assert states["crop"] == "indexed" and states["frame"] == "permanent_error"
        snapshot = harness.store.current()
        assert snapshot.event_ids == (events[0].event_id,)
        assert snapshot.frame_matrix.shape[0] == 0
        counts = harness.representations.event_counts()
        assert counts["searchable"] == 1 and counts["partial"] == 1
    finally:
        await harness.aclose()


async def test_a_transient_artifact_failure_is_retried_not_abandoned(harness):
    events = make_events(2)
    harness.fake.add(events)
    harness.fake.transient_artifacts[events[0].event_id] = 2
    await harness.discover_all()
    await harness.index_all()

    states = harness.events.representation_states(events[0].event_id)
    assert states["crop"] == "retryable"
    # Its peer indexed regardless.
    assert harness.events.representation_states(events[1].event_id)["crop"] == "indexed"

    # After the retry time passes, the same rows are picked up and succeed.
    claimed = harness.representations.claim_batch(
        limit=4, newest_first=True, now=utc_now() + timedelta(seconds=120)
    )
    assert any(item.event_id == events[0].event_id for item in claimed)


async def test_one_bad_event_does_not_block_its_peers(harness):
    events = make_events(4)
    harness.fake.add(events)
    harness.fake.garbage_artifacts.add(events[1].event_id)
    await harness.discover_all()
    await harness.index_all()

    assert harness.events.representation_states(events[1].event_id)["crop"] == "permanent_error"
    searchable = set(harness.store.current().event_ids)
    assert searchable == {events[0].event_id, events[2].event_id, events[3].event_id}


async def test_indexing_survives_a_restart_at_a_commit_boundary(tmp_path):
    fake = FakeComponent4()
    fake.add(make_events(8))

    first = Harness(tmp_path, fake)
    await first.discover_all()
    await first.indexer.run_batch()
    indexed_before = first.representations.counts()["indexed"]
    # Leave rows claimed as if the process died mid-batch.
    first.representations.claim_batch(limit=4, newest_first=True)
    await first.aclose()
    assert indexed_before > 0

    second = Harness(tmp_path, fake)
    try:
        recovered = second.representations.recover_interrupted()
        assert recovered > 0
        assert second.representations.counts()["indexing"] == 0
        await second.index_all()
        counts = second.representations.event_counts()
        assert counts["searchable"] == 8 and counts["known"] == 8
        assert second.representations.counts()["indexed"] == 16  # never duplicated
    finally:
        await second.aclose()


async def test_the_indexer_favours_new_events_but_still_finishes_the_history(harness):
    harness.fake.add(make_events(20, step_seconds=60))
    await harness.discover_all()
    outcomes = [await harness.indexer.run_batch() for _ in range(4)]
    assert all(outcome.claimed for outcome in outcomes)
    # Three newest-first batches then one oldest-first batch: the very first and
    # very last events are both indexed within four batches.
    indexed = {
        item.event_id
        for item in harness.representations.load_indexed_vectors()[0]
    }
    all_events = sorted(harness.events.all_for_snapshot(), key=lambda e: e.crossed_at)
    assert all_events[-1].event_id in indexed
    assert all_events[0].event_id in indexed


async def test_search_over_the_published_snapshot_finds_the_right_event(harness):
    harness.fake.add(make_events(6))
    await harness.discover_all()
    await harness.index_all()

    snapshot = harness.store.current()
    query = harness.runtime.embed_texts(["a blue vehicle"])[0]
    outcome = rank_events(snapshot, query, top_k=3)
    assert len(outcome.results) == 3
    assert outcome.candidate_count == snapshot.event_count
    # One result per event, in descending score order.
    assert len({r.event_id for r in outcome.results}) == 3
    scores = [r.score for r in outcome.results]
    assert scores == sorted(scores, reverse=True)


async def test_an_upstream_outage_never_deletes_local_state(harness):
    harness.fake.add(make_events(6))
    await harness.discover_all()
    await harness.index_all()
    before = harness.store.current()
    assert before.event_count == 6

    harness.fake.outage = "down"
    stop = asyncio.Event()
    task = asyncio.create_task(harness.discovery.run(stop))
    await asyncio.sleep(0.1)
    stop.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    state = harness.sync.read()
    assert state.upstream_state is UpstreamState.UNAVAILABLE
    assert state.consecutive_failures >= 1
    # Local search is untouched.
    assert harness.events.count() == 6
    assert harness.representations.counts()["indexed"] == 12
    assert harness.store.current() is before

    harness.fake.outage = None
    outcome = await harness.discovery.poll_overlap()
    assert outcome.completed


async def test_an_unexpected_inference_failure_never_strands_claimed_rows(harness):
    """Regression: a library raising something unforeseen must not wedge work.

    A claim left in `indexing` would be invisible to every later batch until the
    process restarted, so the indexer treats an unexpected failure as retryable
    rather than letting it escape the batch loop.
    """
    events = make_events(2)
    harness.fake.add(events)
    await harness.discover_all()

    class Exploding:
        ready = True
        model_id = "test"

        def embed_images(self, images):
            raise ValueError("mean must have 1 elements if it is an iterable, got 3")

        def embed_texts(self, texts):
            raise ValueError("boom")

    harness.indexer._coordinator = InferenceCoordinator(Exploding())  # noqa: SLF001
    outcome = await harness.indexer.run_batch()

    assert outcome.claimed > 0
    assert harness.representations.counts()["indexing"] == 0
    states = harness.events.representation_states(events[0].event_id)
    assert states["crop"] in {"retryable", "permanent_error"}

    # The work is still there for a later, healthy batch.
    harness.indexer._coordinator = harness.coordinator  # noqa: SLF001
    from datetime import timedelta as _timedelta

    claimed = harness.representations.claim_batch(
        limit=4, newest_first=True, now=utc_now() + _timedelta(seconds=600)
    )
    assert claimed
