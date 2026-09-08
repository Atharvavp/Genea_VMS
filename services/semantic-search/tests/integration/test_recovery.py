"""Fault injection: interrupted writes, corrupt vectors, corrupt DB, reindex."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.domain.models import RepresentationKind, utc_now
from app.embeddings.manifest import EMBEDDING_BYTES
from app.persistence.database import (
    Database,
    DatabaseCorrupt,
    ModelIdentityMismatch,
    ProcessLock,
    ProcessLockError,
    quarantine_database,
)
from app.retrieval.snapshot import build_snapshot
from app.services.reconciliation import ReconciliationService
from tests.fakes.component4_server import FakeComponent4
from tests.fakes.factories import make_event, make_events
from tests.integration.test_discovery_indexing import Harness


def _reconciler(harness: Harness) -> ReconciliationService:
    return ReconciliationService(
        client=harness.client,
        events=harness.events,
        representations=harness.representations,
        sync=harness.sync,
        store=harness.store,
        rebuilder=harness.rebuilder,
        settings=harness.settings,
    )


@pytest.fixture()
async def harness(tmp_path):
    fake = FakeComponent4()
    instance = Harness(tmp_path, fake)
    try:
        yield instance
    finally:
        await instance.aclose()


async def test_a_crash_between_commit_and_snapshot_swap_is_repaired(harness):
    harness.fake.add(make_events(4))
    await harness.discover_all()
    await harness.index_all()
    assert harness.store.current().event_count == 4

    # A vector commits, then the process dies before the snapshot is published.
    late = make_event("late", crossed_at=utc_now())
    harness.fake.add([late])
    await harness.discovery.poll_overlap()
    harness.representations.mark_indexed(
        late.event_id, RepresentationKind.CROP, harness.runtime.embed_texts(["red"])[0]
    )
    stale = harness.store.current()
    assert stale.event_count == 4
    assert stale.index_revision != harness.representations.index_revision()

    report = await _reconciler(harness).reconcile()
    assert report.snapshot_rebuilt is True
    assert harness.store.current().event_count == 5
    assert harness.store.current().index_revision == harness.representations.index_revision()


async def test_reconciliation_rediscovers_an_event_outside_the_overlap_window(harness):
    from datetime import timedelta

    harness.fake.add(make_events(3, step_seconds=60))
    await harness.discover_all()
    high_water = harness.sync.read().high_water_crossed_at

    # Far behind the high water: the overlap poll cannot see it.
    ancient = make_event("ancient", crossed_at=high_water - timedelta(days=3))
    harness.fake.add([ancient])
    await harness.discovery.poll_overlap()
    assert harness.events.get(ancient.event_id) is None

    report = await _reconciler(harness).reconcile()
    assert report.completed and report.discovered == 1
    assert harness.events.get(ancient.event_id) is not None


async def test_reconciliation_refreshes_metadata_and_never_deletes(harness):
    events = make_events(3)
    harness.fake.add(events)
    await harness.discover_all()
    await harness.index_all()

    # Upstream renames a camera and stops returning one event entirely.
    renamed = make_event(
        0, camera_name="Renamed Bay", crossed_at=events[0].crossed_at, camera=0
    )
    harness.fake.events = [renamed, events[1]]

    report = await _reconciler(harness).reconcile()
    assert report.completed
    assert harness.events.get(renamed.event_id).camera_name == "Renamed Bay"
    # The event the traversal did not return is still local and still searchable.
    assert harness.events.get(events[2].event_id) is not None
    assert harness.events.count() == 3
    assert events[2].event_id in harness.store.current().event_ids


async def test_a_corrupt_vector_is_excluded_rescheduled_and_reindexed(harness):
    events = make_events(3)
    harness.fake.add(events)
    await harness.discover_all()
    await harness.index_all()
    assert harness.store.current().event_count == 3

    with harness.database.transaction() as conn:
        conn.execute(
            "UPDATE representations SET embedding = ? WHERE event_id = ? AND kind = 'crop'",
            (b"\x7f" * EMBEDDING_BYTES, events[0].event_id),
        )

    report = await _reconciler(harness).reconcile()
    assert report.invalid_vectors == 1
    assert report.snapshot_rebuilt is True
    # Excluded from search, not served, and queued to be embedded again.
    assert events[0].event_id not in harness.store.current().event_ids
    assert harness.events.representation_states(events[0].event_id)["crop"] == "pending"

    await harness.index_all()
    assert events[0].event_id in harness.store.current().event_ids


async def test_a_reconciliation_pause_keeps_its_cursor(harness):
    harness.fake.add(make_events(150))
    await harness.discover_all(limit=100)
    harness.fake.outage = "500"
    report = await _reconciler(harness).reconcile()
    assert report.completed is False
    assert report.error_code == "component4_rejected_request"

    harness.fake.outage = None
    resumed = await _reconciler(harness).reconcile()
    assert resumed.completed is True


async def test_a_corrupt_database_is_quarantined_and_rebuilt(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "semantic.db"
    db_path.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)

    database = Database(db_path)
    with pytest.raises((DatabaseCorrupt, sqlite3.DatabaseError)):
        database.quick_check()
    database.close()

    destination = quarantine_database(db_path, data_dir / "quarantine")
    assert (destination / "semantic.db").exists()
    assert not db_path.exists()

    fresh = Database(db_path)
    fresh.ensure_parent()
    state = fresh.initialize()
    assert state.index_revision == 0
    fresh.quick_check()
    fresh.close()
    # The quarantined copy is still there for an operator to inspect.
    assert (destination / "semantic.db").stat().st_size > 0


async def test_a_store_from_another_model_refuses_to_open(tmp_path):
    db_path = tmp_path / "semantic.db"
    database = Database(db_path)
    database.ensure_parent()
    database.initialize()
    with database.transaction() as conn:
        conn.execute("UPDATE system_state SET model_id = 'clip-vit-b32@old'")
    database.close()

    with pytest.raises(ModelIdentityMismatch):
        Database(db_path).initialize()


def test_reindex_refuses_while_the_service_owns_the_volume(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "semantic.db"
    Database(db_path).initialize()

    lock = ProcessLock(data_dir / "semantic.lock")
    lock.acquire()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.reindex", "--confirm-reset-derived-index"],
            cwd=str(Path(__file__).resolve().parents[2]),
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                "SEMANTIC_DATA_DIR": str(data_dir),
                "SEMANTIC_DB_PATH": str(db_path),
                "SEMANTIC_MODEL_DIR": "/opt/models/siglip",
                "HF_HUB_OFFLINE": "1",
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 2, result.stdout + result.stderr
        assert "REFUSED" in result.stderr
        # The database was left exactly as it was.
        assert db_path.exists()
        assert not (data_dir / "quarantine").exists()
    finally:
        lock.release()


def test_reindex_quarantines_and_leaves_an_empty_store(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "semantic.db"
    database = Database(db_path)
    database.ensure_parent()
    database.initialize()
    database.close()

    root = Path(__file__).resolve().parents[2]
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(root),
        "SEMANTIC_DATA_DIR": str(data_dir),
        "SEMANTIC_DB_PATH": str(db_path),
        "SEMANTIC_MODEL_DIR": "/opt/models/siglip",
        "HF_HUB_OFFLINE": "1",
    }
    dry = subprocess.run(
        [sys.executable, "-m", "scripts.reindex"],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=120,
    )
    assert dry.returncode == 0
    assert "Nothing was changed" in dry.stdout
    assert not (data_dir / "quarantine").exists()

    confirmed = subprocess.run(
        [sys.executable, "-m", "scripts.reindex", "--confirm-reset-derived-index"],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=120,
    )
    assert confirmed.returncode == 0, confirmed.stderr
    quarantine = list((data_dir / "quarantine").iterdir())
    assert len(quarantine) == 1
    assert (quarantine[0] / "semantic.db").exists()

    reopened = Database(db_path)
    assert reopened.initialize().index_revision == 0
    assert reopened.connect().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    reopened.close()


def test_an_empty_snapshot_is_valid_and_searchable():
    snapshot = build_snapshot(events=[], vectors=[], index_revision=0, built_at=utc_now())
    assert snapshot.is_empty and snapshot.event_count == 0
