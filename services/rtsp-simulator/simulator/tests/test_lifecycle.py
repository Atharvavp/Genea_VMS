"""Process supervision and camera lifecycle behaviour."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest

from app.config import Settings
from app.domain.models import CameraCreate, CameraStatus, CameraUpdate
from app.persistence.camera_repository import CameraRepository
from app.services.camera_manager import CameraManager
from app.services.process_manager import (
    AlreadyRunning,
    FFmpegProcessManager,
    FFmpegUnavailable,
    ProcessExit,
    StartupFailed,
)


class FakeUpload:
    def __init__(self, filename: str = "parking.mp4", data: bytes = b"video-bytes" * 64) -> None:
        self.filename = filename
        self._stream = io.BytesIO(data)

    async def read(self, size: int) -> bytes:
        return self._stream.read(size)


async def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll until ``predicate()`` is true (process exits are asynchronous)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def status_of(manager: CameraManager, camera_id: str) -> CameraStatus:
    return (await manager.get_camera(camera_id)).status


# --- FFmpegProcessManager ----------------------------------------------


async def test_process_survives_startup_grace(processes, stub_bin) -> None:
    managed = await processes.start("cam_1", [str(stub_bin["long_running"]), "-i", "x"])
    try:
        assert processes.is_running("cam_1")
        assert managed.pid > 0
    finally:
        await processes.stop("cam_1")


async def test_early_exit_raises_startup_failed_with_stderr(processes, stub_bin) -> None:
    with pytest.raises(StartupFailed) as excinfo:
        await processes.start("cam_1", [str(stub_bin["failing"])])
    assert "Connection refused" in str(excinfo.value)
    assert excinfo.value.returncode == 1
    # Nothing is left registered after a failed start.
    assert not processes.is_running("cam_1")
    assert processes.running_camera_ids() == []


async def test_missing_binary_is_reported(processes) -> None:
    with pytest.raises(FFmpegUnavailable):
        await processes.start("cam_1", ["/nonexistent/ffmpeg", "-i", "x"])


async def test_duplicate_start_is_refused(processes, stub_bin) -> None:
    await processes.start("cam_1", [str(stub_bin["long_running"])])
    try:
        with pytest.raises(AlreadyRunning):
            await processes.start("cam_1", [str(stub_bin["long_running"])])
        assert len(processes.running_camera_ids()) == 1
    finally:
        await processes.stop("cam_1")


async def test_requested_stop_is_flagged_as_requested(processes, stub_bin) -> None:
    events: list[ProcessExit] = []
    processes.set_exit_callback(lambda event: _record(events, event))

    await processes.start("cam_1", [str(stub_bin["long_running"])])
    assert await processes.stop("cam_1") is True
    assert not processes.is_running("cam_1")
    assert await processes.stop("cam_1") is False  # idempotent
    assert events and events[0].requested is True


async def test_unexpected_exit_is_reported_to_the_owner(processes, stub_bin) -> None:
    events: list[ProcessExit] = []
    processes.set_exit_callback(lambda event: _record(events, event))

    await processes.start("cam_1", [str(stub_bin["short_failure"])])
    assert await wait_for(lambda: bool(events))
    assert events[0].requested is False
    assert events[0].returncode == 3
    assert any("broken pipe" in line for line in events[0].stderr_tail)
    assert not processes.is_running("cam_1")


async def test_clean_exit_is_reported_with_returncode_zero(processes, stub_bin) -> None:
    events: list[ProcessExit] = []
    processes.set_exit_callback(lambda event: _record(events, event))

    await processes.start("cam_1", [str(stub_bin["short_success"])])
    assert await wait_for(lambda: bool(events))
    assert events[0].returncode == 0
    assert events[0].requested is False


async def test_stubborn_process_is_force_killed(processes, stub_bin) -> None:
    await processes.start("cam_1", [str(stub_bin["stubborn"])])
    managed = processes.get("cam_1")
    await processes.stop("cam_1")
    assert not processes.is_running("cam_1")
    assert managed.process.returncode != 0


async def test_stop_all_stops_every_publisher(processes, stub_bin) -> None:
    for camera_id in ("cam_1", "cam_2", "cam_3"):
        await processes.start(camera_id, [str(stub_bin["long_running"])])
    assert len(processes.running_camera_ids()) == 3
    await processes.stop_all()
    assert processes.running_camera_ids() == []


async def _record(events: list, event: ProcessExit) -> None:
    events.append(event)


# --- CameraManager ------------------------------------------------------


async def create_camera(manager: CameraManager, name: str = "Parking Entrance", **kwargs):
    payload = CameraCreate(name=name, source={"kind": "upload"}, **kwargs)
    return await manager.create_camera(payload, FakeUpload())


async def test_create_without_auto_start_ends_stopped(manager: CameraManager) -> None:
    view = await create_camera(manager)
    assert view.status is CameraStatus.STOPPED
    assert view.stream_path == "parking-entrance"
    assert view.rtsp_url == "rtsp://localhost:8554/simulator/parking-entrance"
    assert view.runtime.pid is None
    assert view.source.width == 1920  # from the fake probe


async def test_create_with_auto_start_runs(manager: CameraManager) -> None:
    view = await create_camera(manager, auto_start=True)
    assert view.status is CameraStatus.RUNNING
    assert view.runtime.pid is not None
    await manager.shutdown()


async def test_create_with_auto_start_records_error_when_ffmpeg_fails(
    manager: CameraManager, settings: Settings, stub_bin
) -> None:
    settings.ffmpeg_binary = str(stub_bin["failing"])
    view = await create_camera(manager, auto_start=True)
    assert view.status is CameraStatus.ERROR
    assert "Connection refused" in view.last_error
    assert view.runtime.pid is None


async def test_start_stop_restart_cycle(manager: CameraManager) -> None:
    camera = await create_camera(manager)

    started = await manager.start_camera(camera.id)
    assert started.status is CameraStatus.RUNNING
    first_pid = started.runtime.pid

    # Starting again is idempotent and does not spawn a second publisher.
    again = await manager.start_camera(camera.id)
    assert again.status is CameraStatus.RUNNING
    assert again.runtime.pid == first_pid

    stopped = await manager.stop_camera(camera.id)
    assert stopped.status is CameraStatus.STOPPED
    assert stopped.runtime.pid is None
    assert (await manager.stop_camera(camera.id)).status is CameraStatus.STOPPED

    restarted = await manager.restart_camera(camera.id)
    assert restarted.status is CameraStatus.RUNNING
    assert restarted.runtime.pid != first_pid
    await manager.shutdown()


async def test_runtime_affecting_update_restarts_the_publisher(manager: CameraManager) -> None:
    camera = await create_camera(manager)
    running = await manager.start_camera(camera.id)

    updated = await manager.update_camera(
        camera.id,
        CameraUpdate.model_validate({"video": {"fps": {"mode": "fixed", "value": 10}}}),
        None,
    )
    assert updated.status is CameraStatus.RUNNING
    assert updated.video.fps.value == 10
    assert updated.runtime.pid != running.runtime.pid
    await manager.shutdown()


async def test_cosmetic_update_does_not_restart(manager: CameraManager) -> None:
    camera = await create_camera(manager)
    running = await manager.start_camera(camera.id)

    updated = await manager.update_camera(
        camera.id, CameraUpdate(name="Renamed", auto_start=True), None
    )
    assert updated.name == "Renamed"
    assert updated.auto_start is True
    assert updated.runtime.pid == running.runtime.pid
    await manager.shutdown()


async def test_update_of_stopped_camera_only_persists(manager: CameraManager) -> None:
    camera = await create_camera(manager)
    updated = await manager.update_camera(
        camera.id, CameraUpdate(loop=False, stream_path="new-path"), None
    )
    assert updated.status is CameraStatus.STOPPED
    assert updated.loop is False
    assert updated.rtsp_url.endswith("/simulator/new-path")


async def test_replacing_the_source_deletes_the_previous_upload(
    manager: CameraManager, settings: Settings
) -> None:
    camera = await create_camera(manager)
    before = sorted(settings.upload_video_dir.iterdir())
    assert len(before) == 1

    await manager.update_camera(
        camera.id, CameraUpdate(source={"kind": "upload"}), FakeUpload("lobby.mp4")
    )
    after = sorted(settings.upload_video_dir.iterdir())
    assert len(after) == 1
    assert after[0] != before[0]
    view = await manager.get_camera(camera.id)
    assert view.source.filename == "lobby.mp4"


async def test_delete_stops_process_and_removes_owned_upload(
    manager: CameraManager, settings: Settings
) -> None:
    camera = await create_camera(manager)
    await manager.start_camera(camera.id)
    assert len(list(settings.upload_video_dir.iterdir())) == 1

    await manager.delete_camera(camera.id)
    assert await manager.list_cameras() == []
    assert list(settings.upload_video_dir.iterdir()) == []


async def test_non_looping_end_of_stream_becomes_stopped(
    manager: CameraManager, settings: Settings, stub_bin
) -> None:
    settings.ffmpeg_binary = str(stub_bin["short_success"])
    camera = await create_camera(manager, loop=False)
    await manager.start_camera(camera.id)
    assert await _await_status(manager, camera.id, CameraStatus.STOPPED)
    view = await manager.get_camera(camera.id)
    assert view.last_error is None


async def test_unexpected_exit_becomes_error(
    manager: CameraManager, settings: Settings, stub_bin
) -> None:
    settings.ffmpeg_binary = str(stub_bin["short_failure"])
    camera = await create_camera(manager, loop=True)
    await manager.start_camera(camera.id)
    assert await _await_status(manager, camera.id, CameraStatus.ERROR)
    view = await manager.get_camera(camera.id)
    assert "broken pipe" in view.last_error
    assert view.runtime.pid is None


async def test_looping_camera_exiting_zero_is_still_an_error(
    manager: CameraManager, settings: Settings, stub_bin
) -> None:
    settings.ffmpeg_binary = str(stub_bin["short_success"])
    camera = await create_camera(manager, loop=True)
    await manager.start_camera(camera.id)
    assert await _await_status(manager, camera.id, CameraStatus.ERROR)


async def test_cameras_run_independently(manager: CameraManager) -> None:
    first = await create_camera(manager, "Parking Entrance")
    second = await create_camera(manager, "Lobby")
    assert first.stream_path != second.stream_path

    await manager.start_camera(first.id)
    await manager.start_camera(second.id)
    assert (await status_of(manager, first.id)) is CameraStatus.RUNNING
    assert (await status_of(manager, second.id)) is CameraStatus.RUNNING

    await manager.stop_camera(first.id)
    assert (await status_of(manager, first.id)) is CameraStatus.STOPPED
    assert (await status_of(manager, second.id)) is CameraStatus.RUNNING
    await manager.shutdown()


async def test_duplicate_names_get_distinct_stream_paths(manager: CameraManager) -> None:
    first = await create_camera(manager, "Lobby")
    second = await create_camera(manager, "Lobby")
    assert first.stream_path == "lobby"
    assert second.stream_path == "lobby-2"


async def test_shutdown_stops_every_publisher(manager: CameraManager) -> None:
    first = await create_camera(manager, "One")
    second = await create_camera(manager, "Two")
    await manager.start_camera(first.id)
    await manager.start_camera(second.id)

    await manager.shutdown()
    assert (await manager.get_camera(first.id)).runtime.pid is None
    assert (await manager.get_camera(second.id)).runtime.pid is None


async def test_startup_reconciles_stale_state_and_honours_auto_start(
    settings: Settings, repository: CameraRepository, storage, prober
) -> None:
    first_manager = CameraManager(
        settings, repository, storage, prober, FFmpegProcessManager(0.3, 1.0, 10)
    )
    stale = await create_camera(first_manager, "Stale", auto_start=False)
    auto = await create_camera(first_manager, "Auto", auto_start=True)
    assert (await status_of(first_manager, auto.id)) is CameraStatus.RUNNING

    # Simulate a hard restart: processes are gone, persisted state is stale.
    await first_manager.shutdown()
    repository.update_status(
        stale.id, CameraStatus.RUNNING, None, (await first_manager.get_camera(stale.id)).updated_at
    )

    second_manager = CameraManager(
        settings, repository, storage, prober, FFmpegProcessManager(0.3, 1.0, 10)
    )
    await second_manager.startup()
    try:
        assert (await status_of(second_manager, stale.id)) is CameraStatus.STOPPED
        assert (await status_of(second_manager, auto.id)) is CameraStatus.RUNNING
        assert (await second_manager.get_camera(auto.id)).runtime.pid is not None
    finally:
        await second_manager.shutdown()


async def test_start_fails_when_source_file_disappeared(
    manager: CameraManager, settings: Settings
) -> None:
    camera = await create_camera(manager)
    Path((await manager.get_camera(camera.id)).source.filename)  # display name only
    for stored in settings.upload_video_dir.iterdir():
        stored.unlink()

    view = await manager.start_camera(camera.id)
    assert view.status is CameraStatus.ERROR
    assert "no longer available" in view.last_error


async def _await_status(
    manager: CameraManager, camera_id: str, expected: CameraStatus, timeout: float = 6.0
) -> bool:
    async def matches() -> bool:
        return (await manager.get_camera(camera_id)).status is expected

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await matches():
            return True
        await asyncio.sleep(0.05)
    return await matches()
