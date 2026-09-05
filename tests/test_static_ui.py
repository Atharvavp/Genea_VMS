"""Static invariants of the served UI.

These assert the CSS/JS/HTML text itself, which is sound only because there is
no build or minification step. Real browser behaviour is covered by
tests/test_e2e_live_view.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text()


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "styles.css").read_text()


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "app.js").read_text()


# --- dialogs start closed ---------------------------------------------------


def test_both_dialogs_ship_hidden(html):
    assert '<div id="modal" class="modal" hidden aria-hidden="true">' in html
    assert '<div id="focus-modal" class="modal" hidden aria-hidden="true">' in html


def test_conditional_blocks_ship_hidden(html):
    for marker in ('id="form-error"', 'id="banner"', 'id="empty-state"',
                   'id="keep-source-hint"', 'id="toast"'):
        line = next(line for line in html.splitlines() if marker in line)
        assert "hidden" in line, marker


def test_hidden_is_restated_with_author_weight(css):
    # Author `display` rules outrank the user-agent [hidden] rule.
    assert "[hidden] { display: none !important; }" in css


def test_modal_is_closed_by_default_in_css(css):
    assert ".modal {\n  display: none;" in css
    assert ".modal:not([hidden]) { display: flex; }" in css
    assert ".modal[hidden] { display: none !important; }" in css


def test_body_scroll_lock_rule_exists(css):
    assert "body.modal-open { overflow: hidden; }" in css


def test_modal_visibility_goes_through_one_helper(js):
    assert "function setModalOpen(id, open)" in js
    # No direct `.hidden = ` writes on the modals outside the helper.
    assert 'el("modal").hidden =' not in js
    assert 'el("focus-modal").hidden =' not in js


def test_init_forces_a_closed_state_before_the_timer(js):
    init = js.split("function init()", 1)[1]
    before_timer = init.split("setInterval", 1)[0]
    assert 'setModalOpen("modal", false)' in before_timer
    assert 'setModalOpen("focus-modal", false)' in before_timer


def test_polling_never_opens_a_dialog(js):
    tick = js.split("async function tick()", 1)[1].split("\n}", 1)[0]
    assert "setModalOpen" not in tick
    assert "openCreator" not in tick
    assert "openEditor" not in tick


# --- assets -----------------------------------------------------------------


def test_assets_are_version_stamped(html):
    # A tab left open across a rebuild must not keep the previous CSS/JS.
    assert "/static/styles.css?v=" in html
    assert "/static/app.js?v=" in html


def test_vendored_reader_is_loaded_before_the_app(html):
    assert html.index("mediamtx-reader.js") < html.index("/static/app.js")


def test_vendored_reader_is_the_pinned_mediamtx_one():
    reader = (STATIC / "vendor" / "mediamtx-reader.js").read_text()
    assert "v1.20.1" in reader  # provenance header
    assert "class MediaMTXWebRTCReader" in reader
    assert "window.MediaMTXWebRTCReader = MediaMTXWebRTCReader;" in reader


def test_no_signalling_is_reimplemented(js):
    """The supported reader does WHEP; the app must not invent its own."""
    assert "window.MediaMTXWebRTCReader" in js
    assert "RTCPeerConnection" not in js
    assert "/whep" not in js  # the URL comes from the API, never built here


# --- player / health separation ---------------------------------------------


def test_player_states_cover_the_prd_set(js):
    for name in ("CONNECTING", "LIVE", "RECONNECTING", "ERROR"):
        assert f"{name}:" in js


def test_health_and_player_badges_are_separate_elements(html, js):
    # Grid tile template lives in app.js, focused view in index.html.
    assert 'data-role="health"' in js and 'data-role="player"' in js
    assert 'id="focus-health"' in html and 'id="focus-player"' in html


def test_player_url_comes_from_the_api(js):
    assert "new Player(camera.webrtc_url" in js
    assert "camera.webrtc_url" in js


def test_reader_errors_map_to_reconnecting_not_teardown(js):
    assert 'message.includes("retrying")' in js
    assert "PLAYER.RECONNECTING" in js


# --- player lifecycle -------------------------------------------------------


def test_each_tile_owns_an_isolated_player(js):
    assert "const players = new Map();" in js
    assert "players.set(" in js
    assert "players.delete(cameraId);" in js


def test_a_running_player_restarts_only_on_enabled_or_url_change(js):
    sync = js.split("function syncTilePlayer(camera)", 1)[1].split("\n}", 1)[0]
    assert "existing.url === camera.webrtc_url" in sync
    assert "camera.enabled" in sync
    # A rename or a health change must not appear as a restart condition.
    assert "camera.name" not in sync
    assert "camera.health" not in sync


def test_tiles_are_patched_rather_than_rebuilt(js):
    render = js.split("function renderGrid(cameras)", 1)[1].split("\n}", 1)[0]
    assert "tiles.get(camera.id)" in render
    assert "updateTile(tile, camera)" in render
    assert "grid.innerHTML" not in js  # would destroy every <video>


def test_focus_hands_the_session_over_instead_of_duplicating_it(js):
    focus = js.split("function openFocus(camera)", 1)[1].split("\n}", 1)[0]
    assert "stopPlayer(camera.id)" in focus
    close = js.split("function closeFocus()", 1)[1].split("\n}\n", 1)[0]
    assert "focusPlayer.close()" in close
    assert "syncTilePlayer(camera)" in close


def test_removing_a_camera_closes_only_its_player(js):
    render = js.split("function renderGrid(cameras)", 1)[1].split("\n}", 1)[0]
    assert "stopPlayer(cameraId)" in render
    assert "tiles.delete(cameraId)" in render


def test_video_elements_are_muted_autoplay_inline(html, js):
    assert "<video muted autoplay playsinline></video>" in js  # tile template
    assert '<video id="focus-video" muted autoplay playsinline></video>' in html


def test_tiles_keep_a_stable_size(css):
    frame = css.split(".video-frame {", 1)[1].split("}", 1)[0]
    assert "aspect-ratio: 16 / 9;" in frame


# --- credentials ------------------------------------------------------------


def test_ui_only_ever_renders_the_masked_url(js):
    assert "camera.rtsp_url_display" in js
    assert "camera.rtsp_url" not in js.replace("camera.rtsp_url_display", "")


def test_edit_form_leaves_a_credentialed_url_blank(js):
    editor = js.split("function openEditor(camera)", 1)[1].split("\n}", 1)[0]
    assert 'camera.has_credentials ? "" : camera.rtsp_url_display' in editor


def test_blank_url_is_omitted_from_the_request(js):
    submit = js.split("async function submitForm(event)", 1)[1]
    assert "if (url) payload.rtsp_url = url;" in submit


# --- scope ------------------------------------------------------------------


def test_no_simulator_coupling(html, js):
    """The VMS sees a generic RTSP URL and nothing else."""
    combined = html + js
    assert "8554" not in js  # no hard-coded simulator port in behaviour
    assert "/api/local-sources" not in combined
    assert "simulator" not in js.lower()


def test_no_out_of_scope_features(html, js):
    combined = (html + js).lower()
    for word in ("record", "playback", "timeline", "snapshot", "onvif", "ptz"):
        assert word not in combined, word


# --- served correctly -------------------------------------------------------


async def test_static_files_are_served_uncached(client):
    for path in ("/", "/static/app.js", "/static/styles.css",
                 "/static/vendor/mediamtx-reader.js"):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-store, max-age=0", path
