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
    MediaMTXPlaybackClient,
    MediaMTXUnavailable,
    PathNotFound,
    PathRejected,
    RecordingPathConfig,
    RecordingUnavailable,
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
PLAYBACK_PORT = 19996

# Short enough to finish a test in seconds rather than five minutes.
RECORDING = RecordingPathConfig(
    record_path="/recordings/%path/%Y-%m-%d/%H-%M-%S-%f",
    segment_duration="2s",
    delete_after="24h",
)

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
        "-p", f"{PLAYBACK_PORT}:9996",
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


@pytest.fixture
async def playback(mediamtx_container):
    instance = MediaMTXPlaybackClient(f"http://localhost:{PLAYBACK_PORT}", timeout=10.0)
    try:
        yield instance
    finally:
        await instance.aclose()


DAY_START = "2000-01-01T00:00:00Z"
DAY_END = "2100-01-01T00:00:00Z"


def _publish(path: str, seconds: int = 60) -> subprocess.Popen:
    """Publish H.264 into the stand-in camera server. No VMS code touches FFmpeg."""
    return subprocess.Popen(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-re", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
            "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency", "-g", "15", "-pix_fmt", "yuv420p", "-an",
            "-f", "rtsp", "-rtsp_transport", "tcp",
            f"rtsp://127.0.0.1:{SOURCE_RTSP_PORT}/{path}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop(publisher: subprocess.Popen) -> None:
    if publisher.poll() is None:
        publisher.terminate()
        try:
            publisher.wait(timeout=10)
        except subprocess.TimeoutExpired:
            publisher.kill()
            publisher.wait(timeout=10)


async def _wait_for_history(playback_client, name, timeout=40.0, expect=True):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            spans = await playback_client.list_timespans(name, DAY_START, DAY_END)
        except RecordingUnavailable:
            spans = []
        if bool(spans) is expect:
            return spans
        await asyncio.sleep(0.5)
    return []


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

    await client.ensure_path(name, UNREACHABLE_URL, record=False, recording=RECORDING)
    assert name in await client.list_configured_path_names()

    runtime = await client.get_path(name)
    assert runtime.name == name
    assert runtime.source_type == "rtspSource"
    # Configured but not connected: this is what OFFLINE is derived from.
    assert runtime.available is False

    # `replace` upserts, so the same call updates the source in place.
    await client.ensure_path(
        name, "rtsp://192.0.2.2:554/other", record=False, recording=RECORDING
    )
    assert await client.list_configured_path_names() == [name]

    assert await client.delete_path(name) is True
    assert await client.delete_path(name) is False
    with pytest.raises(PathNotFound):
        await client.get_path(name)


@requires_docker
async def test_ensure_path_creates_a_missing_path(client):
    """`replace` is an upsert, which is why no separate `add` call is needed."""
    name = "vms_cam_00000002"
    await client.ensure_path(name, UNREACHABLE_URL, record=False, recording=RECORDING)
    assert (await client.get_path(name)).name == name


@requires_docker
async def test_several_paths_are_independent(client):
    names = [f"vms_cam_0000000{index}" for index in range(3, 6)]
    for name in names:
        await client.ensure_path(
            name, UNREACHABLE_URL, record=False, recording=RECORDING
        )

    await client.delete_path(names[1])

    remaining = await client.list_configured_path_names()
    assert names[0] in remaining and names[2] in remaining
    assert names[1] not in remaining


@requires_docker
async def test_static_sources_endpoint_is_absent_in_this_version(client):
    """Documents why health is derived from `available` and not a lastError."""
    import httpx

    name = "vms_cam_00000006"
    await client.ensure_path(name, UNREACHABLE_URL, record=False, recording=RECORDING)
    response = httpx.get(
        f"http://localhost:{API_PORT}/v3/paths/static-sources/get/{name}", timeout=5.0
    )
    assert response.status_code == 404


@requires_docker
async def test_a_source_mediamtx_cannot_parse_is_rejected(client):
    with pytest.raises(PathRejected):
        await client.ensure_path(
            "vms_cam_00000007", "rtsp://", record=False, recording=RECORDING
        )


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
    await client.ensure_path(name, CREDENTIALED_URL, record=True, recording=RECORDING)

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
            name,
            f"rtsp://{SOURCE_CONTAINER}:8554/itest_source",
            record=False,
            recording=RECORDING,
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


# --- recording and playback -------------------------------------------------


@requires_docker
def test_the_playback_server_is_enabled_by_the_shipped_config(mediamtx_container):
    """The recordings UI depends on this port answering."""
    import httpx

    response = httpx.get(f"http://localhost:{PLAYBACK_PORT}/list", timeout=5.0)
    # 400 (a missing `path`) proves the server is listening and parsing.
    assert response.status_code == 400
    assert "Access-Control-Allow-Origin" in response.headers


@requires_docker
async def test_a_full_recording_config_is_accepted(client):
    name = "vms_cam_rec00001"
    await client.ensure_path(name, UNREACHABLE_URL, record=True, recording=RECORDING)
    assert await client.path_config_matches(
        name, UNREACHABLE_URL, record=True, recording=RECORDING
    )


@requires_docker
async def test_config_comparison_survives_duration_normalisation(client):
    """MediaMTX reads `5m` back as `5m0s` and `24h` as `1d`; still a match."""
    name = "vms_cam_rec00002"
    config = RecordingPathConfig(
        record_path="/recordings/%path/%Y-%m-%d/%H-%M-%S-%f",
        segment_duration="5m",
        delete_after="24h",
    )
    await client.ensure_path(name, UNREACHABLE_URL, record=False, recording=config)
    assert await client.path_config_matches(
        name, UNREACHABLE_URL, record=False, recording=config
    )
    # ...and a real difference is still detected.
    assert not await client.path_config_matches(
        name, UNREACHABLE_URL, record=True, recording=config
    )


@requires_docker
@requires_ffmpeg
async def test_a_real_stream_is_recorded_and_listed_and_plays(client, playback):
    """The whole recording chain against the pinned server."""
    import httpx

    name = "vms_cam_rec00003"
    publisher = _publish("rec_source")
    try:
        await client.ensure_path(
            name,
            f"rtsp://{SOURCE_CONTAINER}:8554/rec_source",
            record=True,
            recording=RECORDING,
        )

        spans = await _wait_for_history(playback, name)
        assert spans, "MediaMTX never reported a recorded timespan"

        # `/list` merges consecutive segments into one continuous timespan.
        span = spans[0]
        assert span.duration_seconds > 0
        assert span.end > span.start

        # The playback URL the VMS would hand the browser really returns MP4.
        response = httpx.get(
            f"http://localhost:{PLAYBACK_PORT}/get",
            params={
                "path": name,
                "start": span.start,
                "duration": span.duration_seconds,
                "format": "mp4",
            },
            timeout=30.0,
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"
        assert len(response.content) > 1000
        # `moov` before `mdat`, so a <video> element can start playing and seek
        # rather than waiting for the whole file.
        assert response.content[4:8] == b"ftyp"
        assert b"moov" in response.content[:4096]
    finally:
        _stop(publisher)


@requires_docker
@requires_ffmpeg
async def test_recording_off_keeps_the_history_listable(client, playback):
    """Turning recording off stops new segments; it must not hide old ones."""
    name = "vms_cam_rec00004"
    publisher = _publish("rec_off_source")
    try:
        await client.ensure_path(
            name,
            f"rtsp://{SOURCE_CONTAINER}:8554/rec_off_source",
            record=True,
            recording=RECORDING,
        )
        assert await _wait_for_history(playback, name)

        await client.ensure_path(
            name,
            f"rtsp://{SOURCE_CONTAINER}:8554/rec_off_source",
            record=False,
            recording=RECORDING,
        )
        await asyncio.sleep(1.0)
        assert await playback.list_timespans(name, DAY_START, DAY_END)
    finally:
        _stop(publisher)


@requires_docker
@requires_ffmpeg
async def test_a_partial_replacement_hides_history_and_a_full_one_restores_it(
    client, playback
):
    """The reason `ensure_path` always sends the whole recording block.

    A Component 2-shaped payload resets `recordPath` to MediaMTX's default, and
    the playback server then finds nothing under the configured root - even
    though the files are still on disk.
    """
    import httpx

    name = "vms_cam_rec00005"
    publisher = _publish("partial_source")
    try:
        await client.ensure_path(
            name,
            f"rtsp://{SOURCE_CONTAINER}:8554/partial_source",
            record=True,
            recording=RECORDING,
        )
        assert await _wait_for_history(playback, name)

        # Exactly what Component 2 used to send.
        httpx.post(
            f"http://localhost:{API_PORT}/v3/config/paths/replace/{name}",
            json={
                "source": f"rtsp://{SOURCE_CONTAINER}:8554/partial_source",
                "sourceOnDemand": False,
                "rtspTransport": "tcp",
                "record": False,
            },
            timeout=10.0,
        ).raise_for_status()
        await asyncio.sleep(1.0)

        assert await playback.list_timespans(name, DAY_START, DAY_END) == []

        # The full payload the VMS actually sends brings the history back.
        await client.ensure_path(
            name,
            f"rtsp://{SOURCE_CONTAINER}:8554/partial_source",
            record=False,
            recording=RECORDING,
        )
        await asyncio.sleep(1.0)
        assert await playback.list_timespans(name, DAY_START, DAY_END)
    finally:
        _stop(publisher)


@requires_docker
@requires_ffmpeg
async def test_a_deleted_path_hides_history_and_recreating_it_restores_it(
    client, playback
):
    """Why disabled cameras have no browsable history in this version."""
    import httpx

    name = "vms_cam_rec00006"
    publisher = _publish("disabled_source")
    try:
        await client.ensure_path(
            name,
            f"rtsp://{SOURCE_CONTAINER}:8554/disabled_source",
            record=True,
            recording=RECORDING,
        )
        assert await _wait_for_history(playback, name)

        # Disabling a camera removes its path - Component 2 semantics.
        await client.delete_path(name)
        await asyncio.sleep(0.5)

        with pytest.raises(RecordingUnavailable):
            await playback.list_timespans(name, DAY_START, DAY_END)
        # The recording admin API refuses for the same reason.
        admin = httpx.get(
            f"http://localhost:{API_PORT}/v3/recordings/get/{name}", timeout=5.0
        )
        assert admin.status_code == 400
        assert "not configured" in admin.text

        # Re-enabling the camera makes the same files visible again.
        await client.ensure_path(
            name,
            f"rtsp://{SOURCE_CONTAINER}:8554/disabled_source",
            record=True,
            recording=RECORDING,
        )
        await asyncio.sleep(1.0)
        assert await playback.list_timespans(name, DAY_START, DAY_END)
    finally:
        _stop(publisher)


@requires_docker
async def test_an_empty_day_is_not_an_error(client, playback):
    """A configured path with nothing recorded that day lists as empty."""
    name = "vms_cam_rec00007"
    await client.ensure_path(name, UNREACHABLE_URL, record=True, recording=RECORDING)
    assert await playback.list_timespans(name, DAY_START, DAY_END) == []
    assert (
        await playback.list_timespans(
            name, "2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z"
        )
        == []
    )


@requires_docker
async def test_an_unconfigured_path_is_unavailable(playback):
    with pytest.raises(RecordingUnavailable):
        await playback.list_timespans("vms_cam_nonexistent", DAY_START, DAY_END)


@requires_docker
async def test_an_unreachable_playback_server_is_unavailable():
    dead = MediaMTXPlaybackClient("http://127.0.0.1:1", timeout=1.0)
    try:
        with pytest.raises(RecordingUnavailable):
            await dead.list_timespans("vms_cam_x", DAY_START, DAY_END)
    finally:
        await dead.aclose()


@requires_docker
@requires_ffmpeg
async def test_native_retention_deletes_expired_recordings(client, playback):
    """Retention is MediaMTX's own `recordDeleteAfter`; the VMS runs no cleanup."""
    name = "vms_cam_rec00008"
    short_retention = RecordingPathConfig(
        record_path="/recordings/%path/%Y-%m-%d/%H-%M-%S-%f",
        segment_duration="2s",
        delete_after="15s",
    )
    publisher = _publish("retention_source", seconds=10)
    try:
        await client.ensure_path(
            name,
            f"rtsp://{SOURCE_CONTAINER}:8554/retention_source",
            record=True,
            recording=short_retention,
        )
        assert await _wait_for_history(playback, name)
    finally:
        _stop(publisher)

    # Everything recorded ages past 15s and MediaMTX removes it by itself.
    assert (
        await _wait_for_history(playback, name, timeout=60.0, expect=False) == []
    )


@requires_docker
@requires_ffmpeg
async def test_a_source_loss_and_return_resumes_recording_on_the_same_path(
    client, playback
):
    name = "vms_cam_rec00009"
    publisher = _publish("flap_source", seconds=15)
    try:
        await client.ensure_path(
            name,
            f"rtsp://{SOURCE_CONTAINER}:8554/flap_source",
            record=True,
            recording=RECORDING,
        )
        assert await _wait_for_history(playback, name)
    finally:
        _stop(publisher)

    assert await _wait_until_available(client, name, timeout=30.0, expect=False)
    # The path stays configured, so history stays listable while it is down.
    assert name in await client.list_configured_path_names()
    assert await playback.list_timespans(name, DAY_START, DAY_END)

    publisher = _publish("flap_source", seconds=15)
    try:
        # No recreation, no retoggle: MediaMTX reconnects and records again.
        assert await _wait_until_available(client, name, timeout=40.0)
        spans = await _wait_for_history(playback, name)
        assert spans
    finally:
        _stop(publisher)


@requires_docker
@requires_ffmpeg
async def test_four_cameras_record_concurrently(client, playback):
    """Four at once, where resources permit. Not a capacity claim."""
    names = [f"vms_cam_multi{index}" for index in range(4)]
    publishers = [_publish(f"multi_source{index}", seconds=45) for index in range(4)]
    try:
        for index, name in enumerate(names):
            await client.ensure_path(
                name,
                f"rtsp://{SOURCE_CONTAINER}:8554/multi_source{index}",
                record=True,
                recording=RECORDING,
            )

        for name in names:
            assert await _wait_for_history(playback, name, timeout=60.0), name

        # One source stopping leaves the other three recording.
        _stop(publishers[0])
        await asyncio.sleep(3)
        for name in names[1:]:
            assert await playback.list_timespans(name, DAY_START, DAY_END), name
    finally:
        for publisher in publishers:
            _stop(publisher)


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
        await client.ensure_path(
            "vms_cam_ffffffff", UNREACHABLE_URL, record=False, recording=RECORDING
        )
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
