"""Shared fixtures: temporary storage, stub FFmpeg binaries, wired manager."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.domain.models import SourceMetadata
from app.main import create_app
from app.persistence.camera_repository import CameraRepository
from app.services.camera_manager import CameraManager
from app.services.probe import ProbeService
from app.services.process_manager import FFmpegProcessManager
from app.services.source_storage import SourceStorage

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
FFPROBE_AVAILABLE = shutil.which("ffprobe") is not None

requires_ffmpeg = pytest.mark.skipif(
    not (FFMPEG_AVAILABLE and FFPROBE_AVAILABLE),
    reason="ffmpeg/ffprobe are not installed",
)


def _write_script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def stub_bin(tmp_path: Path) -> dict[str, Path]:
    """Fake ffmpeg binaries with predictable behaviour."""
    directory = tmp_path / "bin"
    directory.mkdir()
    return {
        # Runs until terminated, like a healthy publisher.
        "long_running": _write_script(
            directory / "ffmpeg-ok", "#!/bin/sh\nexec sleep 300\n"
        ),
        # Fails immediately, like a publisher that cannot reach MediaMTX.
        "failing": _write_script(
            directory / "ffmpeg-fail",
            "#!/bin/sh\n"
            "echo 'Connection to tcp://mediamtx:8554 failed: Connection refused' >&2\n"
            "exit 1\n",
        ),
        # Survives startup, then ends cleanly, like a non-looping video ending.
        "short_success": _write_script(
            directory / "ffmpeg-eos", "#!/bin/sh\nsleep 0.8\nexit 0\n"
        ),
        # Survives startup, then dies unexpectedly.
        "short_failure": _write_script(
            directory / "ffmpeg-crash",
            "#!/bin/sh\nsleep 0.8\necho 'broken pipe' >&2\nexit 3\n",
        ),
        # Ignores SIGTERM, forcing a kill.
        "stubborn": _write_script(
            directory / "ffmpeg-stubborn",
            "#!/bin/sh\ntrap '' TERM\nwhile true; do sleep 0.2; done\n",
        ),
    }


@pytest.fixture
def settings(tmp_path: Path, stub_bin: dict[str, Path]) -> Settings:
    data_dir = tmp_path / "data"
    local_dir = data_dir / "local-sources"
    local_dir.mkdir(parents=True)
    return Settings(
        mediamtx_host="mediamtx",
        mediamtx_rtsp_port=8554,
        public_rtsp_host="localhost",
        public_rtsp_port=8554,
        rtsp_path_prefix="simulator",
        data_dir=data_dir,
        database_path=data_dir / "simulator.db",
        upload_video_dir=data_dir / "videos",
        source_video_dir=local_dir,
        max_upload_bytes=5 * 1024 * 1024,
        ffmpeg_binary=str(stub_bin["long_running"]),
        ffmpeg_startup_grace_seconds=0.3,
        ffmpeg_stop_timeout_seconds=1.0,
        ffmpeg_stderr_tail_lines=10,
        log_level="WARNING",
    )


@pytest.fixture
def repository(settings: Settings) -> CameraRepository:
    settings.ensure_directories()
    repo = CameraRepository(settings.database_path)
    repo.initialize()
    return repo


@pytest.fixture
def storage(settings: Settings) -> SourceStorage:
    store = SourceStorage(
        upload_dir=settings.upload_video_dir,
        local_root=settings.source_video_dir,
        temp_dir=settings.data_dir / "tmp",
        max_upload_bytes=settings.max_upload_bytes,
    )
    store.ensure_directories()
    return store


class FakeProbeService(ProbeService):
    """Deterministic ffprobe replacement.

    Files whose name contains ``bad`` are rejected, everything else returns
    plausible 1080p metadata.
    """

    def __init__(self) -> None:
        super().__init__("ffprobe", 5.0)
        self.calls: list[str] = []

    async def probe(self, source_path: Path, display_filename: str) -> SourceMetadata:
        from app.services.probe import ProbeError

        self.calls.append(str(source_path))
        if "bad" in display_filename.lower():
            raise ProbeError("The file contains no usable video stream.")
        return SourceMetadata(
            filename=display_filename,
            format="mov,mp4,m4a,3gp,3g2,mj2",
            duration_seconds=12.5,
            video_codec="h264",
            width=1920,
            height=1080,
            fps=25.0,
            bitrate_kbps=2400,
            has_audio=True,
            size_bytes=source_path.stat().st_size if source_path.exists() else 1024,
        )


@pytest.fixture
def prober() -> FakeProbeService:
    return FakeProbeService()


@pytest.fixture
def processes(settings: Settings) -> FFmpegProcessManager:
    return FFmpegProcessManager(
        startup_grace_seconds=settings.ffmpeg_startup_grace_seconds,
        stop_timeout_seconds=settings.ffmpeg_stop_timeout_seconds,
        stderr_tail_lines=settings.ffmpeg_stderr_tail_lines,
    )


@pytest.fixture
def manager(
    settings: Settings,
    repository: CameraRepository,
    storage: SourceStorage,
    prober: FakeProbeService,
    processes: FFmpegProcessManager,
) -> CameraManager:
    return CameraManager(settings, repository, storage, prober, processes)


@pytest.fixture
def client(settings: Settings, manager: CameraManager, storage: SourceStorage):
    from fastapi.testclient import TestClient

    app = create_app(settings=settings, manager=manager, storage=storage)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_upload(tmp_path: Path) -> tuple[str, bytes]:
    """A small fake 'video' payload (content is irrelevant to the fake prober)."""
    return "parking.mp4", b"\x00\x00\x00\x18ftypmp42" + os.urandom(2048)


def make_real_video(path: Path, seconds: float = 1.0, size: str = "320x240") -> Path:
    """Generate a genuine tiny H.264 file with FFmpeg (used by ffprobe tests)."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path
