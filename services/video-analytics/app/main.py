"""Application factory, lifespan ordering, dependency wiring, and /health.

No domain logic lives here. The lifespan performs the exact ordered startup in
PLAN section 13.2 and the concurrent bounded shutdown in section 13.13.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import cameras as cameras_routes
from app.api import events as events_routes
from app.api import lines as lines_routes
from app.api.errors import REQUEST_ID_HEADER, install_error_handlers, resolve_request_id
from app.config import Settings
from app.domain.models import HealthResponse, WorkerCounts, new_request_id
from app.logging_config import configure_logging
from app.persistence.camera_repository import CameraRepository
from app.persistence.database import Database
from app.persistence.event_repository import EventRepository
from app.persistence.event_storage import EventStorage
from app.persistence.line_repository import LineRepository
from app.services.camera_manager import AnalyticsCameraManager
from app.services.event_service import EventService
from app.services.vms_recordings import VMSRecordingClient, build_client

__all__ = ["create_app"]

logger = logging.getLogger("analytics.main")

STATIC_DIR = Path(__file__).parent / "static"

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' blob: data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

_HEALTH_PROBE_INTERVAL = 10.0


class _HealthProbe:
    """Cached event-storage writability probe, at most once per interval."""

    def __init__(self, directory: Path, interval: float = _HEALTH_PROBE_INTERVAL):
        self._directory = directory
        self._interval = interval
        self._checked_at = 0.0
        self._ok = False

    def check(self, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        if moment - self._checked_at < self._interval and self._checked_at:
            return self._ok
        self._checked_at = moment
        probe = self._directory / f"probe-{os.getpid()}"
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, b"ok")
                os.fsync(fd)
            finally:
                os.close(fd)
            probe.unlink(missing_ok=True)
            self._ok = True
        except OSError:
            self._ok = False
        return self._ok


def create_app(
    settings: Settings | None = None,
    detector: Any = None,
    *,
    start_workers: bool = True,
) -> FastAPI:
    resolved = settings or Settings()
    configure_logging(resolved.analytics_log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        # 1-2. Filesystem roots.
        database = Database(resolved.analytics_db_path)
        database.ensure_parent()

        # 3. Schema.
        schema_version = await asyncio.to_thread(database.initialize)
        logger.info("database_ready", extra={"schema_version": schema_version})

        camera_repository = CameraRepository(database)
        line_repository = LineRepository(database)
        event_repository = EventRepository(database)
        storage = EventStorage(
            data_root=resolved.analytics_data_dir,
            events_root=resolved.analytics_event_dir,
            repository=event_repository,
            jpeg_quality=resolved.analytics_jpeg_quality,
        )
        storage.ensure_roots()

        # 4. Reconcile artifacts before any worker can create an event.
        report = await asyncio.to_thread(storage.reconcile_startup)

        # 5-6. Detector; a failure degrades the service but keeps HTTP alive.
        resolved_detector = detector
        detector_ready = False
        if resolved_detector is None:
            from app.analytics.detector import YoloDetector

            resolved_detector = YoloDetector(
                model_path=resolved.analytics_model_path,
                expected_sha256=resolved.analytics_model_sha256,
                torch_threads=resolved.analytics_torch_threads,
            )
        try:
            loader = getattr(resolved_detector, "load", None)
            if callable(loader):
                await asyncio.to_thread(loader)
            detector_ready = bool(getattr(resolved_detector, "ready", True))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "detector_unavailable", extra={"error_type": type(exc).__name__}
            )
            detector_ready = False

        http_client = build_client()
        recordings = VMSRecordingClient(
            base_url=resolved.vms_api_base_url, client=http_client
        )

        manager = AnalyticsCameraManager(
            cameras=camera_repository,
            lines=line_repository,
            event_storage=storage,
            detector=resolved_detector,
            stall_seconds=resolved.analytics_stall_seconds,
            stop_timeout_seconds=resolved.analytics_stop_timeout_seconds,
        )
        manager.jpeg_quality = resolved.analytics_jpeg_quality  # type: ignore[attr-defined]

        app.state.settings = resolved
        app.state.database = database
        app.state.storage = storage
        app.state.detector = resolved_detector
        app.state.manager = manager
        app.state.http_client = http_client
        app.state.health_probe = _HealthProbe(resolved.health_probe_dir)
        app.state.event_service = EventService(
            repository=event_repository,
            storage=storage,
            recordings=recordings,
            cursor_signing_key=resolved.cursor_signing_key,
        )
        app.state.reconcile_report = report

        # 7. Start one worker per enabled camera.
        if start_workers:
            await manager.start_all_enabled(detector_ready=detector_ready)
        else:
            manager._detector_ready = detector_ready  # noqa: SLF001 - test entry point

        # 8. Ready.
        logger.info(
            "analytics_started",
            extra={
                "detector_ready": detector_ready,
                "temp_removed": report.temp_directories_removed,
                "orphans_removed": report.orphan_directories_removed,
            },
        )
        try:
            yield
        finally:
            await manager.shutdown_all()
            await http_client.aclose()
            release = getattr(resolved_detector, "release", None)
            if callable(release):
                release()
            logger.info("analytics_stopped")

    app = FastAPI(
        title="Genea Analytics",
        version="0.1.0",
        summary="Standalone video analytics and event detection (Component 4)",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        inbound = request.headers.get(REQUEST_ID_HEADER)
        request.state.request_id = (
            inbound
            if inbound and len(inbound) <= 64 and inbound.isascii()
            else new_request_id()
        )
        request_id = resolve_request_id(request)
        started = time.monotonic()
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        logger.info(
            "http_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "route": request.scope.get("route").path
                if request.scope.get("route")
                else request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )
        return response

    install_error_handlers(app)
    app.include_router(cameras_routes.router)
    app.include_router(lines_routes.router)
    app.include_router(events_routes.router)

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> Response:
        import asyncio

        database_ok = True
        try:
            await asyncio.to_thread(request.app.state.database.check_readable)
        except Exception:  # noqa: BLE001
            database_ok = False
        storage_ok = await asyncio.to_thread(request.app.state.health_probe.check)
        detector_ok = bool(getattr(request.app.state.detector, "ready", False))
        counts = request.app.state.manager.worker_counts()

        healthy = database_ok and storage_ok and detector_ok
        body = HealthResponse(
            status="ok" if healthy else "degraded",
            database="ok" if database_ok else "unavailable",
            event_storage="ok" if storage_ok else "unavailable",
            detector="ok" if detector_ok else "unavailable",
            workers=WorkerCounts(**counts),
        )
        return JSONResponse(
            status_code=200 if healthy else 503, content=body.model_dump()
        )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(
                STATIC_DIR / "index.html",
                media_type="text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )

    return app
