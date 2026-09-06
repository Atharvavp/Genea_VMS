"""End-to-end proof in a real browser.

    live:      video file -> simulator FFmpeg -> simulator MediaMTX -> RTSP
                          -> VMS MediaMTX -> WebRTC -> Chromium
    recorded:  the same RTSP -> VMS MediaMTX recording -> its playback server
                             -> <video controls> in Chromium

Opt in with `pytest -m e2e`. Both stacks must already be running, each from its
own clone:

    (simulator clone) docker compose up -d --build      # http://localhost:8080
    (this clone)      RECORDING_SEGMENT_DURATION=5s docker compose up -d --build
    .venv/bin/python -m playwright install chromium
    .venv/bin/python -m pytest -m e2e

A short `RECORDING_SEGMENT_DURATION` matters: with the 5-minute default these
tests would wait five minutes for the first finalised segment. Set it in `.env`
or the environment before starting the stack; `E2E_RECORDING_TIMEOUT` bounds how
long the recording tests wait for history to appear.

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
# How long to wait for MediaMTX to produce listable history. Scales with the
# stack's RECORDING_SEGMENT_DURATION.
RECORDING_TIMEOUT = float(os.environ.get("E2E_RECORDING_TIMEOUT", "90"))

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
            def add(
                self,
                name: str,
                rtsp_url: str,
                enabled: bool = True,
                recording_enabled: bool = False,
            ) -> dict:
                response = http.post(
                    "/api/cameras",
                    json={
                        "name": name,
                        "rtsp_url": rtsp_url,
                        "enabled": enabled,
                        "recording_enabled": recording_enabled,
                    },
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

            def wait_recording(
                self, camera_id: str, state: str, timeout: float = 45.0
            ):
                deadline = time.monotonic() + timeout
                seen = None
                while time.monotonic() < deadline:
                    seen = self.get(camera_id)["recording"]["state"]
                    if seen == state:
                        return
                    time.sleep(0.5)
                raise AssertionError(f"recording stayed {seen}, expected {state}")

            def wait_recordings(self, camera_id: str, timeout: float):
                """Wait until MediaMTX can list history for today."""
                date = time.strftime("%Y-%m-%d", time.gmtime())
                deadline = time.monotonic() + timeout
                last = None
                while time.monotonic() < deadline:
                    response = http.get(
                        "/api/recordings",
                        params={"camera_id": camera_id, "date": date},
                    )
                    last = response.status_code
                    if response.status_code == 200:
                        items = response.json()["items"]
                        if items:
                            return items
                    time.sleep(1.0)
                raise AssertionError(
                    f"no recordings appeared for {camera_id} (last status {last})"
                )

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
    recording: tile.querySelector('[data-role=recording]').textContent,
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


# --- recording and historical playback --------------------------------------


async def test_recording_is_off_until_it_is_switched_on(page, simulator, vms):
    """Registering a camera must never start writing to disk by itself."""
    source = simulator("rec-default")
    camera = vms.add("E2E Rec Default", source.rtsp_url)
    vms.wait_health(camera["id"], "ONLINE")

    assert camera["recording_enabled"] is False
    assert camera["recording"]["state"] == "DISABLED"

    await page.goto(VMS_URL)
    stats = await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")
    assert stats["recording"] == "REC OFF"


async def test_recording_toggles_from_the_tile_without_touching_live(
    page, simulator, vms
):
    source = simulator("rec-toggle")
    camera = vms.add("E2E Rec Toggle", source.rtsp_url)
    vms.wait_health(camera["id"], "ONLINE")

    await page.goto(VMS_URL)
    await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")

    await page.click(f'.tile[data-camera-id="{camera["id"]}"] [data-action="record"]')

    stats = await wait_for_tile(
        page,
        camera["id"],
        lambda s: s["recording"] == "RECORDING",
        what="the RECORDING badge",
    )
    # Live is untouched throughout: the same session keeps playing.
    assert stats["player"] == "LIVE"
    assert vms.get(camera["id"])["recording_enabled"] is True

    await page.click(f'.tile[data-camera-id="{camera["id"]}"] [data-action="record"]')
    stats = await wait_for_tile(
        page,
        camera["id"],
        lambda s: s["recording"] == "REC OFF",
        what="recording switched off",
    )
    assert stats["player"] == "LIVE"
    assert is_playing(stats)


async def test_a_recording_plays_back_in_the_browser(page, simulator, vms):
    """The recorded half of the chain, end to end."""
    source = simulator("rec-play")
    camera = vms.add("E2E Rec Play", source.rtsp_url, recording_enabled=True)
    vms.wait_health(camera["id"], "ONLINE")
    vms.wait_recording(camera["id"], "RECORDING")
    items = vms.wait_recordings(camera["id"], timeout=RECORDING_TIMEOUT)
    assert items

    await page.goto(VMS_URL)
    await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")

    assert await page.is_hidden("#recordings-modal")
    await page.click(
        f'.tile[data-camera-id="{camera["id"]}"] [data-action="recordings"]'
    )
    assert await page.is_visible("#recordings-modal")

    # The date defaults to today (UTC) and the list loads for it.
    assert await page.input_value("#recordings-date") == time.strftime(
        "%Y-%m-%d", time.gmtime()
    )
    await page.wait_for_selector("#recordings-list .recording-row", timeout=30000)

    rows = await page.query_selector_all("#recordings-list .recording-row")
    assert rows

    # Chronological order.
    times = await page.evaluate(
        """() => Array.from(
             document.querySelectorAll('#recordings-list .recording-time')
           ).map((el) => el.textContent)"""
    )
    assert times == sorted(times)

    await rows[0].click()
    await page.wait_for_selector("#recordings-player", state="visible", timeout=10000)

    # A real MP4 arriving from MediaMTX's playback server, in a native element.
    deadline = time.monotonic() + 45
    stats = None
    while time.monotonic() < deadline:
        stats = await page.evaluate(
            """() => {
              const video = document.getElementById("recordings-video");
              return {
                src: video.getAttribute("src") || "",
                width: video.videoWidth,
                readyState: video.readyState,
                duration: video.duration,
                controls: video.controls,
                paused: video.paused,
                currentTime: video.currentTime,
              };
            }"""
        )
        if stats["readyState"] >= 2 and stats["width"] > 0:
            break
        await asyncio.sleep(0.5)

    assert stats["width"] > 0, stats
    assert stats["readyState"] >= 2, stats
    assert stats["controls"] is True  # native controls, no custom transport
    # The URL points at the playback server, not at the VMS or the Control API.
    assert stats["src"].startswith("http://localhost:9996/get?"), stats["src"]
    assert "format=mp4" in stats["src"]

    # It actually advances.
    before = stats["currentTime"]
    await asyncio.sleep(2.0)
    after = await page.evaluate(
        '() => document.getElementById("recordings-video").currentTime'
    )
    assert after > before, f"playback did not advance: {before} -> {after}"

    # Live is unaffected by any of this.
    assert (await tile_stats(page, camera["id"]))["player"] == "LIVE"

    await page.click("#recordings-close")
    assert await page.is_hidden("#recordings-modal")
    # Closing stops the media rather than leaving it downloading in the background.
    assert await page.evaluate(
        '() => !document.getElementById("recordings-video").getAttribute("src")'
    )


async def test_a_day_with_no_recordings_shows_the_empty_state(page, simulator, vms):
    source = simulator("rec-empty")
    camera = vms.add("E2E Rec Empty", source.rtsp_url)
    vms.wait_health(camera["id"], "ONLINE")

    await page.goto(VMS_URL)
    await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")

    await page.click(
        f'.tile[data-camera-id="{camera["id"]}"] [data-action="recordings"]'
    )
    # A day this camera certainly did not record.
    await page.fill("#recordings-date", "2020-01-01")
    await page.dispatch_event("#recordings-date", "change")

    await page.wait_for_selector("#recordings-empty", state="visible", timeout=15000)
    assert await page.is_hidden("#recordings-error")  # empty is not a failure
    await page.click("#recordings-close")


async def test_a_history_failure_does_not_change_the_recording_badge(
    page, simulator, vms
):
    """The separation that matters: playback failing is not the camera failing."""
    source = simulator("rec-outage")
    camera = vms.add("E2E Rec Outage", source.rtsp_url, recording_enabled=True)
    vms.wait_health(camera["id"], "ONLINE")
    vms.wait_recording(camera["id"], "RECORDING")

    await page.goto(VMS_URL)
    # Both must be settled before the outage, so "still LIVE" afterwards means
    # something: the recording badge converges before the WebRTC session does.
    await wait_for_tile(
        page,
        camera["id"],
        lambda s: s["recording"] == "RECORDING" and is_playing(s),
        what="recording and playing",
    )

    # Make the recordings request fail the way a playback outage would.
    await page.route("**/api/recordings*", lambda route: route.abort())
    await page.click(
        f'.tile[data-camera-id="{camera["id"]}"] [data-action="recordings"]'
    )
    await page.wait_for_selector("#recordings-error", state="visible", timeout=15000)

    # The dialog reports history UNAVAILABLE...
    assert await page.is_visible("#recordings-error")
    assert await page.is_hidden("#recordings-list")

    # ...and the camera still reads RECORDING and LIVE.
    stats = await tile_stats(page, camera["id"])
    assert stats["recording"] == "RECORDING"
    assert stats["player"] == "LIVE"
    assert vms.get(camera["id"])["recording"]["state"] == "RECORDING"

    await page.unroute("**/api/recordings*")
    await page.click("#recordings-close")


async def test_a_disabled_camera_cannot_browse_history(page, simulator, vms):
    """Disabling removes the MediaMTX path, and playback needs it configured."""
    source = simulator("rec-disabled")
    camera = vms.add("E2E Rec Disabled", source.rtsp_url, recording_enabled=True)
    vms.wait_health(camera["id"], "ONLINE")

    await page.goto(VMS_URL)
    await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")

    vms.post(f"/api/cameras/{camera['id']}/disable")
    await wait_for_tile(
        page,
        camera["id"],
        lambda s: s["health"] == "DISABLED",
        what="the disabled state",
    )

    disabled = await page.get_attribute(
        f'.tile[data-camera-id="{camera["id"]}"] [data-action="recordings"]', "disabled"
    )
    assert disabled is not None

    # The preference survives, so re-enabling resumes recording by itself.
    assert vms.get(camera["id"])["recording_enabled"] is True
    vms.post(f"/api/cameras/{camera['id']}/enable")
    vms.wait_health(camera["id"], "ONLINE")
    vms.wait_recording(camera["id"], "RECORDING")


async def test_recording_one_camera_leaves_another_alone(page, simulator, vms):
    recorded_source = simulator("rec-iso-a")
    recorded = vms.add("E2E Rec Iso A", recorded_source.rtsp_url, recording_enabled=True)
    other_source = simulator("rec-iso-b")
    other = vms.add("E2E Rec Iso B", other_source.rtsp_url)
    vms.wait_health(recorded["id"], "ONLINE")
    vms.wait_health(other["id"], "ONLINE")

    await page.goto(VMS_URL)
    await wait_for_tile(
        page,
        recorded["id"],
        lambda s: s["recording"] == "RECORDING" and is_playing(s),
        what="recording and playing",
    )
    stats = await wait_for_tile(page, other["id"], is_playing, what="LIVE playback")

    # The second camera is live but explicitly not recording.
    assert stats["recording"] == "REC OFF"
    assert vms.get(other["id"])["recording_enabled"] is False


async def test_recording_survives_a_source_outage(page, simulator, vms):
    source = simulator("rec-flap")
    camera = vms.add("E2E Rec Flap", source.rtsp_url, recording_enabled=True)
    vms.wait_health(camera["id"], "ONLINE")
    vms.wait_recording(camera["id"], "RECORDING")

    await page.goto(VMS_URL)
    await wait_for_tile(page, camera["id"], is_playing, what="LIVE playback")

    source.stop()
    vms.wait_health(camera["id"], "OFFLINE")
    # Requested but unwritable: WAITING, not ERROR and not RECORDING.
    vms.wait_recording(camera["id"], "WAITING")
    await wait_for_tile(
        page,
        camera["id"],
        lambda s: s["recording"] == "WAITING",
        what="the WAITING badge",
    )

    source.start()
    vms.wait_health(camera["id"], "ONLINE")
    # No recreation and no operator retoggle.
    vms.wait_recording(camera["id"], "RECORDING", timeout=60.0)
    assert vms.get(camera["id"])["recording_enabled"] is True


async def test_adding_a_recording_camera_through_the_ui(page, simulator, vms):
    source = simulator("rec-ui")
    await page.goto(VMS_URL)

    await page.click("#add-camera")
    # The checkbox ships unchecked: recording is opt-in.
    assert await page.is_checked("#recording-enabled-input") is False
    await page.fill('#camera-form input[name="name"]', f"E2E Rec UI {TAG}")
    await page.fill("#rtsp-url-input", source.rtsp_url)
    await page.check("#recording-enabled-input")
    await page.click("#modal-submit")
    await page.wait_for_selector("#modal", state="hidden", timeout=15000)

    camera_id = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and camera_id is None:
        for item in httpx.get(f"{VMS_URL}/api/cameras", timeout=10).json():
            if item["name"] == f"E2E Rec UI {TAG}":
                camera_id = item["id"]
        await asyncio.sleep(0.5)
    assert camera_id, "the camera created through the UI never appeared"

    try:
        assert httpx.get(
            f"{VMS_URL}/api/cameras/{camera_id}", timeout=10
        ).json()["recording_enabled"] is True
        await wait_for_tile(
            page,
            camera_id,
            lambda s: s["recording"] == "RECORDING",
            what="the RECORDING badge",
        )
    finally:
        httpx.delete(f"{VMS_URL}/api/cameras/{camera_id}", timeout=10)
