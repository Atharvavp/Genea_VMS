"""End-to-end proof in a real browser.

    video file -> simulator FFmpeg -> simulator MediaMTX -> RTSP
               -> VMS MediaMTX -> WebRTC -> Chromium

Opt in with `pytest -m e2e`. Both stacks must already be running, each from its
own clone:

    (simulator clone) docker compose up -d --build      # http://localhost:8080
    (this clone)      docker compose up -d --build      # http://localhost:8090
    .venv/bin/python -m playwright install chromium
    .venv/bin/python -m pytest -m e2e

The simulator is used here the way a bench uses a signal generator: these tests
call its API to produce and interrupt an RTSP stream. Nothing in `app/` knows it
exists - the VMS only ever sees an RTSP URL.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.e2e

VMS_URL = os.environ.get("VMS_URL", "http://localhost:8090")
SIMULATOR_URL = os.environ.get("SIMULATOR_URL", "http://localhost:8080")
# How the VMS container reaches the simulator's RTSP port on the host.
SIMULATOR_RTSP_HOST = os.environ.get("SIMULATOR_RTSP_HOST", "host.docker.internal:8554")
SAMPLE_VIDEO = os.environ.get("E2E_SAMPLE_VIDEO", "sample-320x240.mp4")

TAG = uuid.uuid4().hex[:6]


def _skip_unless_running(url: str, what: str) -> None:
    try:
        httpx.get(f"{url}/health", timeout=3.0)
    except httpx.HTTPError:
        pytest.skip(f"{what} is not running at {url}")


@pytest.fixture(scope="module", autouse=True)
def stacks_are_up():
    _skip_unless_running(VMS_URL, "the VMS stack")
    _skip_unless_running(SIMULATOR_URL, "the simulator stack")


# --- external RTSP source (the simulator) -----------------------------------


class SimulatorSource:
    """One virtual camera in the simulator stack, used as an RTSP source."""

    def __init__(self, http: httpx.Client, name: str) -> None:
        self._http = http
        response = http.post(
            "/api/cameras",
            json={
                "name": name,
                "auto_start": True,
                "loop": True,
                "source": {"kind": "local", "local_path": SAMPLE_VIDEO},
            },
        )
        response.raise_for_status()
        body = response.json()
        self.id = body["id"]
        self.stream_path = body["stream_path"]

    @property
    def rtsp_url(self) -> str:
        return f"rtsp://{SIMULATOR_RTSP_HOST}/simulator/{self.stream_path}"

    def stop(self) -> None:
        self._http.post(f"/api/cameras/{self.id}/stop").raise_for_status()

    def start(self) -> None:
        self._http.post(f"/api/cameras/{self.id}/start").raise_for_status()

    def destroy(self) -> None:
        self._http.delete(f"/api/cameras/{self.id}")


@pytest.fixture(scope="module")
def simulator():
    with httpx.Client(base_url=SIMULATOR_URL, timeout=30.0) as http:
        created: list[SimulatorSource] = []

        def make(name: str) -> SimulatorSource:
            source = SimulatorSource(http, f"e2e-{TAG}-{name}")
            created.append(source)
            return source

        try:
            yield make
        finally:
            for source in created:
                source.destroy()


# --- the VMS ----------------------------------------------------------------


@pytest.fixture(scope="module")
def vms():
    with httpx.Client(base_url=VMS_URL, timeout=30.0) as http:
        created: list[str] = []

        class Vms:
            def add(self, name: str, rtsp_url: str, enabled: bool = True) -> dict:
                response = http.post(
                    "/api/cameras",
                    json={"name": name, "rtsp_url": rtsp_url, "enabled": enabled},
                )
                response.raise_for_status()
                body = response.json()
                created.append(body["id"])
                return body

            def get(self, camera_id: str) -> dict:
                return http.get(f"/api/cameras/{camera_id}").raise_for_status().json()

            def patch(self, camera_id: str, **payload) -> dict:
                response = http.patch(f"/api/cameras/{camera_id}", json=payload)
                response.raise_for_status()
                return response.json()

            def post(self, path: str) -> dict:
                return http.post(path).raise_for_status().json()

            def delete(self, camera_id: str) -> None:
                http.delete(f"/api/cameras/{camera_id}")
                if camera_id in created:
                    created.remove(camera_id)

            def wait_health(self, camera_id: str, state: str, timeout: float = 45.0):
                deadline = time.monotonic() + timeout
                seen = None
                while time.monotonic() < deadline:
                    seen = http.get(f"/api/cameras/{camera_id}/status").json()["state"]
                    if seen == state:
                        return
                    time.sleep(0.5)
                raise AssertionError(f"health stayed {seen}, expected {state}")

        try:
            yield Vms()
        finally:
            for camera_id in list(created):
                http.delete(f"/api/cameras/{camera_id}")


# --- browser ----------------------------------------------------------------


@pytest.fixture
async def page():
    playwright_api = pytest.importorskip("playwright.async_api")
    async with playwright_api.async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context()
        instance = await context.new_page()
        errors: list[str] = []
        instance.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            yield instance
            assert not errors, f"page errors: {errors}"
        finally:
            await context.close()
            await browser.close()


TILE_STATS_JS = """(cameraId) => {
  const tile = document.querySelector(`.tile[data-camera-id="${cameraId}"]`);
  if (!tile) return null;
  const video = tile.querySelector("video");
  return {
    name: tile.querySelector('[data-role=name]').textContent,
    health: tile.querySelector('[data-role=health]').textContent,
    player: tile.querySelector('[data-role=player]').textContent,
    url: tile.querySelector('[data-role=url]').textContent,
    width: video.videoWidth,
    height: video.videoHeight,
    readyState: video.readyState,
    hasStream: video.srcObject !== null,
  };
}"""


async def tile_stats(page, camera_id):
    return await page.evaluate(TILE_STATS_JS, camera_id)


async def wait_for_tile(page, camera_id, predicate, timeout=45.0, what="condition"):
    deadline = time.monotonic() + timeout
    stats = None
    while time.monotonic() < deadline:
        stats = await tile_stats(page, camera_id)
        if stats and predicate(stats):
            return stats
        await asyncio.sleep(0.5)
    raise AssertionError(f"tile {camera_id} never reached {what}; last: {stats}")


def is_playing(stats) -> bool:
    return (
        stats["player"] == "LIVE"
        and stats["readyState"] >= 2
        and stats["width"] > 0
        and stats["height"] > 0
    )


async def wait_for_tile_removed(page, camera_id, timeout=20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await tile_stats(page, camera_id) is None:
            return True
        await asyncio.sleep(0.5)
    return False


async def frame_signature(page, camera_id):
    """A cheap hash of the current frame, to prove pixels are actually moving."""
    return await page.evaluate(
        """(cameraId) => {
          const video = document.querySelector(
            `.tile[data-camera-id="${cameraId}"] video`);
          const canvas = document.createElement("canvas");
          canvas.width = 32;
          canvas.height = 24;
          const ctx = canvas.getContext("2d");
          ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
          const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
          let hash = 0;
          for (let i = 0; i < data.length; i += 4) {
            hash = (hash * 31 + data[i] + data[i + 1] * 3 + data[i + 2] * 7) | 0;
          }
          return hash;
        }""",
        camera_id,
    )


# --- tests ------------------------------------------------------------------


async def test_registered_camera_plays_in_the_browser(page, simulator, vms):
    """The whole chain, in one test."""
    source = simulator("solo")
    camera = vms.add("E2E Solo", source.rtsp_url)
    vms.wait_health(camera["id"], "ONLINE")

    await page.goto(VMS_URL)
    stats = await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")

    assert stats["health"] == "ONLINE"
    assert stats["url"] == source.rtsp_url
    assert (stats["width"], stats["height"]) == (320, 240)

    # Frames keep arriving, so this is live video and not one stuck picture.
    first = await frame_signature(page, camera["id"])
    await asyncio.sleep(1.5)
    assert await frame_signature(page, camera["id"]) != first


async def test_webrtc_comes_from_the_vms_not_the_simulator(page, simulator, vms):
    source = simulator("origin")
    camera = vms.add("E2E Origin", source.rtsp_url)
    vms.wait_health(camera["id"], "ONLINE")

    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    await page.goto(VMS_URL)
    await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")

    whep = [url for url in requests if url.endswith("/whep")]
    assert whep, "no WHEP request was made"
    assert all(url.startswith("http://localhost:8889/") for url in whep), whep
    # The browser never speaks to the simulator's RTSP port.
    assert not [url for url in requests if ":8554" in url or ":8080" in url]


async def test_four_cameras_play_independently(page, simulator, vms):
    cameras = []
    for index in range(4):
        source = simulator(f"grid{index}")
        cameras.append(vms.add(f"E2E Grid {index}", source.rtsp_url))
    for camera in cameras:
        vms.wait_health(camera["id"], "ONLINE")

    await page.goto(VMS_URL)
    for camera in cameras:
        await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")

    # Deleting one leaves the rest untouched.
    victim = cameras.pop()
    survivor_frames = {
        camera["id"]: await frame_signature(page, camera["id"]) for camera in cameras
    }
    vms.delete(victim["id"])
    assert await wait_for_tile_removed(page, victim["id"])

    for camera in cameras:
        stats = await tile_stats(page, camera["id"])
        assert is_playing(stats), stats
        assert await frame_signature(page, camera["id"]) != survivor_frames[camera["id"]]


async def test_rename_does_not_interrupt_playback(page, simulator, vms):
    source = simulator("rename")
    camera = vms.add("E2E Before", source.rtsp_url)
    other_source = simulator("rename-other")
    other = vms.add("E2E Bystander", other_source.rtsp_url)
    vms.wait_health(camera["id"], "ONLINE")
    vms.wait_health(other["id"], "ONLINE")

    await page.goto(VMS_URL)
    await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")
    await wait_for_tile(page, other["id"], is_playing, what="LIVE playback")

    vms.patch(camera["id"], name="E2E After")

    await wait_for_tile(
        page,
        camera["id"],
        lambda stats: stats["name"] == "E2E After",
        what="the new name",
    )
    # Same session throughout: never dropped back to CONNECTING.
    stats = await tile_stats(page, camera["id"])
    assert stats["player"] == "LIVE"
    assert (await tile_stats(page, other["id"]))["player"] == "LIVE"


async def test_changing_the_source_url_reconciles_one_camera(page, simulator, vms):
    first = simulator("swap-a")
    second = simulator("swap-b")
    camera = vms.add("E2E Swap", first.rtsp_url)
    vms.wait_health(camera["id"], "ONLINE")

    await page.goto(VMS_URL)
    await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")

    updated = vms.patch(camera["id"], rtsp_url=second.rtsp_url)
    # The path and therefore the WebRTC URL are derived from the id, so they
    # survive an edit and the player only has to reconnect.
    assert updated["mediamtx_path"] == camera["mediamtx_path"]
    assert updated["webrtc_url"] == camera["webrtc_url"]

    vms.wait_health(camera["id"], "ONLINE")
    stats = await wait_for_tile(
        page,
        camera["id"],
        lambda s: s["url"] == second.rtsp_url and is_playing(s),
        what="playback from the new source",
    )
    assert stats["health"] == "ONLINE"


async def test_disable_and_enable_one_camera(page, simulator, vms):
    source = simulator("toggle")
    camera = vms.add("E2E Toggle", source.rtsp_url)
    keeper_source = simulator("toggle-keeper")
    keeper = vms.add("E2E Keeper", keeper_source.rtsp_url)
    vms.wait_health(camera["id"], "ONLINE")
    vms.wait_health(keeper["id"], "ONLINE")

    await page.goto(VMS_URL)
    await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")
    await wait_for_tile(page, keeper["id"], is_playing, what="LIVE playback")

    vms.post(f"/api/cameras/{camera['id']}/disable")
    await wait_for_tile(
        page,
        camera["id"],
        lambda stats: stats["health"] == "DISABLED" and not stats["hasStream"],
        what="the disabled state",
    )
    assert (await tile_stats(page, keeper["id"]))["player"] == "LIVE"

    vms.post(f"/api/cameras/{camera['id']}/enable")
    vms.wait_health(camera["id"], "ONLINE")
    await wait_for_tile(page, camera["id"], is_playing, what="playback again")


async def test_source_disappears_and_returns_without_recreating_the_camera(
    page, simulator, vms
):
    source = simulator("flap")
    camera = vms.add("E2E Flap", source.rtsp_url)
    vms.wait_health(camera["id"], "ONLINE")

    await page.goto(VMS_URL)
    await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")

    source.stop()
    vms.wait_health(camera["id"], "OFFLINE")
    stats = await wait_for_tile(
        page,
        camera["id"],
        lambda s: s["health"] == "OFFLINE",
        what="OFFLINE health",
    )
    # Source health and player state are reported separately.
    assert stats["player"] in {"RECONNECTING", "LIVE", "CONNECTING", "ERROR"}

    source.start()
    vms.wait_health(camera["id"], "ONLINE")
    await wait_for_tile(page, camera["id"], is_playing, timeout=60.0, what="recovery")

    # Same camera record throughout: nothing was recreated.
    assert vms.get(camera["id"])["created_at"] == camera["created_at"]
    assert vms.get(camera["id"])["mediamtx_path"] == camera["mediamtx_path"]


async def test_focused_view_leaves_the_other_tiles_alone(page, simulator, vms):
    focused_source = simulator("focus")
    focused = vms.add("E2E Focus", focused_source.rtsp_url)
    other_source = simulator("focus-other")
    other = vms.add("E2E Focus Other", other_source.rtsp_url)
    vms.wait_health(focused["id"], "ONLINE")
    vms.wait_health(other["id"], "ONLINE")

    await page.goto(VMS_URL)
    await wait_for_tile(page, focused["id"], is_playing, what="LIVE playback")
    await wait_for_tile(page, other["id"], is_playing, what="LIVE playback")

    assert await page.is_hidden("#focus-modal")
    await page.click(f'.tile[data-camera-id="{focused["id"]}"] [data-action="focus"]')
    assert await page.is_visible("#focus-modal")

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        focus_video = await page.evaluate(
            """() => {
              const video = document.getElementById("focus-video");
              return {
                width: video.videoWidth,
                readyState: video.readyState,
                state: document.getElementById("focus-player").textContent,
              };
            }"""
        )
        if focus_video["width"] > 0 and focus_video["readyState"] >= 2:
            break
        await asyncio.sleep(0.5)
    assert focus_video["width"] > 0, focus_video
    assert focus_video["state"] == "LIVE"

    # The focused camera's own tile hands its session over rather than running
    # two; every other tile is untouched.
    tile = await tile_stats(page, focused["id"])
    assert tile["hasStream"] is False
    assert (await tile_stats(page, other["id"]))["player"] == "LIVE"

    await page.click("#focus-close")
    assert await page.is_hidden("#focus-modal")
    await wait_for_tile(page, focused["id"], is_playing, what="the tile taking over")


async def test_browser_refresh_recovers_playback(page, simulator, vms):
    source = simulator("refresh")
    camera = vms.add("E2E Refresh", source.rtsp_url)
    vms.wait_health(camera["id"], "ONLINE")

    await page.goto(VMS_URL)
    await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")

    await page.reload()
    await wait_for_tile(page, camera["id"], is_playing, what="playback after reload")


async def test_add_and_delete_a_camera_through_the_ui(page, simulator, vms):
    source = simulator("ui")
    await page.goto(VMS_URL)

    assert await page.is_hidden("#modal")
    await page.click("#add-camera")
    assert await page.is_visible("#modal")
    await page.fill('#camera-form input[name="name"]', f"E2E UI {TAG}")
    await page.fill("#rtsp-url-input", source.rtsp_url)
    await page.click("#modal-submit")
    try:
        # The dialog closes only once the POST has succeeded.
        await page.wait_for_selector("#modal", state="hidden", timeout=15000)
    except Exception:
        message = await page.text_content("#form-error")
        raise AssertionError(f"the dialog stayed open; form error: {message!r}")

    camera_id = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and camera_id is None:
        for item in httpx.get(f"{VMS_URL}/api/cameras", timeout=10).json():
            if item["name"] == f"E2E UI {TAG}":
                camera_id = item["id"]
        await asyncio.sleep(0.5)
    assert camera_id, "the camera created through the UI never appeared"

    try:
        await wait_for_tile(page, camera_id, is_playing, what="LIVE playback")
        page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))
        await page.click(f'.tile[data-camera-id="{camera_id}"] [data-action="delete"]')
        assert await wait_for_tile_removed(page, camera_id)
    finally:
        httpx.delete(f"{VMS_URL}/api/cameras/{camera_id}", timeout=10)


async def test_a_credentialed_url_is_never_rendered_in_the_page(page, vms):
    camera = vms.add(
        f"E2E Secret {TAG}", "rtsp://admin:hunter2@192.0.2.10:554/Streaming"
    )
    await page.goto(VMS_URL)
    await wait_for_tile(
        page, camera["id"], lambda stats: stats["url"] != "", what="its tile"
    )

    content = await page.content()
    assert "hunter2" not in content
    assert "admin:***@192.0.2.10:554" in content

    # The edit form starts empty for a credentialed camera.
    await page.click(f'.tile[data-camera-id="{camera["id"]}"] [data-action="edit"]')
    assert await page.input_value("#rtsp-url-input") == ""
    assert await page.is_visible("#keep-source-hint")
    await page.click("#modal-cancel")
