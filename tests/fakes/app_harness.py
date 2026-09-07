"""A real FastAPI app wired to fakes, for API-tier tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import build_test_settings
from app.main import create_app
from app.persistence.camera_repository import CameraRepository
from app.persistence.database import Database
from app.persistence.event_repository import EventRepository
from app.persistence.event_storage import EventStorage
from app.persistence.line_repository import LineRepository
from app.services.vms_recordings import VMSRecordingClient
from tests.fakes.detector import FakeDetector
from tests.fakes import worker as worker_fakes


@dataclass
class Harness:
    app: Any
    settings: Any
    database: Database
    cameras: CameraRepository
    lines: LineRepository
    events: EventRepository
    storage: EventStorage
    detector: FakeDetector


def build_harness(
    tmp_path: Path,
    *,
    detector_ready: bool = True,
    recording_handler=None,
) -> Harness:
    data_root = tmp_path / "data"
    events_root = data_root / "events"
    events_root.mkdir(parents=True)
    settings = build_test_settings(
        analytics_data_dir=data_root,
        analytics_db_path=data_root / "analytics.db",
        analytics_event_dir=events_root,
        vms_api_base_url="http://vms.invalid:8090",
    )
    database = Database(settings.analytics_db_path)
    database.initialize()
    repositories = (
        CameraRepository(database),
        LineRepository(database),
        EventRepository(database),
    )
    storage = EventStorage(
        data_root=data_root, events_root=events_root, repository=repositories[2]
    )
    detector = FakeDetector(ready=detector_ready)

    app = create_app(settings, detector)

    original_lifespan = app.router.lifespan_context

    def _patch(application) -> None:
        application.state.manager._worker_factory = worker_fakes.factory()  # noqa: SLF001
        if recording_handler is not None:
            transport = httpx.MockTransport(recording_handler)
            client = httpx.AsyncClient(transport=transport)
            application.state.http_client = client
            application.state.event_service._recordings = VMSRecordingClient(  # noqa: SLF001
                base_url=settings.vms_api_base_url, client=client
            )

    class _Wrapped:
        def __init__(self, inner):
            self._inner = inner

        def __call__(self, application):
            context = self._inner(application)

            class _Ctx:
                async def __aenter__(_self):  # noqa: N805
                    value = await context.__aenter__()
                    _patch(application)
                    return value

                async def __aexit__(_self, *exc):  # noqa: N805
                    return await context.__aexit__(*exc)

            return _Ctx()

    app.router.lifespan_context = _Wrapped(original_lifespan)

    return Harness(
        app=app,
        settings=settings,
        database=database,
        cameras=repositories[0],
        lines=repositories[1],
        events=repositories[2],
        storage=storage,
        detector=detector,
    )
