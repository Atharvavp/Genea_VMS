"""Snapshot construction, pre-filtering, max fusion, thresholds, and ties."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from app.domain.models import (
    Direction,
    ObjectCategory,
    ObjectClass,
    RepresentationKind,
    SearchFilters,
    utc_now,
)
from app.embeddings.manifest import EMBEDDING_DIM
from app.persistence.repositories import IndexedVector
from app.retrieval.ranking import rank_events, select_candidates
from app.retrieval.snapshot import SnapshotStore, build_snapshot
from tests.fakes.factories import make_event, make_events, unit_vector

BUILT_AT = utc_now()


def basis(index: int) -> np.ndarray:
    """A unit vector along one axis, so cosines are exactly controllable."""
    vector = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    vector[index] = 1.0
    return vector


def snapshot_of(events, vectors, revision=1):
    return build_snapshot(
        events=events, vectors=vectors, index_revision=revision, built_at=BUILT_AT
    )


def test_only_events_with_an_indexed_crop_are_searchable():
    events = make_events(3)
    vectors = [
        IndexedVector(events[0].event_id, RepresentationKind.CROP, unit_vector(1)),
        IndexedVector(events[0].event_id, RepresentationKind.FRAME, unit_vector(2)),
        # events[1] has only a frame vector - it is NOT searchable.
        IndexedVector(events[1].event_id, RepresentationKind.FRAME, unit_vector(3)),
        IndexedVector(events[2].event_id, RepresentationKind.CROP, unit_vector(4)),
    ]
    snapshot = snapshot_of(events, vectors)
    assert set(snapshot.event_ids) == {events[0].event_id, events[2].event_id}
    assert snapshot.crop_matrix.shape == (2, EMBEDDING_DIM)
    assert snapshot.frame_matrix.shape == (1, EMBEDDING_DIM)


def test_a_snapshot_is_immutable_and_contiguous():
    events = make_events(2)
    vectors = [
        IndexedVector(event.event_id, RepresentationKind.CROP, unit_vector(i))
        for i, event in enumerate(events)
    ]
    snapshot = snapshot_of(events, vectors)
    assert snapshot.crop_matrix.flags["C_CONTIGUOUS"]
    assert not snapshot.crop_matrix.flags["WRITEABLE"]
    with pytest.raises(ValueError):
        snapshot.crop_matrix[0, 0] = 5.0
    with pytest.raises(Exception):
        snapshot.index_revision = 99  # frozen dataclass


def test_snapshot_building_is_deterministic():
    events = make_events(5)
    vectors = [
        IndexedVector(event.event_id, kind, unit_vector(index))
        for index, event in enumerate(events)
        for kind in (RepresentationKind.CROP, RepresentationKind.FRAME)
    ]
    first = snapshot_of(events, vectors)
    second = snapshot_of(events, list(reversed(vectors)))
    assert first.event_ids == second.event_ids
    assert np.array_equal(first.crop_matrix, second.crop_matrix)
    assert np.array_equal(first.frame_matrix, second.frame_matrix)
    assert list(first.event_ids) == sorted(first.event_ids)


def test_an_event_appears_at_most_once_even_with_crop_and_frame():
    event = make_event(1)
    query = basis(0)
    vectors = [
        IndexedVector(event.event_id, RepresentationKind.CROP, basis(0)),
        IndexedVector(event.event_id, RepresentationKind.FRAME, basis(0)),
    ]
    outcome = rank_events(snapshot_of([event], vectors), query, top_k=10)
    assert len(outcome.results) == 1
    assert outcome.candidate_count == 1


def test_the_event_score_is_the_max_of_its_representations():
    event = make_event(1)
    crop = basis(0)
    frame = (basis(0) + basis(1)) / np.sqrt(2)
    query = basis(1).astype(np.float32)
    snapshot = snapshot_of(
        [event],
        [
            IndexedVector(event.event_id, RepresentationKind.CROP, crop),
            IndexedVector(event.event_id, RepresentationKind.FRAME, frame.astype(np.float32)),
        ],
    )
    result = rank_events(snapshot, query, top_k=5).results[0]
    assert result.crop_score == pytest.approx(0.0, abs=1e-6)
    assert result.frame_score == pytest.approx(1 / np.sqrt(2), abs=1e-6)
    assert result.score == pytest.approx(result.frame_score, abs=1e-6)


def test_a_crop_only_event_scores_from_its_crop_alone():
    event = make_event(1)
    snapshot = snapshot_of(
        [event], [IndexedVector(event.event_id, RepresentationKind.CROP, basis(3))]
    )
    result = rank_events(snapshot, basis(3), top_k=5).results[0]
    assert result.frame_score is None
    assert result.crop_score == pytest.approx(1.0, abs=1e-6)
    assert result.score == pytest.approx(1.0, abs=1e-6)


def test_filters_are_applied_before_scoring():
    events = [
        make_event(0, camera=1, object_class=ObjectClass.CAR, direction=Direction.A_TO_B),
        make_event(1, camera=2, object_class=ObjectClass.PERSON, direction=Direction.B_TO_A),
        make_event(2, camera=1, object_class=ObjectClass.BUS, direction=Direction.A_TO_B),
    ]
    vectors = [
        IndexedVector(event.event_id, RepresentationKind.CROP, basis(index))
        for index, event in enumerate(events)
    ]
    snapshot = snapshot_of(events, vectors)

    outcome = rank_events(
        snapshot,
        basis(1),
        filters=SearchFilters(camera_id=events[0].camera_id),
        top_k=10,
    )
    # Only the two camera-1 events were candidates, even though event 1 is the
    # perfect match for this query.
    assert outcome.candidate_count == 2
    assert events[1].event_id not in {r.event_id for r in outcome.results}


def test_every_filter_dimension_narrows_the_candidate_set():
    events = make_events(8, step_seconds=3600)
    vectors = [
        IndexedVector(event.event_id, RepresentationKind.CROP, unit_vector(i))
        for i, event in enumerate(events)
    ]
    snapshot = snapshot_of(events, vectors)

    assert select_candidates(snapshot, SearchFilters()).size == 8
    assert select_candidates(
        snapshot, SearchFilters(camera_id=events[0].camera_id)
    ).size == 2
    assert select_candidates(
        snapshot, SearchFilters(direction=Direction.A_TO_B)
    ).size == 4
    assert select_candidates(
        snapshot, SearchFilters(object_class=ObjectClass.CAR)
    ).size == sum(1 for e in events if e.object_class is ObjectClass.CAR)


def test_the_time_window_is_inclusive_from_and_exclusive_to():
    events = make_events(3, step_seconds=60)
    vectors = [
        IndexedVector(event.event_id, RepresentationKind.CROP, unit_vector(i))
        for i, event in enumerate(events)
    ]
    snapshot = snapshot_of(events, vectors)
    exact = SearchFilters(start=events[0].crossed_at, end=events[2].crossed_at)
    chosen = select_candidates(snapshot, exact)
    assert {snapshot.event_ids[i] for i in chosen} == {
        events[0].event_id, events[1].event_id
    }


def test_a_contradictory_category_and_class_returns_empty_not_rewritten():
    events = [make_event(0, object_class=ObjectClass.CAR)]
    snapshot = snapshot_of(
        events, [IndexedVector(events[0].event_id, RepresentationKind.CROP, basis(0))]
    )
    outcome = rank_events(
        snapshot,
        basis(0),
        filters=SearchFilters(
            object_category=ObjectCategory.PERSON, object_class=ObjectClass.CAR
        ),
    )
    assert outcome.results == () and outcome.candidate_count == 0


def test_min_score_is_applied_after_fusion_and_top_k_after_that():
    events = make_events(4)
    query = basis(0)
    scores = [1.0, 0.8, 0.5, 0.1]
    vectors = []
    for event, score in zip(events, scores):
        vector = np.zeros(EMBEDDING_DIM, dtype=np.float32)
        vector[0] = score
        vector[1] = float(np.sqrt(max(0.0, 1 - score * score)))
        vectors.append(IndexedVector(event.event_id, RepresentationKind.CROP, vector))
    snapshot = snapshot_of(events, vectors)

    assert len(rank_events(snapshot, query, top_k=10).results) == 4
    thresholded = rank_events(snapshot, query, top_k=10, min_score=0.5)
    assert len(thresholded.results) == 3
    assert thresholded.candidate_count == 4  # candidates counted before threshold
    assert len(rank_events(snapshot, query, top_k=2, min_score=0.5).results) == 2


def test_results_are_ordered_by_score_then_time_then_id():
    now = utc_now()
    # Three events with an identical score; only crossed_at and id separate them.
    events = [
        make_event(index, crossed_at=now - timedelta(seconds=index % 2))
        for index in range(3)
    ]
    vectors = [
        IndexedVector(event.event_id, RepresentationKind.CROP, basis(0))
        for event in events
    ]
    snapshot = snapshot_of(events, vectors)
    ranked = rank_events(snapshot, basis(0), top_k=10).results
    assert all(r.score == pytest.approx(1.0) for r in ranked)

    ordering = [(-r.score, -snapshot.crossed_at_ms[r.event_index], r.event_id) for r in ranked]
    assert ordering == sorted(ordering)
    # Repeated runs are identical.
    again = rank_events(snapshot, basis(0), top_k=10).results
    assert [r.event_id for r in again] == [r.event_id for r in ranked]


def test_negative_scores_are_kept_unless_a_threshold_excludes_them():
    event = make_event(1)
    snapshot = snapshot_of(
        [event], [IndexedVector(event.event_id, RepresentationKind.CROP, basis(0))]
    )
    opposite = -basis(0)
    assert rank_events(snapshot, opposite, top_k=5).results[0].score == pytest.approx(-1.0)
    assert rank_events(snapshot, opposite, top_k=5, min_score=0.0).results == ()


def test_an_empty_snapshot_searches_successfully_and_returns_nothing():
    snapshot = build_snapshot(events=[], vectors=[], index_revision=0, built_at=BUILT_AT)
    assert snapshot.is_empty
    outcome = rank_events(snapshot, basis(0), top_k=10)
    assert outcome.results == () and outcome.candidate_count == 0


def test_a_malformed_query_vector_is_refused():
    event = make_event(1)
    snapshot = snapshot_of(
        [event], [IndexedVector(event.event_id, RepresentationKind.CROP, basis(0))]
    )
    with pytest.raises(ValueError):
        rank_events(snapshot, np.zeros(512, dtype=np.float32))
    bad = basis(0).copy()
    bad[0] = np.nan
    with pytest.raises(ValueError):
        rank_events(snapshot, bad)


def test_a_vector_whose_event_is_unknown_is_skipped():
    events = make_events(1)
    stray = IndexedVector("evt_" + "9" * 32, RepresentationKind.CROP, unit_vector(1))
    snapshot = snapshot_of(
        events,
        [IndexedVector(events[0].event_id, RepresentationKind.CROP, unit_vector(2)), stray],
    )
    assert snapshot.event_ids == (events[0].event_id,)


def test_publishing_swaps_atomically_and_never_mutates_the_old_snapshot():
    events = make_events(2)
    first = snapshot_of(
        events[:1],
        [IndexedVector(events[0].event_id, RepresentationKind.CROP, basis(0))],
        revision=1,
    )
    store = SnapshotStore(first)
    held = store.current()

    second = snapshot_of(
        events,
        [
            IndexedVector(event.event_id, RepresentationKind.CROP, basis(index))
            for index, event in enumerate(events)
        ],
        revision=2,
    )
    previous = store.publish(second)

    assert previous is first
    assert store.current() is second
    # The reference an in-flight search already took is unchanged.
    assert held.event_count == 1 and held.index_revision == 1
    assert store.current().event_count == 2
