"""Static asset delivery, security headers, and frontend invariants."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from tests.fakes import worker as worker_fakes
from tests.fakes.app_harness import build_harness

pytestmark = pytest.mark.unit

STATIC = Path(__file__).resolve().parents[2] / "app" / "static"
HTML = (STATIC / "index.html").read_text()
JS = (STATIC / "app.js").read_text()
CSS = (STATIC / "styles.css").read_text()


@pytest.fixture(autouse=True)
def _reset():
    worker_fakes.reset()
    yield
    worker_fakes.reset()


@pytest.fixture
async def client(tmp_path: Path):
    harness = build_harness(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as http:
        async with harness.app.router.lifespan_context(harness.app):
            yield http, harness


# -- delivery ---------------------------------------------------------------


async def test_the_index_is_served_with_no_store(client):
    http, _harness = client
    response = await http.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path,content_type",
    [
        ("/static/app.js", "javascript"),
        ("/static/styles.css", "css"),
        ("/static/index.html", "html"),
    ],
)
async def test_static_assets_are_served_with_their_types(client, path, content_type):
    http, _harness = client
    response = await http.get(path)
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


async def test_the_security_headers_are_applied_to_the_page(client):
    http, _harness = client
    response = await http.get("/")
    policy = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "object-src 'none'" in policy
    assert "blob:" in policy  # snapshot object URLs
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"


# -- no external dependencies ----------------------------------------------


def test_no_external_script_or_stylesheet_is_referenced():
    for match in re.findall(r'(?:src|href)="([^"]+)"', HTML):
        assert not match.startswith(("http://", "https://", "//")), match


def test_there_is_no_build_step_artifact():
    assert "node_modules" not in HTML
    assert "require(" not in JS
    assert "webpack" not in JS


# -- accessibility and structure -------------------------------------------


@pytest.mark.parametrize(
    "element_id",
    [
        "view-cameras",
        "view-configure",
        "view-events",
        "camera-list",
        "line-canvas",
        "snapshot-image",
        "event-list",
        "event-detail",
        "camera-modal",
        "confirm-modal",
    ],
)
def test_the_required_containers_exist(element_id: str):
    assert f'id="{element_id}"' in HTML


@pytest.mark.parametrize(
    "input_id",
    ["line-ax", "line-ay", "line-bx", "line-by"],
)
def test_keyboard_numeric_coordinates_pair_with_the_canvas(input_id: str):
    assert f'id="{input_id}"' in HTML
    assert f'for="{input_id}"' in HTML


def test_every_image_element_carries_an_alt_attribute():
    for tag in re.findall(r"<img\b[^>]*>", HTML):
        assert "alt=" in tag, tag


def test_the_snapshot_and_crop_images_get_alt_text_in_script():
    assert 'alt: `Cropped' in JS or "alt: `Cropped" in JS
    assert "image.alt =" in JS


def test_the_skip_link_and_landmarks_exist():
    assert 'class="skip-link"' in HTML
    assert "<main" in HTML
    assert 'aria-label="Primary"' in HTML


# -- modal hardening --------------------------------------------------------


def test_both_dialogs_ship_hidden():
    for modal in ("camera-modal", "confirm-modal"):
        match = re.search(rf'<div class="modal" id="{modal}"([^>]*)>', HTML)
        assert match is not None
        assert "hidden" in match.group(1)


def test_the_three_css_visibility_layers_exist():
    assert "[hidden] { display: none !important; }" in CSS
    assert ".modal { display: none; }" in CSS
    assert ".modal[hidden] { display: none !important; }" in CSS


def test_one_helper_owns_dialog_visibility():
    assert JS.count("function setModalOpen(") == 1
    assert "init()" in JS
    assert 'setModalOpen("camera-modal", false)' in JS


def test_init_closes_every_dialog_before_any_timer_starts():
    init_body = JS.split("function init() {", 1)[1]
    close_index = init_body.index('setModalOpen("camera-modal", false)')
    route_index = init_body.index("applyRoute()")
    assert close_index < route_index


# -- stale-response and polling invariants ---------------------------------


def test_the_fetch_wrapper_aborts_and_generation_checks():
    assert "AbortController" in JS
    assert "requestGenerationByKey" in JS
    assert "abortControllerByKey" in JS
    assert "class StaleResponse" in JS


def test_polling_pauses_while_the_tab_is_hidden():
    assert "document.hidden" in JS
    assert "visibilitychange" in JS


def test_the_events_view_does_not_poll_automatically():
    apply_route = JS.split("function applyRoute()", 1)[1].split("\n}", 1)[0]
    events_branch = apply_route.split("} else {", 1)[1]
    assert "startPoll" not in events_branch
    assert "loadEvents" in events_branch


def test_a_route_change_stops_every_view_owned_request_and_timer():
    assert "function stopAllViewWork()" in JS
    apply_route = JS.split("function applyRoute()", 1)[1]
    assert "stopAllViewWork()" in apply_route.split("\n}", 1)[0]


def test_the_snapshot_is_not_swapped_during_a_drag():
    snapshot = JS.split("async function loadSnapshot()", 1)[1].split("\n}", 1)[0]
    assert "state.dragging" in snapshot


def test_old_snapshot_object_urls_are_revoked():
    assert "URL.revokeObjectURL" in JS


# -- security ---------------------------------------------------------------


def test_no_raw_rtsp_value_is_ever_server_rendered():
    assert "rtsp_url" not in HTML
    assert "rtsp_url_masked" in JS
    assert '"rtsp_url"' not in JS or "payload.rtsp_url" in JS


def test_names_reach_the_dom_only_through_textcontent():
    assert "innerHTML" not in JS
    assert "insertAdjacentHTML" not in JS
    assert "outerHTML" not in JS
    assert "document.write" not in JS


def test_the_frontend_never_builds_a_playback_url():
    assert "9996" not in JS
    assert "format=mp4" not in JS
    assert "/get?path=" not in JS
    assert "body.recording.playback_url" in JS


def test_the_frontend_never_talks_to_the_vms_directly():
    for match in re.findall(r'fetch\(\s*[`"\']([^`"\']+)', JS):
        assert match.startswith("/api/") or match.startswith("${") or "/api/" in match


def test_the_ui_does_not_automate_vms_discovery():
    assert "9997" not in JS and "9997" not in HTML
    assert "mediamtx" not in JS.lower()


# -- direction semantics ----------------------------------------------------


def test_the_arrow_normal_matches_the_backend_side_formula():
    """n = (-dy, dx) / |v| is the positive normal in both places."""
    assert "const nx = -dy / length;" in JS
    assert "const ny = dx / length;" in JS
    assert "classify_side" in JS  # the comment naming the backend counterpart


def test_the_legend_explains_half_planes_not_endpoints():
    assert "A side" in HTML
    assert "not movement" in HTML.replace("\n", " ")


def test_the_minimum_line_length_matches_the_backend():
    from app.domain.models import MIN_LINE_LENGTH

    assert f"const MIN_LINE_LENGTH = {MIN_LINE_LENGTH};" in JS


# -- copy -------------------------------------------------------------------


def test_the_empty_state_explains_the_manual_vms_mapping():
    assert "cam_xxxxxxxx" in HTML
    assert "host.docker.internal:8555" in HTML
    assert "never discovers VMS cameras" in HTML


def test_the_delete_confirmation_says_history_is_kept():
    assert "event history and images are kept" in JS


def test_no_out_of_scope_capability_is_advertised():
    lowered = (HTML + JS).lower()
    for word in ("deepstream", "kafka", "kubernetes", "re-identification", "embedding"):
        assert word not in lowered
