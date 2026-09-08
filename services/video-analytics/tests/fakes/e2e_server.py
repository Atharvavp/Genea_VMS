"""A deterministic in-process server for the browser suite.

This entry point exists only in the test image. It wires the real FastAPI
application to fake workers, a fake detector, and a scripted VMS recording
endpoint, then serves it on a loopback port for Playwright.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import httpx
import uvicorn

from app.analytics.types import LatestFrame
from app.config import build_test_settings
from app.domain.models import utc_now
from app.main import create_app
from app.persistence.camera_repository import CameraRepository
from app.persistence.database import Database
from app.persistence.event_repository import EventRepository
from app.persistence.event_storage import EventStorage
from app.persistence.line_repository import LineRepository
from app.services.vms_recordings import VMSRecordingClient
from tests.fakes import worker as worker_fakes
from tests.fakes.detector import FakeDetector


@dataclass
class RecordingScript:
    """Controls what the fake VMS returns, so every branch can be shown."""

    mode: str = "available"
    delay_seconds: float = 0.0
    calls: list[str] = field(default_factory=list)

    def handler(self) -> Callable[[httpx.Request], httpx.Response]:
        def _handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(str(request.url))
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            if self.mode == "unavailable":
                return httpx.Response(503, json={"error": {"code": "x"}})
            camera_id = request.url.params.get("camera_id", "cam_0123abcd")
            if self.mode == "not_found":
                items: list[dict[str, Any]] = []
            else:
                start = utc_now() - timedelta(minutes=5)
                items = [
                    {
                        "id": "rec_e2e",
                        "start_time": _iso(start),
                        "end_time": _iso(start + timedelta(hours=6)),
                        "duration_seconds": 21600.0,
                        "playback_url": "http://127.0.0.1:9996/get?path=vms_cam_0123abcd",
                        "source": "mediamtx",
                    }
                ]
            return httpx.Response(
                200,
                json={
                    "camera_id": camera_id,
                    "camera_name": "Fixture",
                    "date": request.url.params.get("date", "2026-09-07"),
                    "items": items,
                },
            )

        return _handler


def _iso(moment) -> str:
    from app.domain.models import to_iso_ms

    return to_iso_ms(moment)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class E2EServer:
    """Runs the real app on a loopback port in a background thread."""

    def __init__(self, data_root: Path):
        self.data_root = data_root
        self.events_root = data_root / "events"
        self.events_root.mkdir(parents=True, exist_ok=True)
        self.settings = build_test_settings(
            analytics_data_dir=data_root,
            analytics_db_path=data_root / "analytics.db",
            analytics_event_dir=self.events_root,
            vms_api_base_url="http://vms.invalid:8090",
        )
        self.database = Database(self.settings.analytics_db_path)
        self.database.initialize()
        self.cameras = CameraRepository(self.database)
        self.lines = LineRepository(self.database)
        self.events = EventRepository(self.database)
        self.storage = EventStorage(
            data_root=data_root,
            events_root=self.events_root,
            repository=self.events,
        )
        self.detector = FakeDetector()
        self.recordings = RecordingScript()
        self.port = _free_port()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.app = self._build_app()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _build_app(self):
        app = create_app(self.settings, self.detector)
        inner = app.router.lifespan_context
        script = self.recordings
        settings = self.settings

        class _Wrapped:
            def __call__(self, application):
                context = inner(application)

                class _Ctx:
                    async def __aenter__(_self):  # noqa: N805
                        value = await context.__aenter__()
                        application.state.manager._worker_factory = (  # noqa: SLF001
                            worker_fakes.factory()
                        )
                        client = httpx.AsyncClient(
                            transport=httpx.MockTransport(script.handler())
                        )
                        application.state.http_client = client
                        application.state.event_service._recordings = (  # noqa: SLF001
                            VMSRecordingClient(
                                base_url=settings.vms_api_base_url, client=client
                            )
                        )
                        return value

                    async def __aexit__(_self, *exc):  # noqa: N805
                        return await context.__aexit__(*exc)

                return _Ctx()

        app.router.lifespan_context = _Wrapped()
        return app

    def start(self, timeout: float = 30.0) -> None:
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{self.base_url}/health", timeout=2.0)
                if response.status_code in (200, 503):
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError("the end-to-end server never became ready")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(20.0)

    # -- scripted state -----------------------------------------------------

    def publish_frame(self, camera_id: str, *, colour: int = 90) -> None:
        """Give a fake worker a snapshot so the Configure view has an image."""
        import numpy as np

        for worker in worker_fakes.FakeWorker.created:
            if worker.camera_id != camera_id:
                continue
            frame = np.full((240, 320, 3), colour, dtype=np.uint8)
            frame[60:180, 100:220] = 220
            worker.latest_frame = LatestFrame(
                sequence=worker.latest_frame.sequence + 1 if worker.latest_frame else 1,
                rgb=frame,
                received_at_utc=utc_now(),
                width=320,
                height=240,
                age_seconds=0.1,
            )
            worker.become_running()

    def make_running(self, camera_id: str) -> None:
        for worker in worker_fakes.FakeWorker.created:
            if worker.camera_id == camera_id:
                worker.become_running()
