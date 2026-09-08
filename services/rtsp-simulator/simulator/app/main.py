"""FastAPI application: REST API plus the single-page UI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.cameras import router as cameras_router
from app.api.errors import register_exception_handlers
from app.config import Settings, get_settings
from app.logging_config import configure_logging
from app.persistence.camera_repository import CameraRepository
from app.services.camera_manager import CameraManager
from app.services.probe import ProbeService
from app.services.process_manager import FFmpegProcessManager
from app.services.source_storage import SourceStorage

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def build_manager(settings: Settings) -> tuple[CameraManager, SourceStorage]:
    repository = CameraRepository(settings.database_path)
    storage = SourceStorage(
        upload_dir=settings.upload_video_dir,
        local_root=settings.source_video_dir,
        temp_dir=settings.data_dir / "tmp",
        max_upload_bytes=settings.max_upload_bytes,
    )
    prober = ProbeService(settings.ffprobe_binary, settings.ffprobe_timeout_seconds)
    processes = FFmpegProcessManager(
        startup_grace_seconds=settings.ffmpeg_startup_grace_seconds,
        stop_timeout_seconds=settings.ffmpeg_stop_timeout_seconds,
        stderr_tail_lines=settings.ffmpeg_stderr_tail_lines,
    )
    return CameraManager(settings, repository, storage, prober, processes), storage


def create_app(
    settings: Settings | None = None,
    manager: CameraManager | None = None,
    storage: SourceStorage | None = None,
) -> FastAPI:
    """Build the app. Tests may inject a pre-wired manager/storage pair."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    if manager is None or storage is None:
        manager, storage = build_manager(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "simulator_starting publish_target=%s public_url_template=%s",
            settings.internal_rtsp_url("<stream_path>"),
            settings.public_rtsp_url("<stream_path>"),
        )
        await manager.startup()
        try:
            yield
        finally:
            logger.info("simulator_stopping")
            await manager.shutdown()

    app = FastAPI(
        title="RTSP Camera Simulator",
        version="1.0.0",
        description=(
            "Turn uploaded video files into virtual RTSP cameras published "
            "through MediaMTX."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.camera_manager = manager
    app.state.source_storage = storage

    @app.middleware("http")
    async def prevent_ui_asset_caching(request: Request, call_next):
        """Serve the UI itself uncached.

        index.html, app.js and styles.css are referenced by stable URLs, so a
        browser that cached a previous build would keep using it after a
        rebuild. The API is untouched.
        """
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    register_exception_handlers(app)
    app.include_router(cameras_router, prefix="/api", tags=["cameras"])

    @app.get("/health", tags=["system"])
    async def health() -> JSONResponse:
        report = await manager.health()
        status_code = 200 if report["status"] == "ok" else 503
        return JSONResponse(report, status_code=status_code)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
