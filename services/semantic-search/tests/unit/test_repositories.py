"""Idempotent discovery, the representation state machine, and revision moves."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from app.domain.models import (
    EventState,
    RepresentationKind,
    RepresentationState,
    UpstreamState,
    utc_now,
)
from app.embeddings.manifest import EMBEDDING_BYTES, MANIFEST
from app.persistence.repositories import (
    VectorError,
    blob_to_vector,
    vector_digest,
    vector_to_blob,
)
from tests.fakes.factories import make_event, make_events, unit_vector


def test_a_discovered_event_creates_exactly_two_pending_representations(repositories):
    events, representations, _ = repositories
    inserted, refreshed = events.upsert_page([make_event(1)])
    assert (inserted, refreshed) == (1, 0)
    states = events.representation_states(make_event(1).event_id)
    assert states == {"crop": "pending", "frame": "pending"}
    assert representations.counts()["pending"] == 2


def test_rediscovering_an_event_never_creates_another_vector_row(repositories):
    events, representations, _ = repositories
    record = make_event(1)
    events.upsert_page([record])
    representations.mark_indexed(record.event_id, RepresentationKind.CROP, unit_vector(1))

    inserted, refreshed = events.upsert_page([record, record])
    assert (inserted, refreshed) == (0, 2)
    assert events.count() == 1
    counts = representations.counts()
    assert counts["indexed"] == 1 and counts["pending"] == 1
    # Rediscovery must not reset committed work.
    assert events.representation_states(record.event_id)["crop"] == "indexed"


def test_rediscovery_refreshes_presentation_metadata(repositories):
    events, _, _ = repositories
    original = make_event(1, camera_name="Old Name")
    events.upsert_page([original])
    renamed = make_event(1, camera_name="New Name", crossed_at=original.crossed_at)
    events.upsert_page([renamed])
    assert events.get(original.event_id).camera_name == "New Name"


def test_indexing_a_vector_bumps_the_index_revision(repositories):
    events, representations, _ = repositories
    record = make_event(1)
    events.upsert_page([record])
    before = representations.index_revision()
    representations.mark_indexed(record.event_id, RepresentationKind.CROP, unit_vector(2))
    after = representations.index_revision()
    assert after == before + 1
    representations.mark_indexed(record.event_id, RepresentationKind.FRAME, unit_vector(3))
    assert representations.index_revision() == after + 1


def test_a_permanent_error_on_an_indexed_row_also_bumps_the_revision(repositories):
    events, representations, _ = repositories
    record = make_event(1)
    events.upsert_page([record])
    representations.mark_indexed(record.event_id, RepresentationKind.CROP, unit_vector(4))
    revision = representations.index_revision()
    representations.mark_permanent_error(
        record.event_id, RepresentationKind.CROP, "upstream_event_not_found"
    )
    assert representations.index_revision() == revision + 1
    assert events.representation_states(record.event_id)["crop"] == "permanent_error"


def test_a_permanent_error_on_a_pending_row_does_not_bump_the_revision(repositories):
    events, representations, _ = repositories
    record = make_event(1)
    events.upsert_page([record])
    revision = representations.index_revision()
    representations.mark_permanent_error(
        record.event_id, RepresentationKind.FRAME, "event_artifact_gone"
    )
    assert representations.index_revision() == revision


def test_stored_vectors_are_768_little_endian_float32_with_a_checksum(repositories):
    events, representations, _ = repositories
    record = make_event(1)
    events.upsert_page([record])
    vector = unit_vector(5)
    representations.mark_indexed(record.event_id, RepresentationKind.CROP, vector)

    row = representations._db.connect().execute(  # noqa: SLF001 - storage assertion
        "SELECT embedding, dimension, dtype, l2_norm, embedding_sha256"
        " FROM representations WHERE event_id = ? AND kind = 'crop'",
        (record.event_id,),
    ).fetchone()
    blob = bytes(row["embedding"])
    assert len(blob) == EMBEDDING_BYTES == 768 * 4
    assert row["dimension"] == 768 and row["dtype"] == "float32"
    assert abs(row["l2_norm"] - 1.0) < 1e-3
    assert row["embedding_sha256"] == vector_digest(blob)
    assert np.allclose(np.frombuffer(blob, dtype="<f4"), vector, atol=1e-6)


def test_a_non_unit_or_non_finite_vector_is_never_stored(repositories):
    events, representations, _ = repositories
    record = make_event(1)
    events.upsert_page([record])
    with pytest.raises(VectorError):
        representations.mark_indexed(
            record.event_id, RepresentationKind.CROP, unit_vector(1) * 3.0
        )
    bad = unit_vector(1).copy()
    bad[0] = np.nan
    with pytest.raises(VectorError):
        representations.mark_indexed(record.event_id, RepresentationKind.CROP, bad)
    with pytest.raises(VectorError):
        representations.mark_indexed(
            record.event_id, RepresentationKind.CROP, np.ones(512, dtype=np.float32)
        )


def test_blob_round_trip_rejects_corruption():
    vector = unit_vector(7)
    blob = vector_to_blob(vector)
    assert np.allclose(blob_to_vector(blob), vector, atol=1e-6)
    with pytest.raises(VectorError):
        blob_to_vector(blob[:-4])
    with pytest.raises(VectorError):
        blob_to_vector(b"\x00" * EMBEDDING_BYTES)  # zero norm


def test_a_corrupt_stored_vector_is_excluded_not_fatal(repositories):
    events, representations, _ = repositories
    records = make_events(2)
    events.upsert_page(records)
    representations.mark_indexed(records[0].event_id, RepresentationKind.CROP, unit_vector(8))
    representations.mark_indexed(records[1].event_id, RepresentationKind.CROP, unit_vector(9))
    with representations._db.transaction() as conn:  # noqa: SLF001 - fault injection
        conn.execute(
            "UPDATE representations SET embedding = ? WHERE event_id = ? AND kind = 'crop'",
            (b"\x01" * EMBEDDING_BYTES, records[1].event_id),
        )

    accepted, invalid = representations.load_indexed_vectors()
    assert [item.event_id for item in accepted] == [records[0].event_id]
    assert invalid == [(records[1].event_id, RepresentationKind.CROP)]

    reset = representations.reset_to_pending(invalid, "vector_checksum_mismatch")
    assert reset == 1
    assert events.representation_states(records[1].event_id)["crop"] == "pending"


def test_claiming_prefers_newest_or_oldest_and_marks_rows_indexing(repositories):
    events, representations, _ = repositories
    records = make_events(6, step_seconds=60)
    events.upsert_page(records)

    newest = representations.claim_batch(limit=2, newest_first=True)
    assert {item.event_id for item in newest} == {records[-1].event_id, records[-2].event_id}
    assert all(item.kind in RepresentationKind for item in newest)
    assert len(newest) == 4  # two events x crop+frame
    states = events.representation_states(records[-1].event_id)
    assert states == {"crop": "indexing", "frame": "indexing"}

    # A claimed row is not offered again.
    oldest = representations.claim_batch(limit=2, newest_first=False)
    assert {item.event_id for item in oldest} == {records[0].event_id, records[1].event_id}


def test_interrupted_indexing_rows_are_recovered_as_retryable(repositories):
    events, representations, _ = repositories
    records = make_events(2)
    events.upsert_page(records)
    representations.claim_batch(limit=2, newest_first=True)

    recovered = representations.recover_interrupted()
    assert recovered == 4
    states = events.representation_states(records[0].event_id)
    assert states == {"crop": "retryable", "frame": "retryable"}
    row = representations._db.connect().execute(  # noqa: SLF001
        "SELECT last_error_code FROM representations WHERE event_id = ?",
        (records[0].event_id,),
    ).fetchone()
    assert row["last_error_code"] == "interrupted_indexing"


def test_the_retry_ladder_is_2_4_8_16_30_60_120_300_then_stays(repositories):
    events, representations, _ = repositories
    record = make_event(1)
    events.upsert_page([record])
    expected = [2, 4, 8, 16, 30, 60, 120, 300, 300, 300]
    for attempt, delay in enumerate(expected):
        before = utc_now()
        retry_at = representations.mark_retryable(
            record.event_id, RepresentationKind.CROP, "upstream_unavailable",
            attempt=attempt,
        )
        assert timedelta(seconds=delay - 2) <= retry_at - before <= timedelta(seconds=delay + 2)


def test_a_retryable_row_is_not_claimed_before_its_retry_time(repositories):
    events, representations, _ = repositories
    record = make_event(1)
    events.upsert_page([record])
    representations.claim_batch(limit=1, newest_first=True)
    representations.mark_retryable(
        record.event_id, RepresentationKind.CROP, "upstream_unavailable", attempt=3
    )
    representations.mark_permanent_error(
        record.event_id, RepresentationKind.FRAME, "event_artifact_gone"
    )
    assert representations.claim_batch(limit=4, newest_first=True) == []
    later = utc_now() + timedelta(seconds=60)
    assert representations.claim_batch(limit=4, newest_first=True, now=later)


def test_event_state_is_computed_from_representations(repositories):
    events, representations, _ = repositories
    records = make_events(3)
    events.upsert_page(records)
    representations.mark_indexed(records[0].event_id, RepresentationKind.CROP, unit_vector(1))
    representations.mark_indexed(records[0].event_id, RepresentationKind.FRAME, unit_vector(2))
    representations.mark_indexed(records[1].event_id, RepresentationKind.CROP, unit_vector(3))
    representations.mark_permanent_error(
        records[1].event_id, RepresentationKind.FRAME, "event_artifact_gone"
    )
    representations.mark_permanent_error(
        records[2].event_id, RepresentationKind.CROP, "event_artifact_gone"
    )

    state_of = lambda record: events.event_state(events.representation_states(record.event_id))
    assert state_of(records[0]) is EventState.COMPLETE
    assert state_of(records[1]) is EventState.PARTIAL
    assert state_of(records[2]) is EventState.FAILED

    counts = representations.event_counts()
    assert counts == {"known": 3, "searchable": 2, "complete": 1, "partial": 1, "failed": 1}


def test_sync_state_round_trips_and_high_water_only_moves_forward(repositories):
    _, _, sync = repositories
    state = sync.read()
    assert state.backfill_complete is False
    assert state.upstream_state is UpstreamState.UNKNOWN

    moment = utc_now()
    sync.update(
        backfill_complete=True,
        backfill_cursor=None,
        upstream_state=UpstreamState.AVAILABLE,
        last_successful_poll_at=moment,
        consecutive_failures=0,
    )
    sync.advance_high_water(moment)
    sync.advance_high_water(moment - timedelta(hours=1))

    state = sync.read()
    assert state.backfill_complete is True
    assert state.upstream_state is UpstreamState.AVAILABLE
    assert state.high_water_crossed_at == moment
    assert state.last_successful_poll_at == moment


def test_unknown_sync_fields_are_refused(repositories):
    _, _, sync = repositories
    with pytest.raises(KeyError):
        sync.update(not_a_field="x")


def test_representation_rows_are_scoped_to_the_model_id(repositories):
    events, representations, _ = repositories
    record = make_event(1)
    events.upsert_page([record])
    with representations._db.transaction() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO representations (event_id, kind, model_id, state, updated_at)"
            " VALUES (?, 'crop', 'other-model', 'indexed', '2026-09-07T00:00:00.000Z')",
            (record.event_id,),
        )
    # A foreign model's row is never loaded, claimed, or counted.
    accepted, invalid = representations.load_indexed_vectors()
    assert accepted == [] and invalid == []
    assert representations.counts()["indexed"] == 0
    assert MANIFEST.model_id != "other-model"
