"""Row mapping, CRUD, ordering, filters, and cursor seek."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.domain.models import CrossingDirection, ObjectCategory, to_iso_ms
from app.persistence.camera_repository import (
    CameraRepository,
    DuplicateVmsCameraId,
)
from app.persistence.database import Database, DataIntegrityError
from app.persistence.event_repository import EventFilter, EventRepository
from app.persistence.line_repository import LineRepository
from tests.fakes import factories

pytestmark = pytest.mark.unit


@pytest.fixture
def repos(tmp_path: Path):
    database = Database(tmp_path / "analytics.db")
    database.initialize()
    return (
        database,
        CameraRepository(database),
        LineRepository(database),
        EventRepository(database),
    )


# -- cameras ----------------------------------------------------------------


def test_camera_round_trip_preserves_every_field(repos):
    _db, cameras, _lines, _events = repos
    record = factories.camera(classes=frozenset({ObjectCategory.PERSON}))
    cameras.insert(record)
    loaded = cameras.get(record.id)
    assert loaded == record


def test_a_credentialed_url_round_trips_verbatim(repos):
    _db, cameras, _lines, _events = repos
    url = "rtsp://admin:hunter2@10.0.0.9:554/live"
    cameras.insert(factories.camera(rtsp_url=url))
    assert cameras.get("acam_0123abcd").rtsp_url == url


def test_cameras_are_listed_by_case_folded_name_then_id(repos):
    _db, cameras, _lines, _events = repos
    cameras.insert(factories.camera("acam_00000003", vms_camera_id="cam_00000003", name="apple"))
    cameras.insert(factories.camera("acam_00000001", vms_camera_id="cam_00000001", name="Banana"))
    cameras.insert(factories.camera("acam_00000002", vms_camera_id="cam_00000002", name="apple"))
    assert [c.id for c in cameras.list()] == [
        "acam_00000002",
        "acam_00000003",
        "acam_00000001",
    ]


def test_duplicate_vms_camera_id_raises_a_typed_error(repos):
    _db, cameras, _lines, _events = repos
    cameras.insert(factories.camera("acam_00000001"))
    with pytest.raises(DuplicateVmsCameraId):
        cameras.insert(factories.camera("acam_00000002"))


def test_update_preserves_created_at_and_changes_the_rest(repos):
    _db, cameras, _lines, _events = repos
    original = factories.camera()
    cameras.insert(original)
    later = original.created_at + timedelta(minutes=5)
    updated = factories.camera(
        name="Renamed", enabled=False, created_at=original.created_at, updated_at=later
    )
    cameras.update(updated)
    loaded = cameras.get(original.id)
    assert loaded.name == "Renamed"
    assert loaded.enabled is False
    assert loaded.created_at == original.created_at
    assert loaded.updated_at == later


def test_updating_a_missing_camera_is_an_integrity_error(repos):
    _db, cameras, _lines, _events = repos
    with pytest.raises(DataIntegrityError):
        cameras.update(factories.camera("acam_ffffffff", vms_camera_id="cam_ffffffff"))


def test_delete_requires_exactly_one_row(repos):
    _db, cameras, _lines, _events = repos
    cameras.insert(factories.camera())
    cameras.delete("acam_0123abcd")
    with pytest.raises(DataIntegrityError):
        cameras.delete("acam_0123abcd")


def test_lookup_by_vms_camera_id(repos):
    _db, cameras, _lines, _events = repos
    cameras.insert(factories.camera())
    assert cameras.get_by_vms_camera_id("cam_0123abcd").id == "acam_0123abcd"
    assert cameras.get_by_vms_camera_id("cam_99999999") is None


@pytest.mark.parametrize(
    "column,value",
    [
        ("enabled_classes_json", '["train"]'),
        ("created_at", "not-a-timestamp"),
        ("enabled", 5),
    ],
)
def test_corrupted_persisted_values_raise_integrity_errors(repos, column, value):
    database, cameras, _lines, _events = repos
    cameras.insert(factories.camera())
    with database.connect() as connection:
        connection.execute("PRAGMA writable_schema = OFF")
    connection = database.connect()
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(f"UPDATE analytics_cameras SET {column} = ?", (value,))
    finally:
        connection.close()
    with pytest.raises(DataIntegrityError):
        cameras.get("acam_0123abcd")


# -- lines ------------------------------------------------------------------


def test_line_upsert_preserves_id_and_created_at(repos):
    _db, cameras, lines, _events = repos
    cameras.insert(factories.camera())
    first = lines.upsert(factories.line())
    later = first.created_at + timedelta(minutes=1)
    second = lines.upsert(
        factories.line(line_id="line_99999999", name="Moved", a=(0.1, 0.1), b=(0.9, 0.9))
        .__class__(
            id="line_99999999",
            camera_id="acam_0123abcd",
            name="Moved",
            x1=0.1,
            y1=0.1,
            x2=0.9,
            y2=0.9,
            direction=first.direction,
            enabled=True,
            created_at=later,
            updated_at=later,
        )
    )
    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.name == "Moved"
    assert lines.get("acam_0123abcd").x1 == 0.1


def test_line_delete_is_idempotent(repos):
    _db, cameras, lines, _events = repos
    cameras.insert(factories.camera())
    lines.upsert(factories.line())
    assert lines.delete("acam_0123abcd") is True
    assert lines.delete("acam_0123abcd") is False


def test_list_all_lines_is_keyed_by_camera(repos):
    _db, cameras, lines, _events = repos
    cameras.insert(factories.camera())
    lines.upsert(factories.line())
    assert set(lines.list_all()) == {"acam_0123abcd"}


# -- events -----------------------------------------------------------------


def _seed_events(events: EventRepository, count: int = 5):
    records = []
    for index in range(count):
        record = factories.event(
            event_id=f"evt_{index:032x}",
            track_id=index,
            offset_ms=index * 1000,
            object_class="person" if index % 2 else "truck",
            category=ObjectCategory.PERSON if index % 2 else ObjectCategory.VEHICLE,
            direction=CrossingDirection.B_TO_A if index % 2 else CrossingDirection.A_TO_B,
        )
        assert events.insert(record) is True
        records.append(record)
    return records


def test_events_round_trip_and_order_newest_first(repos):
    _db, _cameras, _lines, events = repos
    _seed_events(events)
    page = events.query(EventFilter(), limit=10)
    assert [record.id for record in page.items] == [
        f"evt_{index:032x}" for index in (4, 3, 2, 1, 0)
    ]
    assert page.has_more is False


def test_duplicate_dedupe_key_is_reported_not_raised(repos):
    _db, _cameras, _lines, events = repos
    record = factories.event()
    assert events.insert(record) is True
    duplicate = factories.event(event_id="evt_" + "b" * 32)
    assert events.insert(duplicate) is False


def test_find_by_dedupe_key(repos):
    _db, _cameras, _lines, events = repos
    record = factories.event()
    events.insert(record)
    found = events.find_by_dedupe_key(
        record.camera_id,
        record.line_id,
        record.worker_session_id,
        record.track_id,
        record.direction,
    )
    assert found is not None and found.id == record.id
    assert (
        events.find_by_dedupe_key(
            record.camera_id,
            record.line_id,
            record.worker_session_id,
            999,
            record.direction,
        )
        is None
    )


def test_filters_combine(repos):
    _db, _cameras, _lines, events = repos
    _seed_events(events)
    page = events.query(
        EventFilter(
            object_category=ObjectCategory.PERSON,
            direction=CrossingDirection.B_TO_A,
            object_class="person",
        ),
        limit=10,
    )
    assert {record.track_id for record in page.items} == {1, 3}


def test_time_bounds_are_half_open(repos):
    _db, _cameras, _lines, events = repos
    records = _seed_events(events)
    start = records[1].crossed_at
    end = records[3].crossed_at
    page = events.query(EventFilter(start=start, end=end), limit=10)
    assert {record.track_id for record in page.items} == {1, 2}


def test_cursor_seek_pages_without_repeats_or_gaps(repos):
    _db, _cameras, _lines, events = repos
    _seed_events(events, 5)
    first = events.query(EventFilter(), limit=2)
    assert first.has_more is True
    last = first.items[-1]
    second = events.query(
        EventFilter(),
        limit=2,
        cursor_crossed_at=last.crossed_at,
        cursor_id=last.id,
    )
    assert [record.track_id for record in first.items] == [4, 3]
    assert [record.track_id for record in second.items] == [2, 1]


def test_the_seek_breaks_ties_on_id(repos):
    _db, _cameras, _lines, events = repos
    moment = datetime(2026, 9, 7, 9, 0, 0, tzinfo=UTC)
    for index, suffix in enumerate("abc"):
        events.insert(
            factories.event(
                event_id="evt_" + suffix * 32, track_id=index, crossed_at=moment
            )
        )
    page = events.query(EventFilter(), limit=2)
    assert [record.id for record in page.items] == ["evt_" + "c" * 32, "evt_" + "b" * 32]
    tail = events.query(
        EventFilter(),
        limit=2,
        cursor_crossed_at=page.items[-1].crossed_at,
        cursor_id=page.items[-1].id,
    )
    assert [record.id for record in tail.items] == ["evt_" + "a" * 32]


def test_events_of_a_deleted_camera_are_still_returned(repos):
    _db, cameras, _lines, events = repos
    cameras.insert(factories.camera())
    events.insert(factories.event())
    cameras.delete("acam_0123abcd")
    page = events.query(EventFilter(camera_id="acam_0123abcd"), limit=10)
    assert len(page.items) == 1
    assert page.items[0].camera_name == "Loading Bay"


def test_limit_bounds_are_enforced(repos):
    _db, _cameras, _lines, events = repos
    with pytest.raises(ValueError):
        events.query(EventFilter(), limit=0)
    with pytest.raises(ValueError):
        events.query(EventFilter(), limit=101)


def test_event_filter_identity_is_stable_and_distinguishing():
    a = EventFilter(camera_id="acam_00000001")
    b = EventFilter(camera_id="acam_00000001")
    c = EventFilter(camera_id="acam_00000002")
    assert a.identity() == b.identity() != c.identity()


def test_all_event_ids_and_paths(repos):
    _db, _cameras, _lines, events = repos
    _seed_events(events, 3)
    assert len(events.all_event_ids()) == 3
    rows = list(events.iter_paths())
    assert len(rows) == 3
    assert all(path.startswith("events/") for _id, path, _crop in rows)


def test_timestamps_are_stored_with_millisecond_precision(repos):
    _db, _cameras, _lines, events = repos
    record = factories.event()
    events.insert(record)
    assert to_iso_ms(events.get(record.id).crossed_at).endswith("Z")
    assert events.get(record.id).crossed_at == record.crossed_at
