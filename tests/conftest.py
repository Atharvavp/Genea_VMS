"""Shared fixtures: a fake MediaMTX, a wired manager, and a test app.

The fake stands in for the Control API only. It models the two things the VMS
actually depends on - a configured path table, and whether each path's source is
currently readable - and is deliberately faithful to the behaviour verified
against `bluenviron/mediamtx:1.20.1` (see tests/test_integration_mediamtx.py):
`replace` upserts, `delete` of a missing path 404s, `available` is false until
the source connects.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.main import create_app
from app.persistence.camera_repository import CameraRepository
from app.services.camera_manager import CameraManager
from app.services.mediamtx_client import (
    MediaMTXInfo,
    MediaMTXUnavailable,
    PathNotFound,
    PathRejected,
    PathRuntime,
)

SIMULATOR_URL = "rtsp://host.docker.internal:8554/simulator/lobby"
CREDENTIALED_URL = "rtsp://admin:hunter2@10.0.0.9:554/Streaming/Channels/101"


class FakeMediaMTX:
    """In-memory stand-in for MediaMTXClient."""

    def __init__(self) -> None:
        self.paths: dict[str, str] = {}  # path name -> source URL
        self.reachable_sources: set[str] = set()  # sources that connect
        self.rejected_sources: set[str] = set()  # sources MediaMTX refuses
        self.up = True
        self.calls: list[tuple[str, str]] = []
        self.closed = False
        self.version = "v1.20.1"

    # --- test helpers ---------------------------------------------------

    def go_down(self) -> None:
        self.up = False

    def come_back(self, *, wipe_paths: bool = False) -> None:
        """MediaMTX answers again. `wipe_paths` models a container restart."""
        self.up = True
        if wipe_paths:
            self.paths.clear()

    def calls_for(self, kind: str) -> list[str]:
        return [name for call_kind, name in self.calls if call_kind == kind]

    def _guard(self) -> None:
        if not self.up:
            raise MediaMTXUnavailable("MediaMTX Control API is unreachable: refused")

    def _runtime(self, name: str) -> PathRuntime:
        source = self.paths[name]
        available = source in self.reachable_sources
        return PathRuntime(
            name=name,
            available=available,
            ready=available,
            source_type="rtspSource",
            tracks=("H264",) if available else (),
        )

    # --- MediaMTXClient interface ---------------------------------------

    async def get_info(self) -> MediaMTXInfo:
        self._guard()
        return MediaMTXInfo(version=self.version, started="2026-01-01T00:00:00Z")

    async def list_paths(self) -> dict[str, PathRuntime]:
        self._guard()
        self.calls.append(("list_paths", ""))
        return {name: self._runtime(name) for name in self.paths}

    async def get_path(self, name: str) -> PathRuntime:
        self._guard()
        if name not in self.paths:
            raise PathNotFound(f"MediaMTX returned 404: path not found ({name})")
        return self._runtime(name)

    async def list_configured_path_names(self) -> list[str]:
        self._guard()
        self.calls.append(("list_configured", ""))
        return sorted(self.paths)

    async def ensure_path(self, name: str, rtsp_url: str) -> None:
        self._guard()
        if rtsp_url in self.rejected_sources:
            raise PathRejected(f"MediaMTX returned 400: invalid source: '{rtsp_url}'")
        self.calls.append(("ensure", name))
        self.paths[name] = rtsp_url

    async def delete_path(self, name: str) -> bool:
        self._guard()
        self.calls.append(("delete", name))
        return self.paths.pop(name, None) is not None

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "vms.db",
        mediamtx_api_url="http://vms-mediamtx:9997",
        public_webrtc_host="localhost",
        public_webrtc_port=8889,
        # The background loop is driven explicitly in tests: after its startup
        # pass it sleeps rather than racing the assertions.
        camera_health_poll_seconds=3600.0,
        reconcile_min_interval_seconds=0.0,
    )


@pytest.fixture
def fake_mediamtx() -> FakeMediaMTX:
    return FakeMediaMTX()


@pytest.fixture
def repository(settings) -> CameraRepository:
    return CameraRepository(settings.database_path)


@pytest.fixture
async def manager(settings, repository, fake_mediamtx) -> CameraManager:
    instance = CameraManager(settings, repository, fake_mediamtx)  # type: ignore[arg-type]
    await instance.initialize()
    return instance


@pytest.fixture
def app(settings, manager):
    return create_app(settings=settings, manager=manager)


@pytest.fixture
async def client(app):
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        # Lifespan is not run by ASGITransport, so the manager is started by the
        # `manager` fixture instead and the poll loop stays under test control.
        yield http
