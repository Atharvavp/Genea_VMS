"""The single desired-state orchestrator.

Every camera and line mutation goes through here. The manager owns the worker
registry, per-camera serialisation, and worker-instance fencing. Route modules
never create workers, never touch SQLite, and never resolve a filesystem path.

Two identifiers are deliberately different:

* ``worker_instance_id`` fences callbacks from a retired thread;
* ``worker_session_id`` defines tracker and dedupe continuity.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable

from app.analytics.detector import Detector
from app.analytics.stream_worker import StreamWorker
from app.analytics.types import FatalReason, LatestFrame, WorkerConfig, WorkerRuntime
from app.domain.models import (
    CameraCreateRequest,
    CameraPatchRequest,
    CameraRecord,
    LineDirection,
    LinePutRequest,
    LineRecord,
    ObjectCategory,
    RuntimeState,
    new_camera_id,
    new_line_id,
    new_worker_instance_id,
    new_worker_session_id,
    utc_now,
)
from app.persistence.camera_repository import CameraRepository, DuplicateVmsCameraId
from app.persistence.event_storage import EventStorage
from app.persistence.line_repository import LineRepository

__all__ = [
    "AnalyticsCameraManager",
    "CameraView",
    "WorkerEntry",
    "StartupReport",
    "ShutdownReport",
    "CameraNotFound",
    "LineNotConfigured",
    "DuplicateVmsCameraId",
    "WorkerStopTimeout",
    "SnapshotNotReady",
]

logger = logging.getLogger("analytics.manager")


class CameraNotFound(LookupError):
    pass


class LineNotConfigured(LookupError):
    pass


class WorkerStopTimeout(RuntimeError):
    pass


class SnapshotNotReady(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CameraView:
    camera: CameraRecord
    line: LineRecord | None
    runtime: WorkerRuntime


@dataclass
class WorkerEntry:
    worker: StreamWorker | None
    worker_instance_id: str | None
    runtime: WorkerRuntime
    config_revision: int


@dataclass(frozen=True, slots=True)
class StartupReport:
    cameras: int
    started: int
    degraded: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    stopped: int
    timed_out: list[str] = field(default_factory=list)


def _idle_runtime(camera_id: str, state: RuntimeState, revision: int) -> WorkerRuntime:
    return WorkerRuntime(
        camera_id=camera_id,
        worker_instance_id=None,
        worker_session_id=None,
        state=state,
        stopping=False,
        last_frame_at=None,
        last_inference_at=None,
        last_event_at=None,
        reconnect_attempt=0,
        applied_config_revision=revision,
        last_error_code=None,
        last_error_message=None,
        updated_at=utc_now(),
    )


class AnalyticsCameraManager:
    def __init__(
        self,
        *,
        cameras: CameraRepository,
        lines: LineRepository,
        event_storage: EventStorage,
        detector: Detector,
        stall_seconds: float = 10.0,
        stop_timeout_seconds: float = 10.0,
        worker_factory: Callable[..., StreamWorker] | None = None,
        id_factory: Callable[[], str] = new_camera_id,
        line_id_factory: Callable[[], str] = new_line_id,
        instance_id_factory: Callable[[], str] = new_worker_instance_id,
        session_id_factory: Callable[[], str] = new_worker_session_id,
        clock: Callable[[], object] = utc_now,
    ):
        self._cameras = cameras
        self._lines = lines
        self._storage = event_storage
        self._detector = detector
        self._stall_seconds = float(stall_seconds)
        self._stop_timeout = float(stop_timeout_seconds)
        self._worker_factory = worker_factory or StreamWorker
        self._new_camera_id = id_factory
        self._new_line_id = line_id_factory
        self._new_instance_id = instance_id_factory
        self._new_session_id = session_id_factory
        self._clock = clock

        self._registry: dict[str, WorkerEntry] = {}
        self._registry_lock = threading.RLock()
        self._camera_locks: dict[str, asyncio.Lock] = {}
        self._map_lock = asyncio.Lock()
        self._revision = 0
        self._detector_ready = False

    # -- registry helpers ---------------------------------------------------

    async def _camera_lock(self, camera_id: str) -> asyncio.Lock:
        async with self._map_lock:
            lock = self._camera_locks.get(camera_id)
            if lock is None:
                lock = asyncio.Lock()
                self._camera_locks[camera_id] = lock
            return lock

    def _next_revision(self) -> int:
        with self._registry_lock:
            self._revision += 1
            return self._revision

    def _entry(self, camera_id: str) -> WorkerEntry | None:
        with self._registry_lock:
            return self._registry.get(camera_id)

    def _set_entry(self, camera_id: str, entry: WorkerEntry) -> None:
        with self._registry_lock:
            self._registry[camera_id] = entry

    def _publish_runtime(
        self, camera_id: str, worker_instance_id: str, runtime: WorkerRuntime
    ) -> None:
        """Worker callback. Ignored unless it comes from the current instance."""
        with self._registry_lock:
            entry = self._registry.get(camera_id)
            if entry is None or entry.worker_instance_id != worker_instance_id:
                return
            stopping = entry.runtime.stopping
            if runtime.state in (RuntimeState.DISABLED, RuntimeState.ERROR):
                stopping = False
            entry.runtime = runtime.replace(stopping=stopping)

    def _assert_current_instance(self, camera_id: str, instance_id: str) -> bool:
        with self._registry_lock:
            entry = self._registry.get(camera_id)
            return entry is not None and entry.worker_instance_id == instance_id

    def runtime_snapshot(self, camera_id: str) -> WorkerRuntime:
        entry = self._entry(camera_id)
        if entry is None:
            return _idle_runtime(camera_id, RuntimeState.DISABLED, 0)
        with self._registry_lock:
            return entry.runtime

    # -- config snapshots ---------------------------------------------------

    def _build_worker_config(
        self,
        camera: CameraRecord,
        line: LineRecord | None,
        *,
        revision: int,
        requested_session_id: str,
    ) -> WorkerConfig:
        return WorkerConfig(
            camera_id=camera.id,
            vms_camera_id=camera.vms_camera_id,
            camera_name=camera.name,
            rtsp_url=camera.rtsp_url,
            inference_fps=camera.inference_fps,
            confidence_threshold=camera.confidence_threshold,
            enabled_categories=camera.enabled_classes,
            line=line,
            config_revision=revision,
            requested_session_id=requested_session_id,
        )

    # -- worker lifecycle ---------------------------------------------------

    def _start_worker(self, camera: CameraRecord, line: LineRecord | None) -> None:
        """Create and start exactly one worker. Caller holds the camera lock."""
        instance_id = self._new_instance_id()
        revision = self._next_revision()
        config = self._build_worker_config(
            camera,
            line,
            revision=revision,
            requested_session_id=self._new_session_id(),
        )
        entry = WorkerEntry(
            worker=None,
            worker_instance_id=instance_id,
            runtime=_idle_runtime(camera.id, RuntimeState.STARTING, revision),
            config_revision=revision,
        )
        self._set_entry(camera.id, entry)

        try:
            worker = self._worker_factory(
                config=config,
                worker_instance_id=instance_id,
                detector=self._detector,
                event_storage=self._storage,
                on_runtime=self._publish_runtime,
                stall_seconds=self._stall_seconds,
            )
            worker.start()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "worker_start_failed",
                extra={"camera_id": camera.id, "error_type": type(exc).__name__},
            )
            with self._registry_lock:
                entry.worker = None
                entry.runtime = entry.runtime.replace(
                    state=RuntimeState.ERROR,
                    last_error_code=FatalReason.WORKER_START_FAILED.value,
                    last_error_message="The analytics worker thread could not be started.",
                    updated_at=utc_now(),
                )
            return
        with self._registry_lock:
            entry.worker = worker
        logger.info(
            "worker_started",
            extra={"camera_id": camera.id, "worker_instance": instance_id},
        )

    def _mark_degraded(self, camera: CameraRecord) -> None:
        revision = self._next_revision()
        runtime = _idle_runtime(camera.id, RuntimeState.ERROR, revision).replace(
            last_error_code=FatalReason.MODEL_UNAVAILABLE.value,
            last_error_message="The detector is unavailable; analytics cannot run.",
        )
        self._set_entry(
            camera.id,
            WorkerEntry(
                worker=None,
                worker_instance_id=None,
                runtime=runtime,
                config_revision=revision,
            ),
        )

    async def _stop_worker(self, camera_id: str) -> bool:
        """Signal, then bounded join. Returns whether the thread actually exited."""
        entry = self._entry(camera_id)
        if entry is None or entry.worker is None:
            return True
        worker = entry.worker
        with self._registry_lock:
            entry.runtime = entry.runtime.replace(stopping=True, updated_at=utc_now())
        worker.stop()
        await asyncio.to_thread(worker.join, self._stop_timeout)
        if worker.is_alive():
            with self._registry_lock:
                entry.runtime = entry.runtime.replace(
                    state=RuntimeState.ERROR,
                    stopping=True,
                    last_error_code=FatalReason.WORKER_STOP_TIMEOUT.value,
                    last_error_message=(
                        "The analytics worker did not stop within the timeout."
                    ),
                    updated_at=utc_now(),
                )
            logger.error(
                "worker_stop_timeout",
                extra={"camera_id": camera_id, "timeout": self._stop_timeout},
            )
            return False
        with self._registry_lock:
            entry.worker = None
            entry.runtime = entry.runtime.replace(
                state=RuntimeState.DISABLED,
                stopping=False,
                worker_session_id=None,
                reconnect_attempt=0,
                last_error_code=None,
                last_error_message=None,
                updated_at=utc_now(),
            )
        return True

    def _install_config(
        self,
        camera: CameraRecord,
        line: LineRecord | None,
        *,
        new_session: bool,
    ) -> None:
        entry = self._entry(camera.id)
        if entry is None:
            return
        revision = self._next_revision()
        with self._registry_lock:
            entry.config_revision = revision
        worker = entry.worker
        if worker is None:
            with self._registry_lock:
                entry.runtime = entry.runtime.replace(
                    applied_config_revision=revision, updated_at=utc_now()
                )
            return
        current_session = entry.runtime.worker_session_id
        requested = (
            self._new_session_id()
            if new_session or current_session is None
            else current_session
        )
        worker.install_config(
            self._build_worker_config(
                camera, line, revision=revision, requested_session_id=requested
            )
        )

    # -- startup and shutdown ----------------------------------------------

    async def start_all_enabled(self, *, detector_ready: bool) -> StartupReport:
        self._detector_ready = detector_ready
        cameras = await asyncio.to_thread(self._cameras.list)
        lines = await asyncio.to_thread(self._lines.list_all)
        started = 0
        for camera in cameras:
            lock = await self._camera_lock(camera.id)
            async with lock:
                if not camera.enabled:
                    revision = self._next_revision()
                    self._set_entry(
                        camera.id,
                        WorkerEntry(
                            worker=None,
                            worker_instance_id=None,
                            runtime=_idle_runtime(
                                camera.id, RuntimeState.DISABLED, revision
                            ),
                            config_revision=revision,
                        ),
                    )
                    continue
                if not detector_ready:
                    self._mark_degraded(camera)
                    continue
                self._start_worker(camera, lines.get(camera.id))
                started += 1
        logger.info(
            "startup_complete",
            extra={
                "cameras": len(cameras),
                "workers_started": started,
                "detector_ready": detector_ready,
            },
        )
        return StartupReport(
            cameras=len(cameras),
            started=started,
            degraded=not detector_ready,
            detail="" if detector_ready else "detector unavailable",
        )

    async def shutdown_all(self, timeout_per_worker: float | None = None) -> ShutdownReport:
        """Join every worker concurrently, each bounded by the same timeout."""
        timeout = self._stop_timeout if timeout_per_worker is None else timeout_per_worker
        with self._registry_lock:
            workers = [
                (camera_id, entry.worker)
                for camera_id, entry in self._registry.items()
                if entry.worker is not None
            ]
            for _camera_id, entry in self._registry.items():
                if entry.worker is not None:
                    entry.runtime = entry.runtime.replace(stopping=True)
        for _camera_id, worker in workers:
            if worker is not None:
                worker.stop()

        async def _join(camera_id: str, worker: StreamWorker) -> str | None:
            await asyncio.to_thread(worker.join, timeout)
            if worker.is_alive():
                logger.error("shutdown_worker_timeout", extra={"camera_id": camera_id})
                return camera_id
            logger.info("shutdown_worker_stopped", extra={"camera_id": camera_id})
            return None

        results = await asyncio.gather(
            *(_join(camera_id, worker) for camera_id, worker in workers if worker)
        )
        timed_out = [camera_id for camera_id in results if camera_id]
        with self._registry_lock:
            for _camera_id, entry in self._registry.items():
                if entry.worker is not None and not entry.worker.is_alive():
                    entry.worker = None
                    entry.runtime = entry.runtime.replace(stopping=False)
        return ShutdownReport(stopped=len(workers) - len(timed_out), timed_out=timed_out)

    # -- read paths ---------------------------------------------------------

    async def list_cameras(self) -> list[CameraView]:
        cameras = await asyncio.to_thread(self._cameras.list)
        lines = await asyncio.to_thread(self._lines.list_all)
        return [
            CameraView(
                camera=camera,
                line=lines.get(camera.id),
                runtime=self.runtime_snapshot(camera.id),
            )
            for camera in cameras
        ]

    async def get_camera(self, camera_id: str) -> CameraView:
        camera = await asyncio.to_thread(self._cameras.get, camera_id)
        if camera is None:
            raise CameraNotFound(camera_id)
        line = await asyncio.to_thread(self._lines.get, camera_id)
        return CameraView(
            camera=camera, line=line, runtime=self.runtime_snapshot(camera_id)
        )

    async def get_line(self, camera_id: str) -> LineRecord | None:
        if not await asyncio.to_thread(self._cameras.exists, camera_id):
            raise CameraNotFound(camera_id)
        return await asyncio.to_thread(self._lines.get, camera_id)

    async def get_snapshot(self, camera_id: str) -> LatestFrame:
        entry = self._entry(camera_id)
        if entry is None or not await asyncio.to_thread(
            self._cameras.exists, camera_id
        ):
            raise CameraNotFound(camera_id)
        worker = entry.worker
        if worker is None:
            raise SnapshotNotReady(camera_id)
        frame = await asyncio.to_thread(worker.copy_latest_frame)
        if frame is None:
            raise SnapshotNotReady(camera_id)
        return frame

    # -- create -------------------------------------------------------------

    async def create_camera(self, request: CameraCreateRequest) -> CameraView:
        async with self._map_lock:
            existing = await asyncio.to_thread(
                self._cameras.get_by_vms_camera_id, request.vms_camera_id
            )
            if existing is not None:
                raise DuplicateVmsCameraId(request.vms_camera_id)
            now = self._clock()
            camera = CameraRecord(
                id=self._new_camera_id(),
                vms_camera_id=request.vms_camera_id,
                name=request.name,
                rtsp_url=request.rtsp_url,
                enabled=request.enabled,
                inference_fps=request.inference_fps,
                confidence_threshold=request.confidence_threshold,
                enabled_classes=request.categories(),
                created_at=now,  # type: ignore[arg-type]
                updated_at=now,  # type: ignore[arg-type]
            )
            await asyncio.to_thread(self._cameras.insert, camera)
            self._camera_locks.setdefault(camera.id, asyncio.Lock())

        revision = self._next_revision()
        if not camera.enabled:
            self._set_entry(
                camera.id,
                WorkerEntry(
                    worker=None,
                    worker_instance_id=None,
                    runtime=_idle_runtime(camera.id, RuntimeState.DISABLED, revision),
                    config_revision=revision,
                ),
            )
        elif not self._detector_ready:
            self._mark_degraded(camera)
        else:
            self._start_worker(camera, None)
        logger.info(
            "camera_created",
            extra={
                "camera_id": camera.id,
                "vms_camera_id": camera.vms_camera_id,
                "enabled": camera.enabled,
            },
        )
        return CameraView(
            camera=camera, line=None, runtime=self.runtime_snapshot(camera.id)
        )

    # -- patch --------------------------------------------------------------

    async def patch_camera(
        self, camera_id: str, request: CameraPatchRequest
    ) -> CameraView:
        lock = await self._camera_lock(camera_id)
        async with lock:
            camera = await asyncio.to_thread(self._cameras.get, camera_id)
            if camera is None:
                raise CameraNotFound(camera_id)
            line = await asyncio.to_thread(self._lines.get, camera_id)

            provided = request.model_fields_set
            categories: frozenset[ObjectCategory] = (
                request.categories() or camera.enabled_classes
            )
            updated = CameraRecord(
                id=camera.id,
                vms_camera_id=request.vms_camera_id or camera.vms_camera_id,
                name=request.name or camera.name,
                rtsp_url=request.rtsp_url or camera.rtsp_url,
                enabled=(
                    request.enabled if request.enabled is not None else camera.enabled
                ),
                inference_fps=(
                    request.inference_fps
                    if request.inference_fps is not None
                    else camera.inference_fps
                ),
                confidence_threshold=(
                    request.confidence_threshold
                    if request.confidence_threshold is not None
                    else camera.confidence_threshold
                ),
                enabled_classes=categories,
                created_at=camera.created_at,
                updated_at=self._clock(),  # type: ignore[arg-type]
            )
            if updated.vms_camera_id != camera.vms_camera_id:
                clash = await asyncio.to_thread(
                    self._cameras.get_by_vms_camera_id, updated.vms_camera_id
                )
                if clash is not None and clash.id != camera.id:
                    raise DuplicateVmsCameraId(updated.vms_camera_id)

            # Persist everything first: a crash must reconcile to the new row.
            await asyncio.to_thread(self._cameras.update, updated)

            rtsp_changed = "rtsp_url" in provided and updated.rtsp_url != camera.rtsp_url
            semantic_changed = any(
                (
                    updated.inference_fps != camera.inference_fps,
                    updated.confidence_threshold != camera.confidence_threshold,
                    updated.enabled_classes != camera.enabled_classes,
                    updated.vms_camera_id != camera.vms_camera_id,
                )
            )

            # Exactly one runtime action per PATCH.
            if not updated.enabled:
                if camera.enabled and not await self._stop_worker(camera_id):
                    raise WorkerStopTimeout(camera_id)
                if not camera.enabled:
                    self._install_config(updated, line, new_session=False)
            elif not camera.enabled:
                if self._detector_ready:
                    self._start_worker(updated, line)
                else:
                    self._mark_degraded(updated)
            elif rtsp_changed:
                if not await self._stop_worker(camera_id):
                    raise WorkerStopTimeout(camera_id)
                if self._detector_ready:
                    self._start_worker(updated, line)
                else:
                    self._mark_degraded(updated)
            else:
                self._install_config(updated, line, new_session=semantic_changed)

            logger.info(
                "camera_updated",
                extra={
                    "camera_id": camera_id,
                    "fields": ",".join(sorted(provided)),
                    "rtsp_changed": rtsp_changed,
                    "session_reset": semantic_changed or rtsp_changed,
                },
            )
            return CameraView(
                camera=updated, line=line, runtime=self.runtime_snapshot(camera_id)
            )

    # -- delete -------------------------------------------------------------

    async def delete_camera(self, camera_id: str) -> None:
        lock = await self._camera_lock(camera_id)
        async with lock:
            camera = await asyncio.to_thread(self._cameras.get, camera_id)
            if camera is None:
                raise CameraNotFound(camera_id)
            if camera.enabled:
                disabled = CameraRecord(
                    id=camera.id,
                    vms_camera_id=camera.vms_camera_id,
                    name=camera.name,
                    rtsp_url=camera.rtsp_url,
                    enabled=False,
                    inference_fps=camera.inference_fps,
                    confidence_threshold=camera.confidence_threshold,
                    enabled_classes=camera.enabled_classes,
                    created_at=camera.created_at,
                    updated_at=self._clock(),  # type: ignore[arg-type]
                )
                await asyncio.to_thread(self._cameras.update, disabled)
            if not await self._stop_worker(camera_id):
                raise WorkerStopTimeout(camera_id)
            # The line cascades. Events and event images are untouched.
            await asyncio.to_thread(self._cameras.delete, camera_id)
        async with self._map_lock:
            with self._registry_lock:
                self._registry.pop(camera_id, None)
            self._camera_locks.pop(camera_id, None)
        logger.info("camera_deleted", extra={"camera_id": camera_id})

    # -- line ---------------------------------------------------------------

    async def put_line(
        self, camera_id: str, request: LinePutRequest
    ) -> tuple[LineRecord, bool]:
        lock = await self._camera_lock(camera_id)
        async with lock:
            camera = await asyncio.to_thread(self._cameras.get, camera_id)
            if camera is None:
                raise CameraNotFound(camera_id)
            existing = await asyncio.to_thread(self._lines.get, camera_id)
            now = self._clock()
            record = LineRecord(
                id=existing.id if existing else self._new_line_id(),
                camera_id=camera_id,
                name=request.name,
                x1=request.a.x,
                y1=request.a.y,
                x2=request.b.x,
                y2=request.b.y,
                direction=LineDirection(request.direction),
                enabled=request.enabled,
                created_at=existing.created_at if existing else now,  # type: ignore[arg-type]
                updated_at=now,  # type: ignore[arg-type]
            )
            stored = await asyncio.to_thread(self._lines.upsert, record)
            self._install_config(camera, stored, new_session=True)
            logger.info(
                "line_configured",
                extra={
                    "camera_id": camera_id,
                    "line_id": stored.id,
                    "direction": stored.direction.value,
                    "line_created": existing is None,
                },
            )
            return stored, existing is None

    async def delete_line(self, camera_id: str) -> None:
        lock = await self._camera_lock(camera_id)
        async with lock:
            camera = await asyncio.to_thread(self._cameras.get, camera_id)
            if camera is None:
                raise CameraNotFound(camera_id)
            removed = await asyncio.to_thread(self._lines.delete, camera_id)
            if removed:
                self._install_config(camera, None, new_session=True)
                logger.info("line_deleted", extra={"camera_id": camera_id})

    # -- health -------------------------------------------------------------

    def worker_counts(self) -> dict[str, int]:
        with self._registry_lock:
            runtimes: Iterable[WorkerRuntime] = [
                entry.runtime for entry in self._registry.values()
            ]
        counts = {"enabled": 0, "running": 0, "reconnecting": 0, "error": 0}
        for runtime in runtimes:
            if runtime.state != RuntimeState.DISABLED:
                counts["enabled"] += 1
            if runtime.state == RuntimeState.RUNNING:
                counts["running"] += 1
            elif runtime.state == RuntimeState.RECONNECTING:
                counts["reconnecting"] += 1
            elif runtime.state == RuntimeState.ERROR:
                counts["error"] += 1
        return counts

    @property
    def detector_ready(self) -> bool:
        return self._detector_ready
