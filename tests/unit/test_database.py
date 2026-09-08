"""Schema creation, integrity, ownership locking, and identity refusal."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.persistence.database import (
    Database,
    DatabaseCorrupt,
    ModelIdentityMismatch,
    ProcessLock,
    ProcessLockError,
    SCHEMA_VERSION,
    quarantine_database,
)


def test_a_fresh_store_is_stamped_with_the_frozen_manifest(settings):
    from app.embeddings.manifest import MANIFEST

    db = Database(settings.semantic_db_path)
    db.ensure_parent()
    state = db.initialize()
    assert state.schema_version == SCHEMA_VERSION
    assert state.model_id == MANIFEST.model_id
    assert state.embedding_dim == MANIFEST.dimension
    assert state.index_revision == 0
    db.close()


def test_pragmas_are_applied_to_every_connection(database):
    conn = database.connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_initialize_is_idempotent(settings):
    for _ in range(3):
        db = Database(settings.semantic_db_path)
        db.ensure_parent()
        db.initialize()
        rows = db.connect().execute("SELECT COUNT(*) FROM system_state").fetchone()[0]
        assert rows == 1
        assert db.connect().execute("SELECT COUNT(*) FROM sync_state").fetchone()[0] == 1
        db.close()


def test_a_store_from_another_model_is_refused(settings):
    db = Database(settings.semantic_db_path)
    db.ensure_parent()
    db.initialize()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE system_state SET model_id = 'some-other-model@deadbeef'"
            " WHERE singleton = 1"
        )
    db.close()

    reopened = Database(settings.semantic_db_path)
    with pytest.raises(ModelIdentityMismatch) as excinfo:
        reopened.initialize()
    assert excinfo.value.stored["model_id"] == "some-other-model@deadbeef"
    reopened.close()


def test_a_store_with_a_different_dimension_is_refused(settings):
    db = Database(settings.semantic_db_path)
    db.ensure_parent()
    db.initialize()
    with db.transaction() as conn:
        conn.execute("UPDATE system_state SET embedding_dim = 512 WHERE singleton = 1")
    db.close()
    with pytest.raises(ModelIdentityMismatch):
        Database(settings.semantic_db_path).initialize()


def test_quick_check_detects_a_corrupt_file(settings, tmp_path: Path):
    corrupt = settings.semantic_data_dir / "broken.db"
    corrupt.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    db = Database(corrupt)
    with pytest.raises((DatabaseCorrupt, sqlite3.DatabaseError)):
        db.quick_check()


def test_a_rolled_back_transaction_leaves_no_row(database):
    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO events (event_id, camera_id, vms_camera_id, camera_name,"
                " crossed_at, object_category, object_class, direction, discovered_at,"
                " source_last_seen_at) VALUES ('evt_" + "a" * 32 + "', 'acam_0123abcd',"
                " 'cam_0123abcd', 'Bay', '2026-09-07T00:00:00.000Z', 'vehicle', 'car',"
                " 'A_TO_B', '2026-09-07T00:00:00.000Z', '2026-09-07T00:00:00.000Z')"
            )
            raise Boom()
    assert database.connect().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_check_constraints_reject_foreign_enumeration_values(database):
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO events (event_id, camera_id, vms_camera_id, camera_name,"
                " crossed_at, object_category, object_class, direction, discovered_at,"
                " source_last_seen_at) VALUES ('evt_" + "b" * 32 + "', 'acam_0123abcd',"
                " 'cam_0123abcd', 'Bay', '2026-09-07T00:00:00.000Z', 'animal', 'car',"
                " 'A_TO_B', '2026-09-07T00:00:00.000Z', '2026-09-07T00:00:00.000Z')"
            )


def test_representations_require_a_known_event(database):
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO representations (event_id, kind, model_id, state,"
                " updated_at) VALUES ('evt_" + "c" * 32 + "', 'crop', 'm', 'pending',"
                " '2026-09-07T00:00:00.000Z')"
            )


def test_one_process_owns_the_data_directory(settings):
    first = ProcessLock(settings.lock_path)
    first.acquire()
    assert first.held
    second = ProcessLock(settings.lock_path)
    with pytest.raises(ProcessLockError):
        second.acquire()
    first.release()
    # Released ownership can be taken by the next process.
    second.acquire()
    assert second.held
    second.release()


def test_quarantine_moves_every_sidecar_and_keeps_it(settings):
    db_path = settings.semantic_db_path
    db = Database(db_path)
    db.ensure_parent()
    db.initialize()
    db.connect().execute("PRAGMA wal_checkpoint(FULL)")
    db.close()
    Path(str(db_path) + "-wal").touch()
    Path(str(db_path) + "-shm").touch()

    destination = quarantine_database(db_path, settings.quarantine_dir)

    assert not db_path.exists()
    assert (destination / db_path.name).exists()
    assert (destination / (db_path.name + "-wal")).exists()
    assert (destination / (db_path.name + "-shm")).exists()
