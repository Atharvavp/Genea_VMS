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
    """Recording and historical playback are in scope now; these are not."""
    combined = (html + js).lower()
    for word in ("timeline", "snapshot", "motion", "onvif", "ptz", "transcode"):
        assert word not in combined, word


# --- recording controls -----------------------------------------------------


def test_the_add_form_offers_recording_and_defaults_it_off(html, js):
    assert 'id="recording-enabled-input"' in html
    line = next(
        line for line in html.splitlines() if 'id="recording-enabled-input"' in line
    )
    assert "checked" not in line  # never recording unless asked for
    creator = js.split("function openCreator()", 1)[1].split("\n}", 1)[0]
    assert 'el("recording-enabled-input").checked = false' in creator


def test_the_edit_form_shows_the_stored_recording_preference(js):
    editor = js.split("function openEditor(camera)", 1)[1].split("\n}", 1)[0]
    assert "camera.recording_enabled" in editor


def test_the_form_sends_the_recording_preference(js):
    submit = js.split("async function submitForm(event)", 1)[1]
    assert "recording_enabled: recordingEnabled" in submit


def test_the_recording_toggle_uses_the_existing_patch_route(js):
    action = js.split('if (action === "record")', 1)[1].split("\n    }", 1)[0]
    assert '`/api/cameras/${camera.id}`' in action
    assert '"PATCH"' in action
    assert "recording_enabled: !camera.recording_enabled" in action
    # Toggling recording must never touch enable/disable.
    assert "/disable" not in action and "/enable" not in action


def test_recording_badges_cover_the_four_states(js):
    for state in ("DISABLED", "WAITING", "RECORDING", "ERROR"):
        assert f"{state}:" in js
    for label in ("REC OFF", "WAITING", "RECORDING", "REC ERROR"):
        assert label in js


def test_recording_is_a_third_badge_separate_from_health_and_player(html, js):
    assert 'data-role="recording"' in js
    assert 'data-role="health"' in js and 'data-role="player"' in js
    assert 'id="focus-recording"' in html


def test_a_running_player_does_not_restart_when_recording_changes(js):
    """Toggling recording must not interrupt anyone's live view."""
    sync = js.split("function syncTilePlayer(camera)", 1)[1].split("\n}", 1)[0]
    assert "recording" not in sync
    assert "existing.url === camera.webrtc_url" in sync


# --- recordings dialog ------------------------------------------------------


def test_the_recordings_dialog_ships_hidden(html):
    assert '<div id="recordings-modal" class="modal" hidden aria-hidden="true">' in html


def test_every_recordings_state_block_ships_hidden(html):
    for marker in ('id="recordings-loading"', 'id="recordings-empty"',
                   'id="recordings-error"', 'id="recordings-list"',
                   'id="recordings-player"'):
        line = next(line for line in html.splitlines() if marker in line)
        assert "hidden" in line, marker


def test_the_recordings_dialog_goes_through_the_one_visibility_helper(js):
    assert 'setModalOpen("recordings-modal"' in js
    assert 'el("recordings-modal").hidden =' not in js


def test_init_closes_the_recordings_dialog_before_the_timer(js):
    init = js.split("function init()", 1)[1]
    before_timer = init.split("setInterval", 1)[0]
    assert 'setModalOpen("recordings-modal", false)' in before_timer


def test_polling_never_opens_or_reloads_the_recordings_dialog(js):
    """The 2 s poll must not disturb a list being read or a video playing."""
    tick = js.split("async function tick()", 1)[1].split("\n}", 1)[0]
    assert "openRecordings" not in tick
    assert "loadRecordings" not in tick
    refresh = js.split("async function refresh()", 1)[1].split("\n}", 1)[0]
    assert "loadRecordings" not in refresh
    assert "recordings" not in refresh


def test_history_states_cover_the_four_the_dialog_can_be_in(js):
    for name in ("LOADING", "AVAILABLE", "EMPTY", "UNAVAILABLE"):
        assert f'"{name}"' in js


def test_a_history_failure_stays_in_the_dialog(js):
    """A playback outage must not be rendered as a camera recording error."""
    loader = js.split("async function loadRecordings()", 1)[1].split("\n}", 1)[0]
    assert 'setHistoryState("UNAVAILABLE"' in loader
    # It must not write camera state or re-render the grid.
    assert "renderGrid" not in loader
    assert "updateTile" not in loader


def test_the_playback_url_comes_from_the_api(js):
    """The frontend must not know the playback host, path shape or query."""
    assert "item.playback_url" in js
    assert "video.src = item.playback_url" in js
    assert "9996" not in js
    assert "/get?path=" not in js
    assert "format=mp4" not in js


def test_the_recordings_request_is_by_camera_id_and_date(js):
    loader = js.split("async function loadRecordings()", 1)[1].split("\n}", 1)[0]
    assert "/api/recordings?camera_id=" in loader
    assert "encodeURIComponent(cameraId)" in loader
    assert "encodeURIComponent(date)" in loader
    # No MediaMTX path is ever sent by the client.
    assert "mediamtx_path" not in loader


def test_the_date_defaults_to_the_current_utc_day(js):
    assert 'new Date().toISOString().slice(0, 10)' in js
    opener = js.split("function openRecordings(camera)", 1)[1].split("\n}", 1)[0]
    assert "todayUtc()" in opener


def test_historical_playback_uses_native_controls(html, js):
    element = next(
        line for line in html.splitlines() if 'id="recordings-video"' in line
    )
    assert "controls" in element
    assert 'preload="metadata"' in element
    assert "playsinline" in element
    # No custom transport: the browser's own controls do play/pause/seek.
    assert "MediaMTXWebRTCReader" not in js.split(
        "function playRecording(item, list)", 1
    )[1].split("\n}", 1)[0]


def test_the_history_player_is_not_a_webrtc_player(js):
    play = js.split("function playRecording(item, list)", 1)[1].split("\n}", 1)[0]
    assert "new Player(" not in play
    assert "srcObject" not in play


def test_closing_the_recordings_dialog_stops_the_media(js):
    close = js.split("function clearRecordingPlayer()", 1)[1].split("\n}", 1)[0]
    assert "video.pause()" in close
    assert 'video.removeAttribute("src")' in close


def test_disabled_cameras_cannot_open_the_recordings_dialog(js):
    update = js.split("function updateTile(tile, camera)", 1)[1].split("\n}", 1)[0]
    assert 'recordings.disabled = !camera.enabled' in update


def test_recording_rows_are_escaped(js):
    render = js.split("function renderRecordings(items)", 1)[1].split("\n}", 1)[0]
    assert "escapeHtml(" in render


def test_recording_styles_exist(css):
    assert ".badge-recording.recording {" in css
    assert ".recording-row.selected {" in css
    assert ".recordings-list {" in css


# --- served correctly -------------------------------------------------------


async def test_static_files_are_served_uncached(client):
    for path in ("/", "/static/app.js", "/static/styles.css",
                 "/static/vendor/mediamtx-reader.js"):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-store, max-age=0", path
