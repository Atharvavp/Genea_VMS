"""Shared fixtures: a fake MediaMTX, a wired manager, and a test app.

The fake stands in for the Control API and the playback server. It models what
the VMS actually depends on - a configured path table with its recording block,
whether each path's source is currently readable, and what history the playback
server can see - and is deliberately faithful to the behaviour verified against
`bluenviron/mediamtx:1.20.1` (see tests/test_integration_mediamtx.py):

* `replace` upserts, `delete` of a missing path 404s, `available` is false until
  the source connects;
* history is visible only while the path is configured, and only while its
  `recordPath` still matches the one the recordings were written under - the
  reason every replacement must carry the full recording block;
* an empty window is an empty list, an unconfigured path is an error.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    RecordingPathConfig,
    RecordingTimespan,
    RecordingUnavailable,
)
from app.services.recording_manager import RecordingManager

SIMULATOR_URL = "rtsp://host.docker.internal:8554/simulator/lobby"
CREDENTIALED_URL = "rtsp://admin:hunter2@10.0.0.9:554/Streaming/Channels/101"


@dataclass
class FakePathConfig:
    """One configured path, as MediaMTX would hold it."""

    source: str
    record: bool
    recording: RecordingPathConfig


class FakeMediaMTX:
    """In-memory stand-in for MediaMTXClient."""

    def __init__(self) -> None:
        self.playback_up = True
        self.configs: dict[str, FakePathConfig] = {}
        self.reachable_sources: set[str] = set()  # sources that connect
        self.rejected_sources: set[str] = set()  # sources MediaMTX refuses
        self.up = True
        self.calls: list[tuple[str, str]] = []
        self.closed = False
        self.version = "v1.20.1"
        # path name -> spans on disk, independent of whether the path still
        # exists: deleting a path hides history without destroying files.
        self.history: dict[str, list[RecordingTimespan]] = {}
        # The recordPath the stored history was written under, per path.
        self.history_record_path: dict[str, str] = {}

    @property
    def paths(self) -> dict[str, str]:
        """Path name -> source URL, the view most tests care about."""
        return {name: config.source for name, config in self.configs.items()}

    # --- test helpers ---------------------------------------------------

    def go_down(self) -> None:
        self.up = False

    def come_back(self, *, wipe_paths: bool = False) -> None:
        """MediaMTX answers again. `wipe_paths` models a container restart.

        A restart loses the dynamic path table but not the files on disk, so
        `history` survives while `configs` does not.
        """
        self.up = True
        if wipe_paths:
            self.configs.clear()

    def plant_path(self, name: str, source: str, *, record: bool = False) -> None:
        """Configure a path directly, as an earlier run or another actor would."""
        self.configs[name] = FakePathConfig(
            source=source,
            record=record,
            recording=RecordingPathConfig(
                record_path="/recordings/%path/%Y-%m-%d/%H-%M-%S-%f",
                segment_duration="5m",
                delete_after="24h",
            ),
        )

    def record_history(
        self, path: str, start: str, duration_seconds: float
    ) -> None:
        """Pretend MediaMTX finalised a span under this path's current recordPath."""
        self.history.setdefault(path, []).append(
            RecordingTimespan(start=start, duration_seconds=duration_seconds)
        )
        config = self.configs.get(path)
        if config is not None:
            self.history_record_path[path] = config.recording.record_path

    def recording_config_of(self, path: str) -> RecordingPathConfig | None:
        config = self.configs.get(path)
        return config.recording if config else None

    def records(self, path: str) -> bool:
        config = self.configs.get(path)
        return bool(config and config.record)

    def calls_for(self, kind: str) -> list[str]:
        return [name for call_kind, name in self.calls if call_kind == kind]

    def _guard(self) -> None:
        if not self.up:
            raise MediaMTXUnavailable("MediaMTX Control API is unreachable: refused")

    def _runtime(self, name: str) -> PathRuntime:
        source = self.configs[name].source
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
        return {name: self._runtime(name) for name in self.configs}

    async def get_path(self, name: str) -> PathRuntime:
        self._guard()
        if name not in self.configs:
            raise PathNotFound(f"MediaMTX returned 404: path not found ({name})")
        return self._runtime(name)

    async def list_configured_path_names(self) -> list[str]:
        self._guard()
        self.calls.append(("list_configured", ""))
        return sorted(self.configs)

    async def ensure_path(
        self,
        name: str,
        rtsp_url: str,
        *,
        record: bool,
        recording: RecordingPathConfig,
    ) -> None:
        self._guard()
        if rtsp_url in self.rejected_sources:
            raise PathRejected(f"MediaMTX returned 400: invalid source: '{rtsp_url}'")
        self.calls.append(("ensure", name))
        self.configs[name] = FakePathConfig(
            source=rtsp_url, record=record, recording=recording
        )

    async def path_config_matches(
        self,
        name: str,
        rtsp_url: str,
        *,
        record: bool,
        recording: RecordingPathConfig,
    ) -> bool:
        self._guard()
        self.calls.append(("config_matches", name))
        config = self.configs.get(name)
        return bool(
            config
            and config.source == rtsp_url
            and config.record is record
            and config.recording == recording
        )

    async def delete_path(self, name: str) -> bool:
        self._guard()
        self.calls.append(("delete", name))
        return self.configs.pop(name, None) is not None

    async def aclose(self) -> None:
        self.closed = True

    # --- playback server ------------------------------------------------

    async def list_timespans(
        self, path: str, start: str, end: str
    ) -> list[RecordingTimespan]:
        """`/list`, including the two ways it refuses to answer."""
        if not self.playback_up:
            raise RecordingUnavailable(
                "MediaMTX playback server is unreachable: connection refused"
            )
        self.calls.append(("list_timespans", path))
        config = self.configs.get(path)
        if config is None:
            # Verified: an unconfigured path is a 400, even with files on disk.
            raise RecordingUnavailable(
                f"MediaMTX playback server returned 400: "
                f"path '{path}' is not configured"
            )
        written_under = self.history_record_path.get(path)
        if written_under is not None and written_under != config.recording.record_path:
            # A partial replacement moved recordPath: the history is still on
            # disk but the playback server cannot see it any more.
            return []
        spans = [
            span
            for span in self.history.get(path, [])
            if start <= span.start < end
        ]
        return sorted(spans, key=lambda span: span.start)


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
def recording_manager(settings, repository, fake_mediamtx) -> RecordingManager:
    # The same fake plays both roles, so a test can take a camera's path away
    # and see the history disappear with it.
    return RecordingManager(settings, repository, fake_mediamtx)  # type: ignore[arg-type]


@pytest.fixture
def app(settings, manager, recording_manager):
    return create_app(
        settings=settings, manager=manager, recording_manager=recording_manager
    )


@pytest.fixture
async def client(app):
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        # Lifespan is not run by ASGITransport, so the manager is started by the
        # `manager` fixture instead and the poll loop stays under test control.
        yield http
