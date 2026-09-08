"""End-to-end test: file -> FFmpeg -> MediaMTX -> RTSP client.

Requires a reachable MediaMTX and a real FFmpeg. Run it from inside the
simulator container while the Compose stack is up:

    docker compose exec simulator pytest -m integration -v
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.domain.models import CameraCreate, CameraStatus
from app.main import build_manager
from tests.conftest import make_real_video, requires_ffmpeg

pytestmark = [pytest.mark.integration, requires_ffmpeg]

MEDIAMTX_HOST = os.environ.get("MEDIAMTX_HOST", "mediamtx")
MEDIAMTX_PORT = int(os.environ.get("MEDIAMTX_RTSP_PORT", "8554"))


def mediamtx_reachable() -> bool:
    try:
        with socket.create_connection((MEDIAMTX_HOST, MEDIAMTX_PORT), timeout=2):
            return True
    except OSError:
        return False


needs_mediamtx = pytest.mark.skipif(
    not mediamtx_reachable(),
    reason=f"MediaMTX is not reachable at {MEDIAMTX_HOST}:{MEDIAMTX_PORT}",
)


def probe_rtsp(url: str, timeout: float = 20.0) -> dict | None:
    """Read the published stream back with ffprobe over RTSP/TCP."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                "-print_format", "json", "-show_streams", "-i", url,
            ],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return json.loads(result.stdout or "{}")


@pytest.fixture
def live_settings(tmp_path: Path) -> Settings:
    local_dir = tmp_path / "local-sources"
    local_dir.mkdir()
    return Settings(
        mediamtx_host=MEDIAMTX_HOST,
        mediamtx_rtsp_port=MEDIAMTX_PORT,
        public_rtsp_host="localhost",
        public_rtsp_port=MEDIAMTX_PORT,
        data_dir=tmp_path,
        database_path=tmp_path / "integration.db",
        upload_video_dir=tmp_path / "videos",
        source_video_dir=local_dir,
        ffmpeg_startup_grace_seconds=2.0,
        log_level="WARNING",
    )


@needs_mediamtx
async def test_camera_is_readable_over_rtsp_and_disappears_when_stopped(
    live_settings: Settings,
) -> None:
    manager, _ = build_manager(live_settings)
    await manager.startup()
    make_real_video(live_settings.source_video_dir / "clip.mp4", seconds=3.0, size="320x240")

    camera = await manager.create_camera(
        CameraCreate.model_validate(
            {
                "name": "Integration Cam",
                "stream_path": "integration-cam",
                "loop": True,
                "auto_start": True,
                "video": {
                    "codec": "h264",
                    "resolution": {"mode": "fixed", "width": 320, "height": 240},
                    "fps": {"mode": "fixed", "value": 10},
                },
                "source": {"kind": "local", "local_path": "clip.mp4"},
            }
        ),
        None,
    )
    try:
        assert camera.status is CameraStatus.RUNNING, camera.last_error

        url = live_settings.internal_rtsp_url("integration-cam")
        payload = probe_rtsp(url)
        assert payload is not None, f"could not read {url}"
        video = next(s for s in payload["streams"] if s["codec_type"] == "video")
        assert video["codec_name"] == "h264"
        assert (video["width"], video["height"]) == (320, 240)

        stopped = await manager.stop_camera(camera.id)
        assert stopped.status is CameraStatus.STOPPED
        assert probe_rtsp(url, timeout=10.0) is None, "stream still readable after stop"

        restarted = await manager.restart_camera(camera.id)
        assert restarted.status is CameraStatus.RUNNING
        assert probe_rtsp(url) is not None
    finally:
        await manager.delete_camera(camera.id)
        await manager.shutdown()


@needs_mediamtx
async def test_two_cameras_publish_independently(live_settings: Settings) -> None:
    manager, _ = build_manager(live_settings)
    await manager.startup()
    make_real_video(live_settings.source_video_dir / "clip.mp4", seconds=2.0, size="320x240")

    created = []
    try:
        for name, path in (("Cam A", "integration-a"), ("Cam B", "integration-b")):
            camera = await manager.create_camera(
                CameraCreate.model_validate(
                    {
                        "name": name,
                        "stream_path": path,
                        "auto_start": True,
                        "source": {"kind": "local", "local_path": "clip.mp4"},
                    }
                ),
                None,
            )
            assert camera.status is CameraStatus.RUNNING, camera.last_error
            created.append(camera)

        assert probe_rtsp(live_settings.internal_rtsp_url("integration-a")) is not None
        assert probe_rtsp(live_settings.internal_rtsp_url("integration-b")) is not None

        await manager.stop_camera(created[0].id)
        assert probe_rtsp(live_settings.internal_rtsp_url("integration-a"), 10.0) is None
        assert probe_rtsp(live_settings.internal_rtsp_url("integration-b")) is not None
    finally:
        for camera in created:
            await manager.delete_camera(camera.id)
        await manager.shutdown()
