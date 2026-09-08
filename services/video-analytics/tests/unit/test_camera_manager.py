"""Desired-state orchestration, instance fencing, and lifecycle ordering."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.models import (
    CameraCreateRequest,
    CameraPatchRequest,
    LineDirection,
    LinePutRequest,
    ObjectCategory,
    Point,
    RuntimeState,
)
from app.persistence.camera_repository import CameraRepository
from app.persistence.database import Database
from app.persistence.event_repository import EventRepository
from app.persistence.event_storage import EventStorage
from app.persistence.line_repository import LineRepository
from app.services.camera_manager import (
    AnalyticsCameraManager,
    CameraNotFound,
    DuplicateVmsCameraId,
    SnapshotNotReady,
    WorkerStopTimeout,
)
from tests.fakes import worker as worker_fakes
from tests.fakes.detector import FakeDetector

pytestmark = pytest.mark.unit

RTSP = "rtsp://host.docker.internal:8555/vms_cam_0123abcd"


@pytest.fixture(autouse=True)
def _reset_workers():
    worker_fakes.reset()
    yield
    worker_fakes.reset()


@pytest.fixture
def context(tmp_path: Path):
    data_root = tmp_path / "data"
    events_root = data_root / "events"
    events_root.mkdir(parents=True)
    database = Database(data_root / "analytics.db")
    database.initialize()
    cameras = CameraRepository(database)
    lines = LineRepository(database)
    storage = EventStorage(
        data_root=data_root,
        events_root=events_root,
        repository=EventRepository(database),
    )
    detector = FakeDetector()
    ids = iter(f"acam_{index:08x}" for index in range(1, 200))
    line_ids = iter(f"line_{index:08x}" for index in range(1, 200))
    instances = iter(f"wi_{index:032x}" for index in range(1, 200))
    sessions = iter(f"ws_{index:032x}" for index in range(1, 200))
    manager = AnalyticsCameraManager(
        cameras=cameras,
        lines=lines,
        event_storage=storage,
        detector=detector,
        worker_factory=worker_fakes.factory(),
        id_factory=lambda: next(ids),
        line_id_factory=lambda: next(line_ids),
        instance_id_factory=lambda: next(instances),
        session_id_factory=lambda: next(sessions),
        stop_timeout_seconds=0.01,
    )
    return manager, cameras, lines, detector


def create_request(**over) -> CameraCreateRequest:
    payload = {
        "vms_camera_id": "cam_0123abcd",
        "name": "Loading Bay",
        "rtsp_url": RTSP,
        "enabled": True,
    }
    payload.update(over)
    return CameraCreateRequest(**payload)


def line_request(**over) -> LinePutRequest:
    payload = {
        "name": "Entry line",
        "a": Point(x=0.2, y=0.5),
        "b": Point(x=0.8, y=0.5),
        "direction": LineDirection.A_TO_B,
        "enabled": True,
    }
    payload.update(over)
    return LinePutRequest(**payload)


async def ready(manager: AnalyticsCameraManager) -> None:
    await manager.start_all_enabled(detector_ready=True)


# -- startup ----------------------------------------------------------------


async def test_startup_starts_workers_only_for_enabled_rows(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    await manager.create_camera(create_request())
    await manager.create_camera(
        create_request(vms_camera_id="cam_00000002", name="Off", enabled=False)
    )
    assert len(worker_fakes.FakeWorker.created) == 1
    assert manager.runtime_snapshot("acam_00000002").state is RuntimeState.DISABLED


async def test_startup_without_a_detector_starts_nothing_and_marks_error(context):
    manager, cameras, _lines, _detector = context
    await ready(manager)
    await manager.create_camera(create_request())
    worker_fakes.reset()

    fresh = AnalyticsCameraManager(
        cameras=cameras,
        lines=_lines,
        event_storage=manager._storage,  # noqa: SLF001
        detector=FakeDetector(ready=False),
        worker_factory=worker_fakes.factory(),
    )
    report = await fresh.start_all_enabled(detector_ready=False)
    assert report.degraded is True
    assert report.started == 0
    assert worker_fakes.FakeWorker.created == []
    runtime = fresh.runtime_snapshot("acam_00000001")
    assert runtime.state is RuntimeState.ERROR
    assert runtime.last_error_code == "model_unavailable"


async def test_restart_reconstructs_desired_state_with_fresh_sessions(context):
    manager, cameras, lines, _detector = context
    await ready(manager)
    await manager.create_camera(create_request())
    first_session = worker_fakes.FakeWorker.created[0].session_id
    worker_fakes.reset()

    restarted = AnalyticsCameraManager(
        cameras=cameras,
        lines=lines,
        event_storage=manager._storage,  # noqa: SLF001
        detector=FakeDetector(),
        worker_factory=worker_fakes.factory(),
    )
    await restarted.start_all_enabled(detector_ready=True)
    assert len(worker_fakes.FakeWorker.created) == 1
    assert worker_fakes.FakeWorker.created[0].session_id != first_session


# -- create -----------------------------------------------------------------


async def test_create_persists_before_starting_a_worker(context):
    manager, cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    assert cameras.get(view.camera.id) is not None
    assert worker_fakes.FakeWorker.created[0].started is True


async def test_create_disabled_starts_nothing(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request(enabled=False))
    assert worker_fakes.FakeWorker.created == []
    assert view.runtime.state is RuntimeState.DISABLED


async def test_a_duplicate_vms_camera_id_is_refused(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    await manager.create_camera(create_request())
    with pytest.raises(DuplicateVmsCameraId):
        await manager.create_camera(create_request(name="Second"))


async def test_a_worker_that_cannot_start_keeps_the_enabled_row(context):
    manager, cameras, _lines, _detector = context
    manager._worker_factory = worker_fakes.factory(start_raises=True)  # noqa: SLF001
    await ready(manager)
    view = await manager.create_camera(create_request())
    assert cameras.get(view.camera.id).enabled is True
    runtime = manager.runtime_snapshot(view.camera.id)
    assert runtime.state is RuntimeState.ERROR
    assert runtime.last_error_code == "worker_start_failed"


# -- enable and disable -----------------------------------------------------


async def test_disable_persists_before_signalling_the_worker(context):
    manager, cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    worker = worker_fakes.FakeWorker.created[0]
    await manager.patch_camera(view.camera.id, CameraPatchRequest(enabled=False))
    assert cameras.get(view.camera.id).enabled is False
    assert worker.stopped is True
    assert manager.runtime_snapshot(view.camera.id).state is RuntimeState.DISABLED


async def test_enable_creates_a_new_instance_and_session(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    first = worker_fakes.FakeWorker.created[0]
    await manager.patch_camera(view.camera.id, CameraPatchRequest(enabled=False))
    await manager.patch_camera(view.camera.id, CameraPatchRequest(enabled=True))
    second = worker_fakes.FakeWorker.created[-1]
    assert second is not first
    assert second.worker_instance_id != first.worker_instance_id
    assert second.session_id != first.session_id


async def test_disable_is_idempotent(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request(enabled=False))
    await manager.patch_camera(view.camera.id, CameraPatchRequest(enabled=False))
    assert worker_fakes.FakeWorker.created == []


async def test_a_stop_timeout_keeps_the_row_disabled_and_starts_no_replacement(context):
    manager, cameras, _lines, _detector = context
    manager._worker_factory = worker_fakes.factory(hangs_on_stop=True)  # noqa: SLF001
    await ready(manager)
    view = await manager.create_camera(create_request())
    with pytest.raises(WorkerStopTimeout):
        await manager.patch_camera(view.camera.id, CameraPatchRequest(enabled=False))
    assert cameras.get(view.camera.id).enabled is False
    assert len(worker_fakes.FakeWorker.created) == 1
    runtime = manager.runtime_snapshot(view.camera.id)
    assert runtime.last_error_code == "worker_stop_timeout"
    assert runtime.stopping is True


async def test_a_late_worker_exit_clears_the_stop_timeout_state(context):
    manager, _cameras, _lines, _detector = context
    manager._worker_factory = worker_fakes.factory(hangs_on_stop=True)  # noqa: SLF001
    await ready(manager)
    view = await manager.create_camera(create_request())
    with pytest.raises(WorkerStopTimeout):
        await manager.patch_camera(view.camera.id, CameraPatchRequest(enabled=False))
    worker_fakes.FakeWorker.created[0].exit_late()
    runtime = manager.runtime_snapshot(view.camera.id)
    assert runtime.state is RuntimeState.DISABLED
    assert runtime.stopping is False


# -- patches ----------------------------------------------------------------


async def test_a_name_only_patch_keeps_the_session_and_the_pipeline(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    worker = worker_fakes.FakeWorker.created[0]
    before = worker.session_id
    await manager.patch_camera(view.camera.id, CameraPatchRequest(name="Renamed"))
    assert len(worker_fakes.FakeWorker.created) == 1
    assert worker.installed[-1].requested_session_id == before
    assert worker.installed[-1].camera_name == "Renamed"


@pytest.mark.parametrize(
    "patch",
    [
        {"inference_fps": 9.0},
        {"confidence_threshold": 0.5},
        {"enabled_classes": ["person"]},
        {"vms_camera_id": "cam_beefbeef"},
    ],
)
async def test_a_semantic_patch_resets_the_session_but_not_the_thread(context, patch):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    worker = worker_fakes.FakeWorker.created[0]
    before = worker.session_id
    await manager.patch_camera(view.camera.id, CameraPatchRequest(**patch))
    assert len(worker_fakes.FakeWorker.created) == 1  # no new thread
    assert worker.installed[-1].requested_session_id != before


async def test_an_rtsp_patch_stops_before_it_replaces(context):
    manager, cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    first = worker_fakes.FakeWorker.created[0]
    new_url = "rtsp://host.docker.internal:8555/vms_cam_beefbeef"
    await manager.patch_camera(view.camera.id, CameraPatchRequest(rtsp_url=new_url))
    assert first.stopped is True
    assert len(worker_fakes.FakeWorker.created) == 2
    assert worker_fakes.FakeWorker.created[1].config.rtsp_url == new_url
    assert cameras.get(view.camera.id).rtsp_url == new_url


async def test_an_rtsp_patch_persists_before_it_stops(context):
    manager, cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    order: list[str] = []
    worker = worker_fakes.FakeWorker.created[0]
    real_update = cameras.update
    cameras.update = lambda record: (order.append("persist"), real_update(record))[1]
    original_stop = worker.stop
    worker.stop = lambda: (order.append("stop"), original_stop())[1]
    await manager.patch_camera(
        view.camera.id, CameraPatchRequest(rtsp_url="rtsp://other:8555/x")
    )
    assert order == ["persist", "stop"]


async def test_an_rtsp_stop_timeout_starts_no_second_worker(context):
    manager, _cameras, _lines, _detector = context
    manager._worker_factory = worker_fakes.factory(hangs_on_stop=True)  # noqa: SLF001
    await ready(manager)
    view = await manager.create_camera(create_request())
    with pytest.raises(WorkerStopTimeout):
        await manager.patch_camera(
            view.camera.id, CameraPatchRequest(rtsp_url="rtsp://other:8555/x")
        )
    assert len(worker_fakes.FakeWorker.created) == 1


async def test_a_multi_field_patch_performs_exactly_one_runtime_action(context):
    manager, cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    await manager.patch_camera(
        view.camera.id,
        CameraPatchRequest(
            name="Renamed", inference_fps=8.0, rtsp_url="rtsp://other:8555/x"
        ),
    )
    # One stop and exactly one replacement, carrying the complete final config.
    assert len(worker_fakes.FakeWorker.created) == 2
    replacement = worker_fakes.FakeWorker.created[1].config
    assert replacement.camera_name == "Renamed"
    assert replacement.inference_fps == 8.0
    assert replacement.rtsp_url == "rtsp://other:8555/x"


async def test_disabling_wins_over_every_other_change_in_one_patch(context):
    manager, cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    await manager.patch_camera(
        view.camera.id,
        CameraPatchRequest(enabled=False, rtsp_url="rtsp://other:8555/x"),
    )
    assert len(worker_fakes.FakeWorker.created) == 1
    assert cameras.get(view.camera.id).rtsp_url == "rtsp://other:8555/x"


async def test_a_duplicate_vms_id_in_a_patch_is_refused(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    first = await manager.create_camera(create_request())
    await manager.create_camera(
        create_request(vms_camera_id="cam_00000002", name="Second")
    )
    with pytest.raises(DuplicateVmsCameraId):
        await manager.patch_camera(
            first.camera.id, CameraPatchRequest(vms_camera_id="cam_00000002")
        )


async def test_patching_an_unknown_camera_is_not_found(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    with pytest.raises(CameraNotFound):
        await manager.patch_camera("acam_ffffffff", CameraPatchRequest(name="x"))


# -- lines ------------------------------------------------------------------


async def test_a_line_put_resets_the_session_without_a_new_thread(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    worker = worker_fakes.FakeWorker.created[0]
    before = worker.session_id
    record, created = await manager.put_line(view.camera.id, line_request())
    assert created is True
    assert len(worker_fakes.FakeWorker.created) == 1
    assert worker.installed[-1].requested_session_id != before
    assert worker.installed[-1].line.id == record.id


async def test_a_second_line_put_updates_in_place(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    first, created_first = await manager.put_line(view.camera.id, line_request())
    second, created_second = await manager.put_line(
        view.camera.id, line_request(name="Moved", direction=LineDirection.BOTH)
    )
    assert created_first is True and created_second is False
    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.direction is LineDirection.BOTH


async def test_deleting_a_line_installs_none_and_resets(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    await manager.put_line(view.camera.id, line_request())
    worker = worker_fakes.FakeWorker.created[0]
    before = worker.session_id
    await manager.delete_line(view.camera.id)
    assert worker.installed[-1].line is None
    assert worker.installed[-1].requested_session_id != before


async def test_deleting_a_missing_line_is_idempotent(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    await manager.delete_line(view.camera.id)
    await manager.delete_line(view.camera.id)


async def test_line_operations_on_an_unknown_camera_are_not_found(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    with pytest.raises(CameraNotFound):
        await manager.get_line("acam_ffffffff")
    with pytest.raises(CameraNotFound):
        await manager.put_line("acam_ffffffff", line_request())


# -- delete -----------------------------------------------------------------


async def test_delete_stops_first_then_removes_the_row_and_keeps_events(context):
    manager, cameras, lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    await manager.put_line(view.camera.id, line_request())
    from tests.fakes import factories

    events = EventRepository(Database(manager._storage._data_root / "analytics.db"))  # noqa: SLF001
    events.insert(factories.event(camera_id=view.camera.id))

    worker = worker_fakes.FakeWorker.created[0]
    await manager.delete_camera(view.camera.id)
    assert worker.stopped is True
    assert cameras.get(view.camera.id) is None
    assert lines.get(view.camera.id) is None
    assert events.count() == 1


async def test_delete_on_a_stop_timeout_keeps_the_disabled_row(context):
    manager, cameras, _lines, _detector = context
    manager._worker_factory = worker_fakes.factory(hangs_on_stop=True)  # noqa: SLF001
    await ready(manager)
    view = await manager.create_camera(create_request())
    with pytest.raises(WorkerStopTimeout):
        await manager.delete_camera(view.camera.id)
    row = cameras.get(view.camera.id)
    assert row is not None and row.enabled is False


async def test_deleting_twice_is_not_found(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request(enabled=False))
    await manager.delete_camera(view.camera.id)
    with pytest.raises(CameraNotFound):
        await manager.delete_camera(view.camera.id)


# -- instance fencing -------------------------------------------------------


async def test_a_callback_from_a_retired_instance_is_ignored(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    stale = worker_fakes.FakeWorker.created[0]
    await manager.patch_camera(
        view.camera.id, CameraPatchRequest(rtsp_url="rtsp://other:8555/x")
    )
    current = manager.runtime_snapshot(view.camera.id)
    stale.become_running()  # a late publish from the retired thread
    after = manager.runtime_snapshot(view.camera.id)
    assert after.worker_instance_id == current.worker_instance_id
    assert after.worker_instance_id != stale.worker_instance_id


async def test_a_callback_for_an_unknown_camera_is_ignored(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    from app.analytics.types import WorkerRuntime
    from app.domain.models import utc_now

    manager._publish_runtime(  # noqa: SLF001
        "acam_ffffffff",
        "wi_" + "0" * 32,
        WorkerRuntime(
            camera_id="acam_ffffffff",
            worker_instance_id="wi_" + "0" * 32,
            worker_session_id=None,
            state=RuntimeState.RUNNING,
            stopping=False,
            last_frame_at=None,
            last_inference_at=None,
            last_event_at=None,
            reconnect_attempt=0,
            applied_config_revision=1,
            last_error_code=None,
            last_error_message=None,
            updated_at=utc_now(),
        ),
    )
    assert manager.runtime_snapshot("acam_ffffffff").state is RuntimeState.DISABLED


# -- serialisation ----------------------------------------------------------


async def test_concurrent_patches_on_one_camera_serialise(context):
    import asyncio

    manager, cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    await asyncio.gather(
        manager.patch_camera(view.camera.id, CameraPatchRequest(name="A")),
        manager.patch_camera(view.camera.id, CameraPatchRequest(name="B")),
        manager.patch_camera(view.camera.id, CameraPatchRequest(name="C")),
    )
    assert cameras.get(view.camera.id).name in {"A", "B", "C"}
    assert len(worker_fakes.FakeWorker.created) == 1


async def test_different_cameras_proceed_concurrently(context):
    import asyncio

    manager, _cameras, _lines, _detector = context
    await ready(manager)
    first = await manager.create_camera(create_request())
    second = await manager.create_camera(
        create_request(vms_camera_id="cam_00000002", name="Second")
    )
    await asyncio.gather(
        manager.patch_camera(first.camera.id, CameraPatchRequest(name="A")),
        manager.patch_camera(second.camera.id, CameraPatchRequest(name="B")),
    )
    assert len(worker_fakes.FakeWorker.created) == 2


# -- snapshot and shutdown --------------------------------------------------


async def test_snapshot_before_the_first_frame_is_not_ready(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    with pytest.raises(SnapshotNotReady):
        await manager.get_snapshot(view.camera.id)


async def test_snapshot_of_an_unknown_camera_is_not_found(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    with pytest.raises(CameraNotFound):
        await manager.get_snapshot("acam_ffffffff")


async def test_shutdown_joins_every_worker_without_changing_desired_state(context):
    manager, cameras, _lines, _detector = context
    await ready(manager)
    await manager.create_camera(create_request())
    await manager.create_camera(
        create_request(vms_camera_id="cam_00000002", name="Second")
    )
    report = await manager.shutdown_all()
    assert report.stopped == 2
    assert report.timed_out == []
    assert all(record.enabled for record in cameras.list())


async def test_shutdown_reports_a_worker_that_would_not_stop(context):
    manager, _cameras, _lines, _detector = context
    manager._worker_factory = worker_fakes.factory(hangs_on_stop=True)  # noqa: SLF001
    await ready(manager)
    view = await manager.create_camera(create_request())
    report = await manager.shutdown_all()
    assert report.timed_out == [view.camera.id]


async def test_worker_counts_feed_health(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    worker_fakes.FakeWorker.created[0].become_running()
    counts = manager.worker_counts()
    assert counts["enabled"] == 1 and counts["running"] == 1
    await manager.patch_camera(view.camera.id, CameraPatchRequest(enabled=False))
    assert manager.worker_counts() == {
        "enabled": 0,
        "running": 0,
        "reconnecting": 0,
        "error": 0,
    }


async def test_list_and_get_expose_the_line_and_runtime(context):
    manager, _cameras, _lines, _detector = context
    await ready(manager)
    view = await manager.create_camera(create_request())
    await manager.put_line(view.camera.id, line_request())
    listed = await manager.list_cameras()
    assert len(listed) == 1 and listed[0].line is not None
    single = await manager.get_camera(view.camera.id)
    assert single.line is not None
    assert single.camera.enabled_classes == frozenset(
        {ObjectCategory.PERSON, ObjectCategory.VEHICLE}
    )
    with pytest.raises(CameraNotFound):
        await manager.get_camera("acam_ffffffff")
