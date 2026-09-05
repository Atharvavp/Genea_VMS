"""Integration tests against a real MediaMTX 1.20.1.

Opt in with `pytest -m integration`. A container is started from the very
`mediamtx/mediamtx.yml` this repo ships, so the config is tested too, not just
the client. Everything created here is removed again.

    docker compose run --rm --no-deps vms pytest -m integration   # from Docker
    .venv/bin/python -m pytest -m integration                     # from the host
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.persistence.camera_repository import CameraRepository
from app.services.camera_manager import CameraManager
from app.services.mediamtx_client import (
    MediaMTXClient,
    MediaMTXUnavailable,
    PathNotFound,
    PathRejected,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE = "bluenviron/mediamtx:1.20.1"
CONTAINER = "vms-mediamtx-itest"
SOURCE_CONTAINER = "vms-mediamtx-itest-source"
NETWORK = "vms-mediamtx-itest-net"
API_PORT = 19997
RTSP_PORT = 18554
SOURCE_RTSP_PORT = 18555

CREDENTIALED_URL = "rtsp://admin:hunter2@192.0.2.10:554/Streaming"
UNREACHABLE_URL = "rtsp://192.0.2.1:554/nothing-here"

DOCKER = shutil.which("docker")
FFMPEG = shutil.which("ffmpeg")

requires_docker = pytest.mark.skipif(DOCKER is None, reason="docker is not installed")
requires_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


@pytest.fixture(scope="module")
def mediamtx_container():
    """The VMS's own MediaMTX, from the config this repo ships.

    A second, stock MediaMTX stands in for the external camera: the VMS one
    accepts no publishers at all (its `paths` are empty and there is no
    `all_others`), which is the point - it only pulls what the VMS configures.
    """
    if DOCKER is None:
        pytest.skip("docker is not installed")
    _teardown()
    _run(DOCKER, "network", "create", NETWORK, check=False)
    # Stock config: `all_others` lets anything publish, like a camera would.
    _run(
        DOCKER, "run", "-d", "--name", SOURCE_CONTAINER, "--network", NETWORK,
        "-p", f"{SOURCE_RTSP_PORT}:8554", IMAGE,
    )
    _run(
        DOCKER, "run", "-d", "--name", CONTAINER, "--network", NETWORK,
        "-p", f"{API_PORT}:9997", "-p", f"{RTSP_PORT}:8554",
        "-v", f"{REPO_ROOT / 'mediamtx' / 'mediamtx.yml'}:/mediamtx.yml:ro",
        IMAGE,
    )
    try:
        _wait_for_api()
        yield f"http://localhost:{API_PORT}"
    finally:
        _teardown()


def _teardown() -> None:
    _run(DOCKER, "rm", "-f", CONTAINER, SOURCE_CONTAINER, check=False)
    _run(DOCKER, "network", "rm", NETWORK, check=False)


def _wait_for_api(timeout: float = 20.0) -> None:
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://localhost:{API_PORT}/v3/info", timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    logs = _run(DOCKER, "logs", CONTAINER, check=False).stdout
    raise AssertionError(f"MediaMTX did not become ready.\n{logs}")


@pytest.fixture
async def client(mediamtx_container):
    instance = MediaMTXClient(mediamtx_container, timeout=5.0)
    try:
        yield instance
    finally:
        for name in list(await instance.list_configured_path_names()):
            if name.startswith("vms_"):
                await instance.delete_path(name)
        await instance.aclose()


# --- the shipped config -----------------------------------------------------


@requires_docker
async def test_shipped_config_boots_and_reports_the_pinned_version(client):
    info = await client.get_info()
    assert info.version == "v1.20.1"
    assert info.started


@requires_docker
async def test_control_api_accepts_the_vms_without_credentials(client):
    # The default MediaMTX policy grants `api` to 127.0.0.1 only, which would
    # lock out the VMS container; mediamtx.yml widens it deliberately.
    assert await client.list_configured_path_names() == []


def test_compose_does_not_publish_the_control_api():
    """9997 must reach only the compose network: it takes unauthenticated calls."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    published = []
    in_ports = False
    for line in compose.splitlines():
        stripped = line.strip()
        if stripped.startswith("ports:"):
            in_ports = True
            continue
        if in_ports:
            if stripped.startswith("- "):
                published.append(stripped[2:].strip('"\''))
            elif stripped and not stripped.startswith("#"):
                in_ports = False
    assert published, "expected published ports in docker-compose.yml"
    assert not [entry for entry in published if entry.endswith(":9997")], published


@requires_docker
def test_disabled_protocols_do_not_listen(mediamtx_container):
    logs = _run(DOCKER, "logs", CONTAINER, check=False).stdout + _run(
        DOCKER, "logs", CONTAINER, check=False
    ).stderr
    for protocol in ("[RTMP]", "[HLS]", "[SRT]", "[MoQ]"):
        assert protocol not in logs, protocol
    assert "[RTSP]" in logs and "[WebRTC]" in logs and "[API]" in logs


# --- dynamic path management ------------------------------------------------


@requires_docker
async def test_add_read_replace_and_delete_a_path_without_restarting(client):
    name = "vms_cam_00000001"

    await client.ensure_path(name, UNREACHABLE_URL)
    assert name in await client.list_configured_path_names()

    runtime = await client.get_path(name)
    assert runtime.name == name
    assert runtime.source_type == "rtspSource"
    # Configured but not connected: this is what OFFLINE is derived from.
    assert runtime.available is False

    # `replace` upserts, so the same call updates the source in place.
    await client.ensure_path(name, "rtsp://192.0.2.2:554/other")
    assert await client.list_configured_path_names() == [name]

    assert await client.delete_path(name) is True
    assert await client.delete_path(name) is False
    with pytest.raises(PathNotFound):
        await client.get_path(name)


@requires_docker
async def test_ensure_path_creates_a_missing_path(client):
    """`replace` is an upsert, which is why no separate `add` call is needed."""
    name = "vms_cam_00000002"
    await client.ensure_path(name, UNREACHABLE_URL)
    assert (await client.get_path(name)).name == name


@requires_docker
async def test_several_paths_are_independent(client):
    names = [f"vms_cam_0000000{index}" for index in range(3, 6)]
    for name in names:
        await client.ensure_path(name, UNREACHABLE_URL)

    await client.delete_path(names[1])

    remaining = await client.list_configured_path_names()
    assert names[0] in remaining and names[2] in remaining
    assert names[1] not in remaining


@requires_docker
async def test_static_sources_endpoint_is_absent_in_this_version(client):
    """Documents why health is derived from `available` and not a lastError."""
    import httpx

    name = "vms_cam_00000006"
    await client.ensure_path(name, UNREACHABLE_URL)
    response = httpx.get(
        f"http://localhost:{API_PORT}/v3/paths/static-sources/get/{name}", timeout=5.0
    )
    assert response.status_code == 404


@requires_docker
async def test_a_source_mediamtx_cannot_parse_is_rejected(client):
    with pytest.raises(PathRejected):
        await client.ensure_path("vms_cam_00000007", "rtsp://")


@requires_docker
async def test_unreachable_control_api_raises_mediamtx_unavailable():
    dead = MediaMTXClient("http://127.0.0.1:1", timeout=1.0)
    try:
        with pytest.raises(MediaMTXUnavailable):
            await dead.get_info()
    finally:
        await dead.aclose()


# --- credentials ------------------------------------------------------------


@requires_docker
async def test_the_client_never_hands_back_the_credentialed_config(client):
    import httpx

    name = "vms_cam_00000008"
    await client.ensure_path(name, CREDENTIALED_URL)

    # The raw Control API really does echo the password back...
    raw = httpx.get(
        f"http://localhost:{API_PORT}/v3/config/paths/get/{name}", timeout=5.0
    ).text
    assert "hunter2" in raw

    # ...which is exactly why the client extracts nothing but names.
    assert await client.list_configured_path_names() == [name]
    runtime = await client.get_path(name)
    assert "hunter2" not in repr(runtime)


# --- a real stream ----------------------------------------------------------


@requires_docker
async def test_only_vms_configured_paths_exist(client):
    """`paths: {}` with no `all_others`: an unconfigured path is not a path."""
    import httpx

    response = httpx.get(f"http://localhost:{API_PORT}/v3/paths/get/anything", timeout=5)
    assert response.status_code == 404


@requires_docker
@requires_ffmpeg
def test_the_vms_mediamtx_refuses_publishers():
    """Nothing can push a stream into the VMS MediaMTX; it only pulls.

    Without `all_others` there is no path for a publisher to claim, so an
    unmanaged stream cannot appear alongside the registered cameras.
    """
    publish = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5", "-t", "1",
            "-c:v", "libx264", "-preset", "ultrafast", "-an",
            "-f", "rtsp", "-rtsp_transport", "tcp",
            f"rtsp://127.0.0.1:{RTSP_PORT}/intruder",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert publish.returncode != 0, "the VMS MediaMTX accepted a publisher"
    assert "400 Bad Request" in publish.stderr, publish.stderr

    # ...and the same publish succeeds against a server that allows it, so the
    # refusal is this config's doing and not a broken command line.
    allowed = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5", "-t", "1",
            "-c:v", "libx264", "-preset", "ultrafast", "-an",
            "-f", "rtsp", "-rtsp_transport", "tcp",
            f"rtsp://127.0.0.1:{SOURCE_RTSP_PORT}/intruder",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert allowed.returncode == 0, allowed.stderr


@requires_docker
@requires_ffmpeg
async def test_a_live_rtsp_source_becomes_available_then_offline(client):
    """The ONLINE/OFFLINE mapping against a stream that really exists.

    FFmpeg publishes a test pattern into the stand-in camera server, and a
    managed path pulls it exactly the way it pulls a real camera. No VMS code
    goes anywhere near FFmpeg.
    """
    name = "vms_cam_00000009"
    publisher = subprocess.Popen(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-re", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
            "-t", "120", "-c:v", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-an",
            "-f", "rtsp", "-rtsp_transport", "tcp",
            f"rtsp://127.0.0.1:{SOURCE_RTSP_PORT}/itest_source",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await client.ensure_path(
            name, f"rtsp://{SOURCE_CONTAINER}:8554/itest_source"
        )

        assert await _wait_until_available(client, name, timeout=25.0), (
            "MediaMTX never reported the live source as available"
        )
        runtime = await client.get_path(name)
        assert runtime.ready is True
        assert runtime.tracks  # H264 negotiated, so a browser can play it

        # The source disappears: `available` flips back and the path stays put,
        # so the camera recovers without being recreated.
        publisher.terminate()
        publisher.wait(timeout=10)
        assert await _wait_until_available(client, name, timeout=25.0, expect=False)
        assert name in await client.list_configured_path_names()
    finally:
        if publisher.poll() is None:
            publisher.kill()
            publisher.wait(timeout=10)


async def _wait_until_available(client, name, timeout, expect=True) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (await client.get_path(name)).available is expect:
            return True
        await asyncio.sleep(0.4)
    return False


# --- the manager against the real thing -------------------------------------


@requires_docker
async def test_manager_reconciles_against_a_real_mediamtx(tmp_path, mediamtx_container):
    from app.domain.models import CameraCreate, CameraHealthState, CameraUpdate

    settings = Settings(
        database_path=tmp_path / "vms.db",
        mediamtx_api_url=mediamtx_container,
        camera_health_poll_seconds=3600.0,
        reconcile_min_interval_seconds=0.0,
    )
    client = MediaMTXClient(mediamtx_container, timeout=5.0)
    manager = CameraManager(settings, CameraRepository(settings.database_path), client)
    await manager.initialize()
    try:
        enabled = await manager.create_camera(
            CameraCreate(name="Gate", rtsp_url=UNREACHABLE_URL)
        )
        disabled = await manager.create_camera(
            CameraCreate(name="Store", rtsp_url=UNREACHABLE_URL, enabled=False)
        )

        configured = await client.list_configured_path_names()
        assert enabled.mediamtx_path in configured
        assert disabled.mediamtx_path not in configured

        await manager.refresh_health()
        assert (
            await manager.get_health(enabled.id)
        ).state is CameraHealthState.OFFLINE

        # A stale managed path left behind by an earlier run is cleaned up.
        await client.ensure_path("vms_cam_ffffffff", UNREACHABLE_URL)
        await manager.reconcile_all("test")
        configured = await client.list_configured_path_names()
        assert "vms_cam_ffffffff" not in configured
        assert enabled.mediamtx_path in configured

        await manager.update_camera(enabled.id, CameraUpdate(enabled=False))
        assert enabled.mediamtx_path not in await client.list_configured_path_names()
    finally:
        for name in await client.list_configured_path_names():
            if name.startswith("vms_"):
                await client.delete_path(name)
        await manager.shutdown()
