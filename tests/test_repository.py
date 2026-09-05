"""Schema, connection settings and CRUD of the camera repository."""

from __future__ import annotations

import sqlite3

import pytest

from app.domain.models import CameraRecord, utcnow_iso
from app.persistence.camera_repository import CameraRepository, DuplicatePath
from app.persistence.database import connect


def make_record(camera_id="cam_00000001", name="Lobby", enabled=True, url=None):
    now = utcnow_iso()
    return CameraRecord(
        id=camera_id,
        name=name,
        rtsp_url=url or f"rtsp://cam/{camera_id}",
        mediamtx_path=f"vms_{camera_id}",
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def repo(tmp_path):
    repository = CameraRepository(tmp_path / "vms.db")
    repository.initialize()
    return repository


def test_initialize_creates_schema_and_is_idempotent(repo):
    repo.initialize()
    with connect(repo.db_path) as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(cameras)").fetchall()
        }
    assert columns == {
        "id",
        "name",
        "rtsp_url",
        "mediamtx_path",
        "enabled",
        "created_at",
        "updated_at",
    }


def test_health_is_not_persisted(repo):
    """Health is runtime state derived from MediaMTX, so it has no column."""
    with connect(repo.db_path) as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(cameras)").fetchall()
        }
    assert not columns & {"health_state", "last_error", "last_checked_at"}


def test_connection_uses_wal(repo):
    with connect(repo.db_path) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_insert_get_and_list(repo):
    first = repo.insert(make_record("cam_00000001", "Lobby"))
    second = repo.insert(make_record("cam_00000002", "Dock"))
    assert repo.get(first.id) == first
    assert [record.id for record in repo.list()] == [first.id, second.id]


def test_get_missing_returns_none(repo):
    assert repo.get("cam_deadbeef") is None


def test_duplicate_mediamtx_path_rejected(repo):
    repo.insert(make_record("cam_00000001"))
    clash = CameraRecord(
        id="cam_00000009",
        name="Clash",
        rtsp_url="rtsp://cam/x",
        mediamtx_path="vms_cam_00000001",
        enabled=True,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    with pytest.raises(DuplicatePath):
        repo.insert(clash)


def test_duplicate_id_rejected(repo):
    repo.insert(make_record("cam_00000001"))
    with pytest.raises((DuplicatePath, sqlite3.IntegrityError)):
        repo.insert(make_record("cam_00000001", name="Other"))


def test_update_changes_name_url_and_enabled(repo):
    record = repo.insert(make_record("cam_00000001"))
    updated = CameraRecord(
        id=record.id,
        name="Renamed",
        rtsp_url="rtsp://other:554/live",
        mediamtx_path=record.mediamtx_path,
        enabled=False,
        created_at=record.created_at,
        updated_at=utcnow_iso(),
    )
    repo.update(updated)
    stored = repo.get(record.id)
    assert stored.name == "Renamed"
    assert stored.rtsp_url == "rtsp://other:554/live"
    assert stored.enabled is False
    assert stored.mediamtx_path == record.mediamtx_path
    assert stored.created_at == record.created_at


def test_update_missing_raises(repo):
    with pytest.raises(KeyError):
        repo.update(make_record("cam_deadbeef"))


def test_delete(repo):
    record = repo.insert(make_record("cam_00000001"))
    repo.insert(make_record("cam_00000002"))
    assert repo.delete(record.id) is True
    assert repo.delete(record.id) is False
    assert [item.id for item in repo.list()] == ["cam_00000002"]


def test_count(repo):
    assert repo.count() == (0, 0)
    repo.insert(make_record("cam_00000001", enabled=True))
    repo.insert(make_record("cam_00000002", enabled=False))
    assert repo.count() == (2, 1)


def test_credentialed_url_round_trips_verbatim(repo):
    url = "rtsp://admin:hunter2@10.0.0.9:554/Streaming"
    repo.insert(make_record("cam_00000001", url=url))
    # MediaMTX needs the real URL; masking happens at the API boundary only.
    assert repo.get("cam_00000001").rtsp_url == url


def test_list_keeps_insertion_order_for_same_timestamp_creations(repo):
    """Two cameras created in the same millisecond still list in order."""
    stamp = utcnow_iso()
    for index in range(5):
        repo.insert(
            CameraRecord(
                id=f"cam_0000000{index}",
                name=f"Camera {index}",
                rtsp_url=f"rtsp://cam/{index}",
                mediamtx_path=f"vms_cam_0000000{index}",
                enabled=True,
                created_at=stamp,
                updated_at=stamp,
            )
        )
    assert [record.id for record in repo.list()] == [
        f"cam_0000000{index}" for index in range(5)
    ]
