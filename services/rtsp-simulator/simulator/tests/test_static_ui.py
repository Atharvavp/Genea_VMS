"""Regression tests for the single-page UI's static contract.

These lock down the specific defect where author CSS (`.modal { display: flex }`,
`label { display: block }`) outranked the user-agent `[hidden]` rule, leaving the
Add Camera dialog and every conditional form field permanently visible. There is
no browser test framework in this repo, so these assert the served CSS/JS/HTML
contract that the fix depends on; the visual behaviour is checked manually.
"""

from __future__ import annotations

ASSET_VERSION = "v=modal-visibility-20260904"


def get_text(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    return response.text


# --- initial markup -----------------------------------------------------


def test_modal_markup_starts_closed(client) -> None:
    html = get_text(client, "/")
    assert '<div id="modal" class="modal" hidden aria-hidden="true">' in html


def test_conditional_blocks_start_hidden(client) -> None:
    html = get_text(client, "/")
    for marker in (
        'id="banner" class="banner" hidden',
        'id="empty-state" class="empty" hidden',
        'class="radio" id="keep-source-option" hidden',
        'id="source-local" class="source-panel" hidden',
        'id="keep-source-info" class="hint" hidden',
        'id="form-error" class="form-error" hidden',
    ):
        assert marker in html, marker
    # The fixed-value inputs are hidden until their mode selects "fixed".
    assert html.count('class="resolution-fixed" hidden') == 2
    assert html.count('class="fps-fixed" hidden') == 1
    assert html.count('class="bitrate-fixed" hidden') == 1


def test_assets_are_versioned(client) -> None:
    html = get_text(client, "/")
    assert f'href="/static/styles.css?{ASSET_VERSION}"' in html
    assert f'src="/static/app.js?{ASSET_VERSION}"' in html


# --- CSS visibility contract -------------------------------------------


def test_hidden_attribute_beats_author_display_rules(client) -> None:
    css = get_text(client, "/static/styles.css")
    assert "[hidden] { display: none !important; }" in css


def test_modal_is_closed_by_default_without_relying_on_the_generic_rule(client) -> None:
    css = get_text(client, "/static/styles.css")
    modal_block = css.split(".modal {", 1)[1].split("}", 1)[0]
    assert "display: none;" in modal_block
    assert "display: flex" not in modal_block
    assert ".modal:not([hidden]) { display: flex; }" in css
    assert ".modal[hidden] { display: none !important; }" in css


def test_open_modal_locks_the_page_behind_it(client) -> None:
    assert "body.modal-open { overflow: hidden; }" in get_text(client, "/static/styles.css")


# --- JS state contract --------------------------------------------------


def test_modal_state_goes_through_the_helpers(client) -> None:
    js = get_text(client, "/static/app.js")
    assert "function isModalOpen()" in js
    assert "function setModalOpen(open)" in js
    assert "setModalOpen(true);" in js
    assert "setModalOpen(false);" in js
    # No direct writes to the attribute are left anywhere.
    assert 'el("modal").hidden =' not in js


def test_init_forces_a_deterministic_closed_state(client) -> None:
    js = get_text(client, "/static/app.js")
    init_body = js.split("function init() {", 1)[1]
    assert "setModalOpen(false);" in init_body.split("el(\"add-camera\")", 1)[0]
    assert "syncConditionalFields();" in init_body.split("el(\"add-camera\")", 1)[0]


def test_polling_never_opens_the_modal(client) -> None:
    js = get_text(client, "/static/app.js")
    tick_body = js.split("function tick() {", 1)[1].split("}", 1)[0]
    assert "!isModalOpen()" in tick_body
    assert "openModal" not in tick_body


# --- cache headers ------------------------------------------------------


def test_ui_assets_are_served_uncached(client) -> None:
    for path in ("/", "/static/styles.css", "/static/app.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, max-age=0", path


def test_api_responses_are_not_touched_by_the_ui_cache_rule(client) -> None:
    for path in ("/api/cameras", "/health"):
        assert "no-store" not in client.get(path).headers.get("cache-control", "")
