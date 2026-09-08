"""Schema version 1, pragmas, constraints, and history survival."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.persistence.database import (
    REQUIRED_INDEXES,
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    Database,
    SchemaIncompatibleError,
)

pytestmark = pytest.mark.unit

NOW = "2026-09-07T09:15:14.123Z"


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state" / "analytics.db")
    db.initialize()
    return db


def _camera(connection: sqlite3.Connection, camera_id: str = "acam_0123abcd", **over):
    values = {
        "id": camera_id,
        "vms_camera_id": over.get("vms_camera_id", "cam_0123abcd"),
        "name": over.get("name", "Loading Bay"),
        "rtsp_url": over.get("rtsp_url", "rtsp://cam/live"),
        "enabled": over.get("enabled", 1),
        "inference_fps": over.get("inference_fps", 5.0),
        "confidence_threshold": over.get("confidence_threshold", 0.25),
        "enabled_classes_json": over.get("enabled_classes_json", '["person","vehicle"]'),
        "created_at": NOW,
        "updated_at": NOW,
    }
    connection.execute(
        "INSERT INTO analytics_cameras (id, vms_camera_id, name, rtsp_url, enabled, "
        "inference_fps, confidence_threshold, enabled_classes_json, created_at, "
        "updated_at) VALUES (:id, :vms_camera_id, :name, :rtsp_url, :enabled, "
        ":inference_fps, :confidence_threshold, :enabled_classes_json, :created_at, "
        ":updated_at)",
        values,
    )
    return camera_id


def _line(connection: sqlite3.Connection, camera_id: str, line_id="line_00000001", **over):
    connection.execute(
        "INSERT INTO line_configs (id, camera_id, name, x1, y1, x2, y2, direction, "
        "enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            line_id,
            camera_id,
            over.get("name", "Entry line"),
            over.get("x1", 0.2),
            over.get("y1", 0.5),
            over.get("x2", 0.8),
            over.get("y2", 0.5),
            over.get("direction", "A_TO_B"),
            over.get("enabled", 1),
            NOW,
            NOW,
        ),
    )
    return line_id


def _event(connection: sqlite3.Connection, camera_id: str, line_id: str, **over):
    event_id = over.get("id", "evt_" + "0" * 32)
    connection.execute(
        "INSERT INTO events (id, camera_id, vms_camera_id, camera_name, line_id, "
        "line_name, worker_session_id, track_id, object_category, object_class, "
        "direction, confidence, crossed_at, bbox_x1, bbox_y1, bbox_x2, bbox_y2, "
        "centroid_x, centroid_y, frame_width, frame_height, source_pts_ns, "
        "snapshot_path, crop_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            camera_id,
            over.get("vms_camera_id", "cam_0123abcd"),
            "Loading Bay",
            line_id,
            "Entry line",
            over.get("worker_session_id", "ws_" + "1" * 32),
            over.get("track_id", 7),
            over.get("object_category", "vehicle"),
            over.get("object_class", "truck"),
            over.get("direction", "A_TO_B"),
            over.get("confidence", 0.91),
            over.get("crossed_at", NOW),
            over.get("bbox_x1", 0.10),
            over.get("bbox_y1", 0.20),
            over.get("bbox_x2", 0.30),
            over.get("bbox_y2", 0.60),
            0.20,
            0.40,
            1920,
            1080,
            None,
            "events/a/b/c/frame.jpg",
            "events/a/b/c/crop.jpg",
            NOW,
        ),
    )
    return event_id


# -- schema -----------------------------------------------------------------


def test_a_new_database_creates_the_schema_exactly_once(tmp_path: Path):
    db = Database(tmp_path / "analytics.db")
    assert db.initialize() == SCHEMA_VERSION
    assert db.initialize() == SCHEMA_VERSION
    with db.read() as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
    for table in REQUIRED_TABLES:
        assert table in names
    for index in REQUIRED_INDEXES:
        assert index in names


def test_every_connection_carries_the_required_pragmas(database: Database):
    with database.read() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert str(
            connection.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert connection.isolation_level is None


def test_a_future_schema_version_is_fatal(tmp_path: Path):
    db = Database(tmp_path / "analytics.db")
    db.initialize()
    with db.write() as connection:
        connection.execute("PRAGMA user_version = 2")
    with pytest.raises(SchemaIncompatibleError):
        db.initialize()


def test_existing_tables_with_version_zero_are_refused(tmp_path: Path):
    path = tmp_path / "analytics.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE analytics_cameras (id TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(SchemaIncompatibleError):
        Database(path).initialize()


def test_the_data_directory_is_created(tmp_path: Path):
    db = Database(tmp_path / "nested" / "deeper" / "analytics.db")
    db.initialize()
    assert (tmp_path / "nested" / "deeper").is_dir()


# -- foreign keys and history ----------------------------------------------


def test_deleting_a_camera_cascades_its_line(database: Database):
    with database.write() as connection:
        camera_id = _camera(connection)
        _line(connection, camera_id)
    with database.write() as connection:
        connection.execute("DELETE FROM analytics_cameras WHERE id = ?", (camera_id,))
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM line_configs").fetchone()[0] == 0


def test_events_survive_camera_and_line_deletion(database: Database):
    with database.write() as connection:
        camera_id = _camera(connection)
        line_id = _line(connection, camera_id)
        _event(connection, camera_id, line_id)
    with database.write() as connection:
        connection.execute("DELETE FROM analytics_cameras WHERE id = ?", (camera_id,))
    with database.read() as connection:
        row = connection.execute(
            "SELECT camera_name, line_name FROM events"
        ).fetchone()
    assert row["camera_name"] == "Loading Bay"
    assert row["line_name"] == "Entry line"


def test_events_declare_no_foreign_keys(database: Database):
    with database.read() as connection:
        keys = connection.execute("PRAGMA foreign_key_list(events)").fetchall()
    assert keys == []


def test_a_line_is_a_singleton_per_camera(database: Database):
    with database.write() as connection:
        camera_id = _camera(connection)
        _line(connection, camera_id)
    with pytest.raises(sqlite3.IntegrityError):
        with database.write() as connection:
            _line(connection, camera_id, line_id="line_00000002")


# -- CHECK constraints ------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"vms_camera_id": "cam_ZZZZZZZZ"},
        {"vms_camera_id": "cam_012"},
        {"name": "   "},
        {"name": "x" * 101},
        {"rtsp_url": ""},
        {"enabled": 2},
        {"inference_fps": 0.5},
        {"inference_fps": 10.5},
        {"confidence_threshold": 0.05},
        {"confidence_threshold": 0.99},
        {"enabled_classes_json": '["truck"]'},
        {"enabled_classes_json": '["vehicle","person"]'},
    ],
)
def test_camera_check_constraints_reject_bad_values(database: Database, override):
    with pytest.raises(sqlite3.IntegrityError):
        with database.write() as connection:
            _camera(connection, **override)


@pytest.mark.parametrize("camera_id", ["cam_0123abcd", "acam_ZZZZZZZZ", "acam_012"])
def test_camera_id_shape_is_enforced(database: Database, camera_id: str):
    with pytest.raises(sqlite3.IntegrityError):
        with database.write() as connection:
            _camera(connection, camera_id)


def test_duplicate_vms_camera_id_is_rejected(database: Database):
    with database.write() as connection:
        _camera(connection, "acam_00000001")
    with pytest.raises(sqlite3.IntegrityError):
        with database.write() as connection:
            _camera(connection, "acam_00000002")


@pytest.mark.parametrize(
    "override",
    [
        {"x1": -0.1},
        {"y2": 1.1},
        {"direction": "a_to_b"},
        {"direction": "SIDEWAYS"},
        {"enabled": 7},
        {"x1": 0.5, "y1": 0.5, "x2": 0.52, "y2": 0.5},  # shorter than 0.05
    ],
)
def test_line_check_constraints(database: Database, override):
    with database.write() as connection:
        camera_id = _camera(connection)
    with pytest.raises(sqlite3.IntegrityError):
        with database.write() as connection:
            _line(connection, camera_id, **override)


def test_a_line_of_exactly_the_minimum_length_is_accepted(database: Database):
    with database.write() as connection:
        camera_id = _camera(connection)
        _line(connection, camera_id, x1=0.5, y1=0.5, x2=0.55, y2=0.5)


@pytest.mark.parametrize(
    "override",
    [
        {"object_category": "bicycle"},
        {"object_class": "train"},
        {"direction": "BOTH"},
        {"confidence": 1.5},
        {"track_id": -1},
        {"bbox_x1": 0.9, "bbox_x2": 0.1},
        {"bbox_y1": 0.9, "bbox_y2": 0.1},
    ],
)
def test_event_check_constraints(database: Database, override):
    with database.write() as connection:
        camera_id = _camera(connection)
        line_id = _line(connection, camera_id)
    with pytest.raises(sqlite3.IntegrityError):
        with database.write() as connection:
            _event(connection, camera_id, line_id, **override)


def test_the_event_dedupe_tuple_is_unique(database: Database):
    with database.write() as connection:
        camera_id = _camera(connection)
        line_id = _line(connection, camera_id)
        _event(connection, camera_id, line_id, id="evt_" + "a" * 32)
    with pytest.raises(sqlite3.IntegrityError):
        with database.write() as connection:
            _event(connection, camera_id, line_id, id="evt_" + "b" * 32)


def test_the_same_track_may_fire_both_directions_once_each(database: Database):
    with database.write() as connection:
        camera_id = _camera(connection)
        line_id = _line(connection, camera_id)
        _event(connection, camera_id, line_id, id="evt_" + "a" * 32, direction="A_TO_B")
        _event(connection, camera_id, line_id, id="evt_" + "b" * 32, direction="B_TO_A")
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_the_same_track_in_a_new_session_is_a_distinct_event(database: Database):
    with database.write() as connection:
        camera_id = _camera(connection)
        line_id = _line(connection, camera_id)
        _event(connection, camera_id, line_id, id="evt_" + "a" * 32)
        _event(
            connection,
            camera_id,
            line_id,
            id="evt_" + "b" * 32,
            worker_session_id="ws_" + "2" * 32,
        )
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


# -- transactions -----------------------------------------------------------


def test_a_failed_write_rolls_back(database: Database):
    with pytest.raises(sqlite3.IntegrityError):
        with database.write() as connection:
            _camera(connection, "acam_00000001")
            _camera(connection, "acam_00000002")  # duplicate vms id
    with database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM analytics_cameras"
        ).fetchone()[0] == 0


def test_check_readable_probe(database: Database):
    database.check_readable()


def test_timestamps_sort_lexically_and_chronologically(database: Database):
    from app.domain.models import to_iso_ms

    base = datetime(2026, 9, 7, 9, 15, 14, 123000, tzinfo=UTC)
    stamps = [to_iso_ms(base + timedelta(milliseconds=n)) for n in (0, 1, 999, 1000)]
    assert stamps == sorted(stamps)
