"""Camera orchestration: CRUD, lifecycle, supervision and reconciliation.

This is the only component that combines persistence, source storage, ffprobe
and the FFmpeg process manager. Every lifecycle or update operation for a given
camera runs under that camera's own asyncio lock, so unrelated cameras never
block each other and a camera can never end up with two publishers.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from app.api.errors import (
    ApiError,
    BadRequest,
    CameraNotFound,
    DuplicateStreamPath,
    OperationInProgress,
    SourceRejected,
)
from app.config import Settings
from app.domain.models import (
    TRANSITIONAL_STATUSES,
    CameraCreate,
    CameraRecord,
    CameraStatus,
    CameraUpdate,
    CameraView,
    RuntimeInfo,
    SourceKind,
    SourceMetadata,
    VideoConfig,
    new_camera_id,
    slugify_stream_path,
    utcnow,
)
from app.persistence.camera_repository import CameraRepository
from app.services.ffmpeg_command import build_publish_command
from app.services.probe import ProbeError, ProbeService
from app.services.process_manager import (
    FFmpegProcessManager,
    ProcessError,
    ProcessExit,
    format_stderr_tail,
)
from app.services.source_storage import SourceError, SourceStorage

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ResolvedSource:
    """A validated source file ready to be attached to a camera."""

    def __init__(
        self,
        kind: SourceKind,
        path: Path,
        original_filename: str,
        stored_filename: str | None,
        metadata: SourceMetadata,
    ) -> None:
        self.kind = kind
        self.path = path
        self.original_filename = original_filename
        self.stored_filename = stored_filename
        self.metadata = metadata


class CameraManager:
    def __init__(
        self,
        settings: Settings,
        repository: CameraRepository,
        storage: SourceStorage,
        prober: ProbeService,
        processes: FFmpegProcessManager,
    ) -> None:
        self._settings = settings
        self._repo = repository
        self._storage = storage
        self._prober = prober
        self._processes = processes
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._processes.set_exit_callback(self._on_process_exit)

    # --- application lifecycle -----------------------------------------

    async def startup(self) -> None:
        self._settings.ensure_directories()
        self._storage.ensure_directories()
        await self._db(self._repo.initialize)
        records = await self._db(self._repo.list_cameras)

        auto_start_ids: list[str] = []
        for record in records:
            if record.status in TRANSITIONAL_STATUSES or record.status is CameraStatus.RUNNING:
                # No process survived the restart: the persisted state is stale.
                await self._set_status(record, CameraStatus.STOPPED, last_error=None)
            if record.auto_start:
                auto_start_ids.append(record.id)

        logger.info(
            "startup_reconciled cameras=%d auto_start=%d", len(records), len(auto_start_ids)
        )
        if auto_start_ids:
            await asyncio.gather(
                *(self._auto_start(camera_id) for camera_id in auto_start_ids),
                return_exceptions=True,
            )

    async def shutdown(self) -> None:
        await self._processes.stop_all()

    async def _auto_start(self, camera_id: str) -> None:
        try:
            await self.start_camera(camera_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("auto_start_failed camera_id=%s error=%s", camera_id, exc)

    # --- reads ----------------------------------------------------------

    async def list_cameras(self) -> list[CameraView]:
        records = await self._db(self._repo.list_cameras)
        return [self.to_view(record) for record in records]

    async def get_camera(self, camera_id: str) -> CameraView:
        return self.to_view(await self._require(camera_id))

    def to_view(self, record: CameraRecord) -> CameraView:
        managed = self._processes.get(record.id)
        runtime = RuntimeInfo()
        if managed is not None and managed.process.returncode is None:
            runtime = RuntimeInfo(pid=managed.pid, started_at=managed.started_at)
        return CameraView(
            id=record.id,
            name=record.name,
            stream_path=record.stream_path,
            rtsp_url=self._settings.public_rtsp_url(record.stream_path),
            auto_start=record.auto_start,
            loop=record.loop,
            status=record.status,
            video=record.video,
            source=record.source_metadata,
            source_kind=record.source_kind,
            runtime=runtime,
            last_error=record.last_error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    # --- create ---------------------------------------------------------

    async def create_camera(self, payload: CameraCreate, upload: Any | None) -> CameraView:
        stream_path = await self._resolve_stream_path(payload, upload=None)
        source = await self._resolve_source(
            kind_spec=payload.source, upload=upload, required=True
        )

        camera_id = new_camera_id()
        source_path, stored_filename = self._finalize_source(source, camera_id)
        now = utcnow()
        record = CameraRecord(
            id=camera_id,
            name=payload.name,
            stream_path=stream_path,
            source_kind=source.kind,
            source_path=str(source_path),
            source_original_filename=source.original_filename,
            source_stored_filename=stored_filename,
            source_metadata=source.metadata,
            video=payload.video,
            loop=payload.loop,
            auto_start=payload.auto_start,
            status=CameraStatus.CREATED,
            last_error=None,
            created_at=now,
            updated_at=now,
        )

        try:
            await self._db(self._repo.insert, record)
        except BaseException:
            if stored_filename:
                self._storage.delete_stored(stored_filename)
            raise

        logger.info(
            "camera_created camera_id=%s stream_path=%s source_kind=%s",
            record.id,
            record.stream_path,
            record.source_kind.value,
        )

        if record.auto_start:
            async with await self._lock_for(record.id):
                record = await self._start_locked(record)
        else:
            record = await self._set_status(record, CameraStatus.STOPPED, last_error=None)
        return self.to_view(record)

    # --- update ---------------------------------------------------------

    async def update_camera(
        self, camera_id: str, payload: CameraUpdate, upload: Any | None
    ) -> CameraView:
        lock = await self._lock_for(camera_id)
        if lock.locked():
            record = await self._require(camera_id)
            raise OperationInProgress(camera_id, record.status.value)

        async with lock:
            record = await self._require(camera_id)
            if record.status in TRANSITIONAL_STATUSES:
                raise OperationInProgress(camera_id, record.status.value)

            updated = record.model_copy(deep=True)

            if payload.name is not None:
                updated.name = payload.name
            if payload.auto_start is not None:
                updated.auto_start = payload.auto_start
            if payload.loop is not None:
                updated.loop = payload.loop
            if payload.stream_path is not None and payload.stream_path != record.stream_path:
                if await self._db(
                    self._repo.stream_path_exists, payload.stream_path, camera_id
                ):
                    raise DuplicateStreamPath(payload.stream_path)
                updated.stream_path = payload.stream_path
            if payload.video is not None:
                updated.video = _merge_video(record.video, payload.video)

            new_source: ResolvedSource | None = None
            if payload.source is not None or upload is not None:
                new_source = await self._resolve_source(
                    kind_spec=payload.source, upload=upload, required=True
                )

            previous_stored_filename = record.source_stored_filename
            new_stored_filename: str | None = None
            if new_source is not None:
                source_path, new_stored_filename = self._finalize_source(new_source, camera_id)
                updated.source_kind = new_source.kind
                updated.source_path = str(source_path)
                updated.source_original_filename = new_source.original_filename
                updated.source_stored_filename = new_stored_filename
                updated.source_metadata = new_source.metadata

            was_running = self._processes.is_running(camera_id)
            restart_needed = (
                was_running
                and updated.runtime_config_signature() != record.runtime_config_signature()
            )
            updated.updated_at = utcnow()

            try:
                await self._db(self._repo.update, updated)
            except BaseException:
                if new_stored_filename:
                    self._storage.delete_stored(new_stored_filename)
                raise

            # The old upload is only removed once the new one is committed.
            if new_source is not None and previous_stored_filename:
                self._storage.delete_stored(previous_stored_filename)

            logger.info(
                "camera_updated camera_id=%s stream_path=%s restart=%s",
                updated.id,
                updated.stream_path,
                restart_needed,
            )

            if restart_needed:
                updated = await self._restart_locked(updated)
            return self.to_view(updated)

    # --- delete ---------------------------------------------------------

    async def delete_camera(self, camera_id: str) -> None:
        lock = await self._lock_for(camera_id)
        async with lock:
            record = await self._require(camera_id)
            await self._stop_locked(record, persist_status=False)
            await self._db(self._repo.delete, camera_id)
            if record.owns_uploaded_file():
                self._storage.delete_stored(record.source_stored_filename)
            logger.info(
                "camera_deleted camera_id=%s stream_path=%s", record.id, record.stream_path
            )
        async with self._locks_guard:
            self._locks.pop(camera_id, None)

    # --- lifecycle ------------------------------------------------------

    async def start_camera(self, camera_id: str) -> CameraView:
        lock = await self._lock_for(camera_id)
        if lock.locked():
            record = await self._require(camera_id)
            raise OperationInProgress(camera_id, record.status.value)
        async with lock:
            record = await self._require(camera_id)
            if record.status in TRANSITIONAL_STATUSES:
                raise OperationInProgress(camera_id, record.status.value)
            return self.to_view(await self._start_locked(record))

    async def stop_camera(self, camera_id: str) -> CameraView:
        lock = await self._lock_for(camera_id)
        if lock.locked():
            record = await self._require(camera_id)
            raise OperationInProgress(camera_id, record.status.value)
        async with lock:
            record = await self._require(camera_id)
            if record.status is CameraStatus.STOPPING:
                raise OperationInProgress(camera_id, record.status.value)
            return self.to_view(await self._stop_locked(record))

    async def restart_camera(self, camera_id: str) -> CameraView:
        lock = await self._lock_for(camera_id)
        if lock.locked():
            record = await self._require(camera_id)
            raise OperationInProgress(camera_id, record.status.value)
        async with lock:
            record = await self._require(camera_id)
            if record.status in TRANSITIONAL_STATUSES:
                raise OperationInProgress(camera_id, record.status.value)
            logger.info("camera_restart camera_id=%s", camera_id)
            return self.to_view(await self._restart_locked(record))

    # --- lifecycle internals (caller holds the per-camera lock) ---------

    async def _start_locked(self, record: CameraRecord) -> CameraRecord:
        if self._processes.is_running(record.id):
            # Idempotent: a publisher is already up for this camera.
            if record.status is not CameraStatus.RUNNING:
                record = await self._set_status(record, CameraStatus.RUNNING, last_error=None)
            return record

        if not Path(record.source_path).is_file():
            message = (
                f"Source video '{record.source_original_filename}' is no longer available."
            )
            logger.warning("camera_source_missing camera_id=%s", record.id)
            return await self._set_status(record, CameraStatus.ERROR, last_error=message)

        logger.info(
            "camera_starting camera_id=%s stream_path=%s", record.id, record.stream_path
        )
        record = await self._set_status(record, CameraStatus.STARTING, last_error=None)

        command = build_publish_command(
            record,
            self._settings.internal_rtsp_url(record.stream_path),
            self._settings.ffmpeg_binary,
        )
        logger.debug("ffmpeg_command camera_id=%s argv=%s", record.id, command)

        try:
            managed = await self._processes.start(record.id, command)
        except ProcessError as exc:
            message = self._sanitize(str(exc), record)
            logger.warning("ffmpeg_error camera_id=%s error=%s", record.id, message)
            return await self._set_status(record, CameraStatus.ERROR, last_error=message)

        logger.info(
            "camera_started camera_id=%s pid=%s rtsp=%s",
            record.id,
            managed.pid,
            self._settings.public_rtsp_url(record.stream_path),
        )
        return await self._set_status(record, CameraStatus.RUNNING, last_error=None)

    async def _stop_locked(
        self, record: CameraRecord, persist_status: bool = True
    ) -> CameraRecord:
        if not self._processes.is_running(record.id):
            if persist_status and record.status is not CameraStatus.STOPPED:
                record = await self._set_status(record, CameraStatus.STOPPED, last_error=None)
            return record

        logger.info("camera_stopping camera_id=%s", record.id)
        if persist_status:
            record = await self._set_status(record, CameraStatus.STOPPING, last_error=None)
        await self._processes.stop(record.id)
        if persist_status:
            record = await self._set_status(record, CameraStatus.STOPPED, last_error=None)
            logger.info("camera_stopped camera_id=%s", record.id)
        return record

    async def _restart_locked(self, record: CameraRecord) -> CameraRecord:
        record = await self._stop_locked(record)
        return await self._start_locked(record)

    # --- process exit handling ------------------------------------------

    async def _on_process_exit(self, event: ProcessExit) -> None:
        if event.requested:
            # A stop/restart/delete we initiated; that code path owns the status.
            return

        lock = await self._lock_for(event.camera_id)
        async with lock:
            record = await self._db(self._repo.get, event.camera_id)
            if record is None:
                return
            if self._processes.is_running(event.camera_id):
                # A newer publisher already took over; leave its status alone.
                return

            tail = format_stderr_tail(event.stderr_tail)
            if not record.loop and event.returncode == 0:
                logger.info("camera_stopped camera_id=%s reason=end_of_stream", record.id)
                await self._set_status(record, CameraStatus.STOPPED, last_error=None)
                return

            message = tail or f"FFmpeg exited unexpectedly with code {event.returncode}."
            logger.warning(
                "ffmpeg_exit_unexpected camera_id=%s returncode=%s error=%s",
                record.id,
                event.returncode,
                message,
            )
            await self._set_status(
                record, CameraStatus.ERROR, last_error=self._sanitize(message, record)
            )

    # --- helpers --------------------------------------------------------

    async def _require(self, camera_id: str) -> CameraRecord:
        record = await self._db(self._repo.get, camera_id)
        if record is None:
            raise CameraNotFound(camera_id)
        return record

    async def _set_status(
        self, record: CameraRecord, status: CameraStatus, last_error: str | None
    ) -> CameraRecord:
        now = utcnow()
        await self._db(self._repo.update_status, record.id, status, last_error, now)
        record.status = status
        record.last_error = last_error
        record.updated_at = now
        return record

    async def _lock_for(self, camera_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(camera_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[camera_id] = lock
            return lock

    async def _resolve_stream_path(self, payload: CameraCreate, upload: Any | None) -> str:
        if payload.stream_path:
            if await self._db(self._repo.stream_path_exists, payload.stream_path, None):
                raise DuplicateStreamPath(payload.stream_path)
            return payload.stream_path

        base = slugify_stream_path(payload.name)
        candidate = base
        suffix = 2
        while await self._db(self._repo.stream_path_exists, candidate, None):
            candidate = f"{base}-{suffix}"[:64].strip("-")
            suffix += 1
        return candidate

    async def _resolve_source(
        self, kind_spec: Any, upload: Any | None, required: bool
    ) -> ResolvedSource:
        """Validate exactly one source and probe it."""
        has_upload = upload is not None
        wants_local = kind_spec is not None and kind_spec.kind is SourceKind.LOCAL

        if has_upload and wants_local:
            raise BadRequest(
                "Provide either an uploaded file or a mounted source path, not both."
            )
        if not has_upload and not wants_local:
            if not required:
                raise BadRequest("No source provided.")
            raise BadRequest(
                "A source video is required: upload a file, or select a file from the "
                "mounted source directory."
            )

        if wants_local:
            try:
                path = self._storage.resolve_local(kind_spec.local_path or "")
            except SourceError as exc:
                raise SourceRejected(str(exc)) from exc
            metadata = await self._probe(path, path.name)
            return ResolvedSource(SourceKind.LOCAL, path, path.name, None, metadata)

        try:
            staged, original_filename = await self._storage.stage_upload(upload)
        except SourceError as exc:
            raise SourceRejected(str(exc)) from exc

        try:
            metadata = await self._probe(staged, original_filename)
        except BaseException:
            self._storage.discard(staged)
            raise
        return ResolvedSource(SourceKind.UPLOAD, staged, original_filename, None, metadata)

    async def _probe(self, path: Path, filename: str) -> SourceMetadata:
        try:
            return await self._prober.probe(path, filename)
        except ProbeError as exc:
            raise SourceRejected(str(exc), {"filename": filename}) from exc

    def _finalize_source(
        self, source: ResolvedSource, camera_id: str
    ) -> tuple[Path, str | None]:
        """Move a staged upload into permanent storage; local files stay put."""
        if source.kind is SourceKind.LOCAL:
            return source.path, None
        path, stored_filename = self._storage.promote(
            source.path, camera_id, source.original_filename
        )
        return path, stored_filename

    def _sanitize(self, message: str, record: CameraRecord) -> str:
        """Keep internal filesystem paths out of user-facing error strings."""
        cleaned = message.replace(record.source_path, record.source_original_filename)
        cleaned = cleaned.replace(str(self._settings.data_dir), "<data>")
        return cleaned[:1000]

    async def _db(self, func: Callable[..., T], *args: Any) -> T:
        """Run a blocking SQLite call on a worker thread."""
        return await asyncio.to_thread(func, *args)

    # --- health ---------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        ffmpeg_path = shutil.which(self._settings.ffmpeg_binary)
        ffprobe_path = shutil.which(self._settings.ffprobe_binary)
        database_ok = True
        camera_count = 0
        try:
            camera_count = len(await self._db(self._repo.list_cameras))
        except Exception:  # pragma: no cover - defensive
            database_ok = False
        healthy = bool(ffmpeg_path) and bool(ffprobe_path) and database_ok
        return {
            "status": "ok" if healthy else "degraded",
            "ffmpeg": bool(ffmpeg_path),
            "ffprobe": bool(ffprobe_path),
            "database": database_ok,
            "cameras": camera_count,
            "running_publishers": len(self._processes.running_camera_ids()),
        }


def _merge_video(current: VideoConfig, update: Any) -> VideoConfig:
    merged = current.model_dump()
    for field in ("codec", "resolution", "fps", "bitrate"):
        value = getattr(update, field, None)
        if value is not None:
            merged[field] = value
    return VideoConfig.model_validate(merged)


__all__ = ["CameraManager", "ApiError"]
