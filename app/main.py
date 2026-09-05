"""FastAPI application: REST API plus the single-page live-view UI.

No media passes through this process. It configures MediaMTX and reports what
MediaMTX says; video goes RTSP source -> MediaMTX -> WebRTC -> browser.
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
from app.config import Settings, get_settings
from app.logging_config import configure_logging
from app.persistence.camera_repository import CameraRepository
from app.services.camera_manager import CameraManager
from app.services.mediamtx_client import MediaMTXClient

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def build_manager(settings: Settings) -> CameraManager:
    repository = CameraRepository(settings.database_path)
    client = MediaMTXClient(settings.mediamtx_api_url, settings.mediamtx_timeout_seconds)
    return CameraManager(settings, repository, client)


def create_app(
    settings: Settings | None = None,
    manager: CameraManager | None = None,
) -> FastAPI:
    """Build the app. Tests may inject a pre-wired manager."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    manager = manager or build_manager(settings)

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

    app = FastAPI(
        title="Genea VMS - Live View",
        version="1.0.0",
        description=(
            "Register generic RTSP cameras, have MediaMTX ingest them, and "
            "watch them in the browser over WebRTC."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.camera_manager = manager

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
