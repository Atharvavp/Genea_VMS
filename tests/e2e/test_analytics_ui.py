"""Browser scenarios for the three P0 views (PLAN section 23.6)."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import httpx
import pytest

from app.domain.models import utc_now
from tests.fakes import factories

pytestmark = pytest.mark.e2e

CAMERA = {
    "vms_camera_id": "cam_0123abcd",
    "name": "Loading Bay",
    "rtsp_url": "rtsp://admin:hunter2@10.0.0.9:554/live",
}
SECRET = "hunter2"


def create_camera(server, **over) -> dict:
    payload = dict(CAMERA)
    payload.update(over)
    response = httpx.post(f"{server.base_url}/api/cameras", json=payload, timeout=20)
    assert response.status_code == 201, response.text
    return response.json()


def seed_event(server, camera_id: str, **over):
    candidate = factories.candidate(
        camera_id=camera_id,
        crossed_at=over.pop("crossed_at", utc_now()),
        **over,
    )
    result = server.storage.commit_event(candidate)
    return server.events.get(result.event_id)


# -- cameras view -----------------------------------------------------------


def test_the_empty_state_explains_the_manual_mapping(ui, server):
    ui.goto(f"{server.base_url}/#/cameras")
    empty = ui.locator("#cameras-empty")
    empty.wait_for(state="visible")
    assert "cam_xxxxxxxx" in empty.inner_text()
    assert "never discovers VMS cameras" in empty.inner_text()


def test_add_a_camera_through_the_dialog(ui, server):
    ui.goto(f"{server.base_url}/#/cameras")
    assert ui.locator("#camera-modal").is_hidden()

    ui.click("#add-camera")
    ui.locator("#camera-modal").wait_for(state="visible")
    ui.fill("#camera-vms-id", "cam_0123abcd")
    ui.fill("#camera-name", "Loading Bay")
    ui.fill("#camera-rtsp", "rtsp://host.docker.internal:8555/vms_cam_0123abcd")
    ui.click("#camera-submit")

    ui.locator("#camera-modal").wait_for(state="hidden")
    ui.locator(".camera-card").wait_for(state="visible")
    assert "Loading Bay" in ui.locator(".camera-card").inner_text()


def test_a_validation_error_is_shown_and_the_dialog_stays_open(ui, server):
    ui.goto(f"{server.base_url}/#/cameras")
    ui.click("#add-camera")
    ui.fill("#camera-vms-id", "not-a-vms-id")
    ui.fill("#camera-name", "Bad")
    ui.fill("#camera-rtsp", "rtsp://cam/live")
    ui.click("#camera-submit")
    error = ui.locator("#camera-error")
    error.wait_for(state="visible")
    assert "vms_camera_id" in error.inner_text()
    assert ui.locator("#camera-modal").is_visible()


def test_runtime_badges_reflect_worker_state(ui, server):
    camera = create_camera(server)
    ui.goto(f"{server.base_url}/#/cameras")
    badge = ui.locator(f'[data-testid="state-{camera["id"]}"]')
    badge.wait_for(state="visible")
    assert badge.get_attribute("data-state") in {"STARTING", "RUNNING"}

    server.make_running(camera["id"])
    ui.wait_for_function(
        "id => document.querySelector(`[data-testid=\"state-${id}\"]`)"
        "?.dataset.state === 'RUNNING'",
        arg=camera["id"],
    )
    assert badge.get_attribute("data-state") == "RUNNING"


def test_disable_and_enable_round_trip(ui, server):
    camera = create_camera(server)
    ui.goto(f"{server.base_url}/#/cameras")
    toggle = ui.locator(f'[data-testid="toggle-{camera["id"]}"]')
    toggle.wait_for(state="visible")
    assert toggle.inner_text() == "Disable"

    toggle.click()
    ui.wait_for_function(
        "id => document.querySelector(`[data-testid=\"state-${id}\"]`)"
        "?.dataset.state === 'DISABLED'",
        arg=camera["id"],
    )
    assert (
        httpx.get(f"{server.base_url}/api/cameras/{camera['id']}").json()["enabled"]
        is False
    )

    ui.locator(f'[data-testid="toggle-{camera["id"]}"]').click()
    ui.wait_for_function(
        "id => document.querySelector(`[data-testid=\"toggle-${id}\"]`)"
        "?.textContent === 'Disable'",
        arg=camera["id"],
    )


def test_delete_confirms_and_says_history_is_kept(ui, server):
    camera = create_camera(server)
    ui.goto(f"{server.base_url}/#/cameras")
    ui.locator(f'[data-testid="delete-{camera["id"]}"]').click()
    dialog = ui.locator("#confirm-modal")
    dialog.wait_for(state="visible")
    assert "event history and images are kept" in ui.locator("#confirm-text").inner_text()
    ui.click("#confirm-yes")
    ui.locator("#cameras-empty").wait_for(state="visible")


def test_credentials_never_appear_in_the_rendered_page(ui, server):
    create_camera(server)
    ui.goto(f"{server.base_url}/#/cameras")
    ui.locator(".camera-card").wait_for(state="visible")
    content = ui.content()
    assert SECRET not in content
    assert "***:***@10.0.0.9:554/live" in content


def test_a_camera_name_containing_markup_renders_as_text(ui, server):
    create_camera(server, name="<img src=x onerror=alert(1)>Bay")
    ui.goto(f"{server.base_url}/#/cameras")
    card = ui.locator(".camera-card h3")
    card.wait_for(state="visible")
    assert card.inner_text() == "<img src=x onerror=alert(1)>Bay"
    assert ui.locator(".camera-card h3 img").count() == 0


def test_polling_pauses_while_the_tab_is_hidden(ui, server):
    create_camera(server)
    ui.goto(f"{server.base_url}/#/cameras")
    ui.locator(".camera-card").wait_for(state="visible")

    requests: list[str] = []
    ui.on("request", lambda request: requests.append(request.url))
    ui.evaluate(
        """() => {
            Object.defineProperty(document, 'hidden', {value: true, configurable: true});
            document.dispatchEvent(new Event('visibilitychange'));
        }"""
    )
    ui.wait_for_timeout(3000)
    hidden_calls = [url for url in requests if url.endswith("/api/cameras")]
    assert len(hidden_calls) <= 1, f"polling continued while hidden: {hidden_calls}"


# -- configure view ---------------------------------------------------------


def test_draw_save_and_reload_a_line(ui, server):
    camera = create_camera(server)
    server.publish_frame(camera["id"])
    ui.goto(f"{server.base_url}/#/cameras/{camera['id']}/configure")
    ui.locator("#snapshot-image").wait_for(state="visible")
    ui.wait_for_function(
        "() => document.querySelector('#snapshot-image').naturalWidth > 0"
    )

    canvas = ui.locator("#line-canvas")
    box = canvas.bounding_box()
    ui.mouse.move(box["x"] + box["width"] * 0.2, box["y"] + box["height"] * 0.5)
    ui.mouse.down()
    ui.mouse.move(box["x"] + box["width"] * 0.8, box["y"] + box["height"] * 0.5, steps=8)
    ui.mouse.up()

    assert float(ui.input_value("#line-ax")) == pytest.approx(0.2, abs=0.05)
    assert float(ui.input_value("#line-bx")) == pytest.approx(0.8, abs=0.05)

    ui.fill("#line-name", "Entry line")
    ui.select_option("#line-direction", "A_TO_B")
    ui.click("#line-save")
    ui.locator("#line-note").wait_for(state="visible")
    assert "tracking session" in ui.locator("#line-note").inner_text()

    stored = httpx.get(f"{server.base_url}/api/cameras/{camera['id']}/line").json()
    assert stored["direction"] == "A_TO_B"
    assert stored["a"]["x"] == pytest.approx(0.2, abs=0.05)

    ui.reload()
    ui.locator("#snapshot-image").wait_for(state="visible")
    ui.wait_for_function(
        "() => document.querySelector('#line-ax').value !== ''"
    )
    assert float(ui.input_value("#line-ax")) == pytest.approx(stored["a"]["x"], abs=0.002)
    assert ui.input_value("#line-direction") == "A_TO_B"


def test_a_too_short_line_is_rejected_in_the_browser(ui, server):
    camera = create_camera(server)
    server.publish_frame(camera["id"])
    ui.goto(f"{server.base_url}/#/cameras/{camera['id']}/configure")
    ui.locator("#line-canvas").wait_for(state="visible")

    box = ui.locator("#line-canvas").bounding_box()
    ui.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    ui.mouse.down()
    ui.mouse.move(box["x"] + box["width"] * 0.51, box["y"] + box["height"] * 0.5, steps=3)
    ui.mouse.up()

    error = ui.locator("#line-error")
    error.wait_for(state="visible")
    assert "0.05" in error.inner_text()


def test_the_keyboard_only_coordinate_path_works(ui, server):
    camera = create_camera(server)
    server.publish_frame(camera["id"])
    ui.goto(f"{server.base_url}/#/cameras/{camera['id']}/configure")
    ui.locator("#line-canvas").wait_for(state="visible")

    for field, value in (
        ("#line-ax", "0.100"),
        ("#line-ay", "0.200"),
        ("#line-bx", "0.900"),
        ("#line-by", "0.800"),
    ):
        ui.fill(field, value)
        ui.dispatch_event(field, "change")
    ui.fill("#line-name", "Keyboard line")
    ui.select_option("#line-direction", "BOTH")
    ui.click("#line-save")
    ui.locator("#line-note").wait_for(state="visible")

    stored = httpx.get(f"{server.base_url}/api/cameras/{camera['id']}/line").json()
    assert stored["a"] == {"x": 0.1, "y": 0.2}
    assert stored["b"] == {"x": 0.9, "y": 0.8}
    assert stored["direction"] == "BOTH"


def test_the_direction_arrow_points_into_the_b_half_plane(ui, server):
    """The overlay's normal must agree with classify_side() in the backend."""
    camera = create_camera(server)
    server.publish_frame(camera["id"])
    ui.goto(f"{server.base_url}/#/cameras/{camera['id']}/configure")
    ui.locator("#line-canvas").wait_for(state="visible")

    for field, value in (
        ("#line-ax", "0.200"),
        ("#line-ay", "0.500"),
        ("#line-bx", "0.800"),
        ("#line-by", "0.500"),
    ):
        ui.fill(field, value)
        ui.dispatch_event(field, "change")
    ui.select_option("#line-direction", "A_TO_B")
    ui.dispatch_event("#line-direction", "change")

    # The arrow tip is drawn at midpoint + n*k with n = (-dy, dx)/|v|.
    # For A left-to-right that is straight down the screen, which is the B side.
    tip = ui.evaluate(
        """() => {
            const canvas = document.getElementById('line-canvas');
            const context = canvas.getContext('2d');
            const w = canvas.width, h = canvas.height;
            const above = context.getImageData(Math.round(w * 0.5) - 2,
                Math.round(h * 0.5) - 30, 4, 4).data;
            const below = context.getImageData(Math.round(w * 0.5) - 2,
                Math.round(h * 0.5) + 26, 4, 4).data;
            const ink = (d) => { let n = 0; for (let i = 3; i < d.length; i += 4)
                if (d[i] > 0) n++; return n; };
            return {above: ink(above), below: ink(below)};
        }"""
    )
    assert tip["below"] > tip["above"], (
        "the A_TO_B arrow must be drawn towards the B half-plane (screen-down "
        f"for a left-to-right line); got {tip}"
    )


def test_snapshot_not_ready_shows_a_waiting_message(ui, server):
    camera = create_camera(server)
    ui.goto(f"{server.base_url}/#/cameras/{camera['id']}/configure")
    status = ui.locator("#snapshot-status")
    status.wait_for(state="visible")
    ui.wait_for_function(
        "() => document.querySelector('#snapshot-status').textContent.includes('Waiting')"
    )


def test_deleting_the_camera_while_configuring_returns_to_the_list(ui, server):
    camera = create_camera(server)
    server.publish_frame(camera["id"])
    ui.goto(f"{server.base_url}/#/cameras/{camera['id']}/configure")
    ui.locator("#snapshot-image").wait_for(state="visible")
    httpx.delete(f"{server.base_url}/api/cameras/{camera['id']}", timeout=20)
    ui.wait_for_function("() => window.location.hash === '#/cameras'", timeout=20000)


# -- events view ------------------------------------------------------------


def test_events_list_filter_and_detail(ui, server):
    camera = create_camera(server)
    for index in range(3):
        seed_event(
            server,
            camera["id"],
            track_id=index,
            crossed_at=utc_now() - timedelta(seconds=index),
        )
    ui.goto(f"{server.base_url}/#/events")
    ui.locator(".event-row").first.wait_for(state="visible")
    assert ui.locator(".event-row").count() == 3

    ui.select_option("#filter-direction", "B_TO_A")
    ui.click("#event-filters button[type=submit]")
    ui.locator("#events-empty").wait_for(state="visible")

    ui.select_option("#filter-direction", "A_TO_B")
    ui.click("#event-filters button[type=submit]")
    ui.locator(".event-row").first.wait_for(state="visible")

    ui.locator(".event-row").first.click()
    ui.locator("#event-detail").wait_for(state="visible")
    assert "truck" in ui.locator("#detail-title").inner_text()
    ui.wait_for_function(
        "() => document.querySelector('#detail-frame').naturalWidth > 0"
    )


def test_load_more_never_repeats_an_event(ui, server):
    camera = create_camera(server)
    for index in range(30):
        seed_event(
            server,
            camera["id"],
            track_id=index,
            crossed_at=utc_now() - timedelta(seconds=index),
        )
    ui.goto(f"{server.base_url}/#/events")
    ui.locator(".event-row").first.wait_for(state="visible")
    assert ui.locator(".event-row").count() == 25
    ui.click("#events-more")
    ui.wait_for_function("() => document.querySelectorAll('.event-row').length === 30")
    ids = ui.eval_on_selector_all(
        ".event-row", "nodes => nodes.map(node => node.dataset.eventId)"
    )
    assert len(ids) == len(set(ids)) == 30


def test_a_missing_full_frame_falls_back_to_a_message(ui, server):
    camera = create_camera(server)
    record = seed_event(server, camera["id"])
    (server.data_root / record.snapshot_path).unlink()
    ui.goto(f"{server.base_url}/#/events")
    ui.locator(".event-row").first.click()
    note = ui.locator("#detail-image-note")
    note.wait_for(state="visible")
    assert "no longer available" in note.inner_text()


def test_events_of_a_deleted_camera_remain_browsable(ui, server):
    camera = create_camera(server)
    seed_event(server, camera["id"])
    httpx.delete(f"{server.base_url}/api/cameras/{camera['id']}", timeout=20)
    ui.goto(f"{server.base_url}/#/events")
    ui.locator(".event-row").first.wait_for(state="visible")
    assert "Loading Bay" in ui.locator(".event-row").first.inner_text()


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("not_found", "No VMS recording contains this timestamp"),
        ("unavailable", "recording service is unavailable"),
    ],
)
def test_recording_lookup_failure_states(ui, server, mode, expected):
    camera = create_camera(server)
    seed_event(server, camera["id"])
    server.recordings.mode = mode
    ui.goto(f"{server.base_url}/#/events")
    ui.locator(".event-row").first.click()
    ui.locator("#event-detail").wait_for(state="visible")
    ui.click("#detail-recording")
    status = ui.locator("#recording-status")
    status.wait_for(state="visible")
    ui.wait_for_function(
        "text => document.querySelector('#recording-status').textContent.includes(text)",
        arg=expected,
    )
    # The event display itself is unchanged by the lookup failure.
    assert "truck" in ui.locator("#detail-title").inner_text()


def test_an_available_recording_opens_the_url_the_vms_returned(ui, server):
    camera = create_camera(server)
    seed_event(server, camera["id"])
    server.recordings.mode = "available"
    ui.goto(f"{server.base_url}/#/events")
    ui.locator(".event-row").first.click()
    ui.locator("#event-detail").wait_for(state="visible")

    opened: list[str] = []
    ui.expose_function("recordOpen", lambda url: opened.append(url))
    ui.evaluate("() => { window.open = (url) => { window.recordOpen(url); }; }")
    ui.click("#detail-recording")
    ui.wait_for_function(
        "() => document.querySelector('#recording-status')"
        ".textContent.includes('Recording found')"
    )
    assert opened == ["http://127.0.0.1:9996/get?path=vms_cam_0123abcd"]


def test_a_late_event_response_cannot_overwrite_a_newer_selection(ui, server):
    camera = create_camera(server)
    first = seed_event(server, camera["id"], track_id=1, crossed_at=utc_now())
    second = seed_event(
        server,
        camera["id"],
        track_id=2,
        crossed_at=utc_now() - timedelta(seconds=5),
    )
    ui.goto(f"{server.base_url}/#/events")
    ui.locator(".event-row").first.wait_for(state="visible")

    ui.route(
        f"**/api/events/{first.id}",
        lambda route: (route.request, __import__("time").sleep(1.2), route.continue_())[
            -1
        ],
    )
    ui.locator(f'[data-testid="event-{first.id}"]').click()
    ui.locator(f'[data-testid="event-{second.id}"]').click()
    ui.wait_for_timeout(2500)
    assert (
        ui.locator(f'[data-testid="event-{second.id}"]').get_attribute("aria-pressed")
        == "true"
    )
    assert (
        ui.locator(f'[data-testid="event-{first.id}"]').get_attribute("aria-pressed")
        == "false"
    )


# -- resilience -------------------------------------------------------------


def test_a_network_outage_shows_one_banner_and_recovers(ui, server):
    create_camera(server)
    ui.goto(f"{server.base_url}/#/cameras")
    ui.locator(".camera-card").wait_for(state="visible")

    ui.route("**/api/cameras", lambda route: route.abort())
    ui.locator("#connection-banner").wait_for(state="visible", timeout=20000)
    assert "Cannot reach" in ui.locator("#connection-banner-text").inner_text()

    ui.unroute("**/api/cameras")
    ui.locator("#connection-banner").wait_for(state="hidden", timeout=20000)
    assert ui.locator(".camera-card").count() == 1


def test_the_narrow_viewport_stays_usable(ui, server):
    camera = create_camera(server)
    seed_event(server, camera["id"])
    ui.set_viewport_size({"width": 390, "height": 780})
    ui.goto(f"{server.base_url}/#/cameras")
    ui.locator(".camera-card").wait_for(state="visible")
    overflow = ui.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"the page scrolls horizontally by {overflow}px"

    ui.goto(f"{server.base_url}/#/events")
    ui.locator(".event-row").first.wait_for(state="visible")
    overflow = ui.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1


def test_navigating_away_stops_the_previous_view_polling(ui, server):
    create_camera(server)
    ui.goto(f"{server.base_url}/#/cameras")
    ui.locator(".camera-card").wait_for(state="visible")

    ui.goto(f"{server.base_url}/#/events")
    ui.wait_for_timeout(500)
    requests: list[str] = []
    ui.on("request", lambda request: requests.append(request.url))
    ui.wait_for_timeout(3000)
    polls = [url for url in requests if re.search(r"/api/cameras$", url)]
    assert len(polls) <= 1, f"the cameras poll kept running: {polls}"
