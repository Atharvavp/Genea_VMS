"""Crop rules, atomic finalisation, failure injection, and reconciliation."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from app.analytics.types import CommitOutcome
from app.persistence.database import Database
from app.persistence.event_repository import EventRepository
from app.persistence.event_storage import (
    EventStorage,
    EventStorageError,
    clip_crop_box,
)
from app.security.event_paths import (
    UnsafeEventPath,
    event_directory_relative,
    event_image_relative,
    resolve_within,
    temp_directory_name,
)
from tests.fakes import factories

pytestmark = pytest.mark.unit

EVENT_ID = "evt_" + "0" * 32
NONCE = "a" * 16


@pytest.fixture
def store(tmp_path: Path):
    data_root = tmp_path / "data"
    events_root = data_root / "events"
    events_root.mkdir(parents=True)
    database = Database(data_root / "analytics.db")
    database.initialize()
    repository = EventRepository(database)
    ids = iter([f"evt_{index:032x}" for index in range(1, 500)])
    storage = EventStorage(
        data_root=data_root,
        events_root=events_root,
        repository=repository,
        id_factory=lambda: next(ids),
        nonce_factory=lambda: NONCE,
    )
    return storage, repository, data_root, events_root


# -- crop rules -------------------------------------------------------------


@pytest.mark.parametrize(
    "box,expected",
    [
        ((0.0, 0.0, 10.0, 10.0), (0, 0, 10, 10)),
        ((0.4, 0.6, 9.2, 9.9), (0, 0, 10, 10)),
        ((-5.0, -5.0, 4.0, 4.0), (0, 0, 4, 4)),
        ((56.0, 40.0, 200.0, 200.0), (56, 40, 64, 48)),
        ((63.0, 47.0, 64.0, 48.0), None),
    ],
)
def test_crop_box_floor_ceil_and_clamp(box, expected):
    assert clip_crop_box(*box, 64, 48) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_non_finite_boxes_are_rejected(value):
    assert clip_crop_box(value, 0.0, 10.0, 10.0, 64, 48) is None


def test_a_sub_two_pixel_crop_is_not_an_event(store):
    storage, repository, _root, _events = store
    result = storage.commit_event(factories.candidate(bbox=(10.0, 10.0, 11.0, 11.0)))
    assert result.outcome is CommitOutcome.INVALID_CROP
    assert repository.count() == 0


# -- paths ------------------------------------------------------------------


def test_relative_layout_is_exact():
    relative = event_image_relative(
        "events", "acam_ab12cd34", "2026-09-07", EVENT_ID, "frame"
    )
    assert relative == f"events/acam_ab12cd34/2026-09-07/{EVENT_ID}/frame.jpg"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"camera_id": "cam_ab12cd34"},
        {"camera_id": "../etc"},
        {"event_id": "evt_zz"},
        {"day": "2026-13-40"},
        {"day": "2026-9-7"},
        {"events_root_name": "../events"},
    ],
)
def test_path_builders_refuse_anything_not_built_from_validated_ids(kwargs):
    args = {
        "events_root_name": "events",
        "camera_id": "acam_ab12cd34",
        "day": "2026-09-07",
        "event_id": EVENT_ID,
    }
    args.update(kwargs)
    with pytest.raises(UnsafeEventPath):
        event_directory_relative(**args)


def test_temp_directory_name_shape():
    assert temp_directory_name(EVENT_ID, NONCE) == f".tmp-{EVENT_ID}-{NONCE}"
    with pytest.raises(UnsafeEventPath):
        temp_directory_name(EVENT_ID, "zz")


@pytest.mark.parametrize(
    "relative",
    ["/etc/passwd", "../outside", "events/../../etc", "", "events/./x"],
)
def test_resolution_refuses_escapes(tmp_path: Path, relative: str):
    with pytest.raises(UnsafeEventPath):
        resolve_within(tmp_path, relative)


def test_resolution_refuses_a_symlinked_component(tmp_path: Path):
    (tmp_path / "real").mkdir()
    (tmp_path / "events").symlink_to(tmp_path / "real")
    with pytest.raises(UnsafeEventPath):
        resolve_within(tmp_path, "events/frame.jpg")


def test_resolution_accepts_a_plain_descendant(tmp_path: Path):
    target = resolve_within(tmp_path, "events/acam_ab12cd34/frame.jpg")
    assert target == tmp_path.resolve() / "events" / "acam_ab12cd34" / "frame.jpg"


# -- the happy path ---------------------------------------------------------


def test_commit_writes_both_images_then_the_row(store):
    storage, repository, data_root, _events = store
    result = storage.commit_event(factories.candidate())
    assert result.outcome is CommitOutcome.INSERTED
    record = repository.get(result.event_id)
    assert record is not None

    frame = data_root / record.snapshot_path
    crop = data_root / record.crop_path
    assert frame.is_file() and crop.is_file()
    assert oct(frame.stat().st_mode)[-3:] == "640"

    from PIL import Image

    with Image.open(io.BytesIO(frame.read_bytes())) as image:
        assert image.size == (64, 48)
        assert image.mode == "RGB"
    with Image.open(io.BytesIO(crop.read_bytes())) as image:
        assert image.size == (40 - 4, 30 - 6)


def test_normalised_bbox_is_derived_from_the_clipped_crop(store):
    storage, repository, _root, _events = store
    result = storage.commit_event(factories.candidate(bbox=(4.0, 6.0, 40.0, 30.0)))
    record = repository.get(result.event_id)
    assert record.bbox_x1 == pytest.approx(4 / 64)
    assert record.bbox_y1 == pytest.approx(6 / 48)
    assert record.bbox_x2 == pytest.approx(40 / 64)
    assert record.bbox_y2 == pytest.approx(30 / 48)


def test_no_temp_directory_survives_a_success(store):
    storage, _repository, _root, events_root = store
    storage.commit_event(factories.candidate())
    assert not list(events_root.rglob(".tmp-*"))


def test_a_repeated_crossing_returns_already_exists_without_writing(store):
    storage, repository, _root, events_root = store
    first = storage.commit_event(factories.candidate())
    second = storage.commit_event(factories.candidate())
    assert second.outcome is CommitOutcome.ALREADY_EXISTS
    assert second.event_id == first.event_id
    assert repository.count() == 1
    assert len(list(events_root.rglob("frame.jpg"))) == 1


def test_write_order_is_files_then_rename_then_insert(store, monkeypatch):
    storage, repository, _root, _events = store
    order: list[str] = []
    real_rename = os.rename
    real_insert = repository.insert

    def spy_rename(src, dst):
        order.append("rename")
        return real_rename(src, dst)

    def spy_insert(record):
        order.append("insert")
        return real_insert(record)

    monkeypatch.setattr(os, "rename", spy_rename)
    monkeypatch.setattr(repository, "insert", spy_insert)
    storage.commit_event(factories.candidate())
    assert order == ["rename", "insert"]


# -- failure injection ------------------------------------------------------


def test_a_write_failure_leaves_no_row_and_no_directory(store, monkeypatch):
    """A simulated ENOSPC on the image write, with cleanup still working."""
    storage, repository, _root, events_root = store
    real_open = os.open

    def boom(path, *args, **kwargs):
        if str(path).endswith(".jpg"):
            raise OSError(28, "No space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", boom)
    with pytest.raises(EventStorageError):
        storage.commit_event(factories.candidate())
    assert repository.count() == 0
    assert not list(events_root.rglob("*.jpg"))
    assert not list(events_root.rglob(".tmp-*"))


def test_an_fsync_failure_is_a_storage_failure(store, monkeypatch):
    storage, repository, _root, _events = store
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("io")))
    with pytest.raises(EventStorageError):
        storage.commit_event(factories.candidate())
    assert repository.count() == 0


def test_a_rename_failure_cleans_the_temporary_directory(store, monkeypatch):
    storage, repository, _root, events_root = store
    monkeypatch.setattr(
        os, "rename", lambda *_a: (_ for _ in ()).throw(OSError("rename"))
    )
    with pytest.raises(EventStorageError):
        storage.commit_event(factories.candidate())
    assert repository.count() == 0
    assert not list(events_root.rglob(".tmp-*"))


def test_a_database_failure_after_rename_removes_the_final_directory(store, monkeypatch):
    storage, repository, _root, events_root = store
    monkeypatch.setattr(
        repository, "insert", lambda _r: (_ for _ in ()).throw(RuntimeError("db"))
    )
    with pytest.raises(EventStorageError):
        storage.commit_event(factories.candidate())
    assert not list(events_root.rglob("frame.jpg"))


def test_a_unique_conflict_at_insert_cleans_the_loser_and_returns_the_winner(
    store, monkeypatch
):
    storage, repository, _root, events_root = store
    winner = factories.event(event_id="evt_" + "f" * 32)
    repository.insert(winner)
    monkeypatch.setattr(repository, "find_by_dedupe_key", _sequence(None, winner))
    result = storage.commit_event(factories.candidate())
    assert result.outcome is CommitOutcome.ALREADY_EXISTS
    assert result.event_id == winner.id
    assert len(list(events_root.rglob("frame.jpg"))) == 0


def _sequence(*values):
    calls = {"n": 0}

    def _call(*_args, **_kwargs):
        index = min(calls["n"], len(values) - 1)
        calls["n"] += 1
        return values[index]

    return _call


def test_an_empty_jpeg_encode_is_a_storage_failure(store, monkeypatch):
    storage, _repository, _root, _events = store
    monkeypatch.setattr(storage, "_encode_jpeg", lambda _rgb: b"")
    with pytest.raises(EventStorageError):
        storage.commit_event(factories.candidate())


# -- reconciliation ---------------------------------------------------------


def test_reconciliation_removes_temp_and_orphan_directories_only(store):
    storage, repository, _root, events_root = store
    committed = storage.commit_event(factories.candidate())

    day = events_root / "acam_0123abcd" / "2026-09-07"
    temp = day / f".tmp-{EVENT_ID}-{NONCE}"
    temp.mkdir(parents=True)
    (temp / "frame.jpg").write_bytes(b"partial")
    orphan = day / ("evt_" + "9" * 32)
    orphan.mkdir()
    (orphan / "frame.jpg").write_bytes(b"orphan")
    unknown = day / "something-else"
    unknown.mkdir()

    report = storage.reconcile_startup()
    assert report.temp_directories_removed == 1
    assert report.orphan_directories_removed == 1
    assert report.unknown_paths == 1
    assert not temp.exists()
    assert not orphan.exists()
    assert unknown.exists()
    assert repository.get(committed.event_id) is not None
    assert (events_root / repository.get(committed.event_id).snapshot_path.split("/", 1)[1]).exists()


def test_reconciliation_never_deletes_a_row_whose_image_is_missing(store):
    storage, repository, data_root, _events = store
    result = storage.commit_event(factories.candidate())
    record = repository.get(result.event_id)
    (data_root / record.snapshot_path).unlink()

    report = storage.reconcile_startup()
    assert report.rows_with_missing_artifacts == 1
    assert repository.get(result.event_id) is not None
    assert storage.resolve_image(record.snapshot_path) is None
    assert storage.resolve_image(record.crop_path) is not None


def test_reconciliation_of_an_empty_store_is_clean(store):
    storage, _repository, _root, _events = store
    assert storage.reconcile_startup().clean is True


def test_resolve_image_refuses_an_unsafe_stored_path(store):
    storage, _repository, _root, _events = store
    assert storage.resolve_image("../../etc/passwd") is None
    assert storage.resolve_image("/etc/passwd") is None


def test_resolve_image_refuses_an_empty_file(store, tmp_path: Path):
    storage, _repository, data_root, events_root = store
    target = events_root / "acam_0123abcd" / "2026-09-07" / EVENT_ID
    target.mkdir(parents=True)
    (target / "frame.jpg").write_bytes(b"")
    relative = f"events/acam_0123abcd/2026-09-07/{EVENT_ID}/frame.jpg"
    assert storage.resolve_image(relative) is None


# -- the read-only verifier -------------------------------------------------


def test_verifier_reports_clean_for_a_healthy_store(store, capsys):
    from scripts.verify_event_store import main

    storage, _repository, data_root, events_root = store
    storage.commit_event(factories.candidate())
    exit_code = main(
        [
            "--data-dir",
            str(data_root),
            "--db-path",
            str(data_root / "analytics.db"),
            "--event-dir",
            str(events_root),
        ]
    )
    assert exit_code == 0
    assert "event store is consistent" in capsys.readouterr().out


@pytest.mark.parametrize("corruption", ["missing_artifact", "orphan_dir", "temp_dir"])
def test_verifier_fails_a_corrupt_store(store, capsys, corruption):
    from scripts.verify_event_store import main

    storage, repository, data_root, events_root = store
    result = storage.commit_event(factories.candidate())
    record = repository.get(result.event_id)
    day = events_root / "acam_0123abcd" / "2026-09-07"

    if corruption == "missing_artifact":
        (data_root / record.snapshot_path).unlink()
    elif corruption == "orphan_dir":
        (day / ("evt_" + "9" * 32)).mkdir()
    else:
        (day / f".tmp-{EVENT_ID}-{NONCE}").mkdir()

    exit_code = main(
        [
            "--data-dir",
            str(data_root),
            "--db-path",
            str(data_root / "analytics.db"),
            "--event-dir",
            str(events_root),
        ]
    )
    assert exit_code == 1
    assert "PROBLEM" in capsys.readouterr().out
