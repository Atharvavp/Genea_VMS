"""FastAPI application: REST API plus the single-page live-view UI.

No media passes through this process. It configures MediaMTX and reports what
MediaMTX says; live video goes RTSP source -> MediaMTX -> WebRTC -> browser, and
recorded video goes MediaMTX's recording store -> its playback server ->
browser. The VMS builds the URLs for both and serves neither.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.cameras import router as cameras_router
from app.api.errors import register_exception_handlers
from app.api.recordings import router as recordings_router
from app.config import Settings, get_settings
from app.logging_config import configure_logging
from app.persistence.camera_repository import CameraRepository
from app.services.camera_manager import CameraManager
from app.services.mediamtx_client import MediaMTXClient, MediaMTXPlaybackClient
from app.services.recording_manager import RecordingManager

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def build_manager(settings: Settings) -> CameraManager:
    repository = CameraRepository(settings.database_path)
    client = MediaMTXClient(settings.mediamtx_api_url, settings.mediamtx_timeout_seconds)
    return CameraManager(settings, repository, client)


def build_recording_manager(settings: Settings) -> RecordingManager:
    """Recordings read the same desired state, through a separate client.

    The playback server is a different port and a different failure domain from
    the Control API, so it gets its own client: a playback outage must not be
    able to look like a control-plane outage.
    """
    repository = CameraRepository(settings.database_path)
    playback = MediaMTXPlaybackClient(
        settings.mediamtx_playback_url, settings.mediamtx_timeout_seconds
    )
    return RecordingManager(settings, repository, playback)


def create_app(
    settings: Settings | None = None,
    manager: CameraManager | None = None,
    recording_manager: RecordingManager | None = None,
) -> FastAPI:
    """Build the app. Tests may inject pre-wired managers."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    manager = manager or build_manager(settings)
    recording_manager = recording_manager or build_recording_manager(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "vms_starting mediamtx_api=%s webrtc_base=%s",
            settings.mediamtx_api_url,
            settings.public_webrtc_base_url,
        )
        await manager.startup()
        try:
            yield
        finally:
            await manager.shutdown()
            await recording_manager.aclose()

    app = FastAPI(
        title="Genea VMS - Live View and Recording",
        version="1.1.0",
        description=(
            "Register generic RTSP cameras, have MediaMTX ingest them, watch "
            "them in the browser over WebRTC, record them continuously, and "
            "play the recorded history back by date."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.camera_manager = manager
    app.state.recording_manager = recording_manager

    @app.middleware("http")
    async def prevent_ui_asset_caching(request: Request, call_next):
        """Serve the UI itself uncached.

        index.html, app.js and styles.css sit at stable URLs, so a browser that
        cached a previous build would keep using it after a rebuild. The API is
        untouched.
        """
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    register_exception_handlers(app)
    app.include_router(cameras_router, prefix="/api", tags=["cameras"])
    app.include_router(recordings_router, prefix="/api", tags=["recordings"])

    @app.get("/health", tags=["system"])
    async def health() -> JSONResponse:
        report = await manager.health_report()
        return JSONResponse(report, status_code=200 if report["status"] == "ok" else 503)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
