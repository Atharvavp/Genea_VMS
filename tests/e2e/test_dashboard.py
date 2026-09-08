"""Chromium against the real dashboard and a real in-process Component 5.

The server is the actual FastAPI app; only Component 4 and the embedding model
are fakes. Any uncaught script error fails the test that provoked it.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from datetime import timedelta

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from app.config import Settings
from app.domain.models import Direction, ObjectClass, utc_now
from app.main import create_app
from tests.fakes.component4_server import FakeComponent4
from tests.fakes.factories import FakeEmbeddingRuntime, jpeg_bytes, make_event
from tests.integration.test_discovery_indexing import Harness

XSS_NAME = '<img src=x onerror="window.__xss=1">'


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    def __init__(self, app, port: int):
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.base = f"http://127.0.0.1:{port}"

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("the test server did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=20)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("e2e")
    fake = FakeComponent4()
    events = [
        make_event(0, camera=0, camera_name="Loading Bay",
                   object_class=ObjectClass.CAR, direction=Direction.A_TO_B,
                   crossed_at=utc_now() - timedelta(minutes=30)),
        make_event(1, camera=0, camera_name="Loading Bay",
                   object_class=ObjectClass.PERSON, direction=Direction.B_TO_A,
                   crossed_at=utc_now() - timedelta(minutes=20)),
        make_event(2, camera=1, camera_name=XSS_NAME,
                   object_class=ObjectClass.BUS, direction=Direction.A_TO_B,
                   crossed_at=utc_now() - timedelta(minutes=10)),
    ]
    fake.add(events)

    async def seed() -> None:
        harness = Harness(tmp_path, fake)
        try:
            await harness.discover_all()
            await harness.index_all()
        finally:
            await harness.aclose()

    # Seeded on a dedicated thread with its own loop: pytest-asyncio's auto
    # mode may already have a loop running on this one.
    thread = threading.Thread(target=lambda: asyncio.run(seed()))
    thread.start()
    thread.join()

    settings = Settings(
        semantic_data_dir=tmp_path,
        semantic_db_path=tmp_path / "semantic.db",
        component4_api_base_url="http://component4.test:8100",
    )
    app = create_app(
        settings,
        embedding_runtime=FakeEmbeddingRuntime(),
        http_client=fake.client(),
        start_background=False,
    )
    server = Server(app, _free_port())
    server.start()
    try:
        yield server, fake, events
    finally:
        server.stop()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(args=["--no-sandbox"])
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture()
def page(browser, world):
    server, _fake, _events = world
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []

    def _console(message):
        # A deliberately provoked HTTP status (an invalid upload, an upstream
        # outage) is logged by Chromium as a console error but is not a script
        # fault. Only real script failures fail the test.
        if message.type == "error" and "Failed to load resource" not in message.text:
            errors.append(message.text)

    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", _console)
    page.goto(server.base + "/")
    page.wait_for_selector("#status-search.ok")
    yield page
    context.close()
    assert not errors, f"uncaught script errors: {errors}"


def _search_text(page, query: str):
    page.fill("#query", query)
    page.click("#search-button")
    page.wait_for_function("() => !document.getElementById('summary').textContent.includes('Searching')")


def test_the_dashboard_loads_and_reports_local_readiness(page):
    expect(page.locator("h1")).to_have_text("Semantic event search")
    expect(page.locator("#status-search")).to_have_class("pill ok")
    expect(page.locator("#status-index")).to_contain_text("3 searchable")
    expect(page.locator("#status-upstream")).to_be_visible()


def test_text_search_renders_one_card_per_event_in_score_order(page):
    _search_text(page, "a blue vehicle")
    cards = page.locator(".card")
    expect(cards).to_have_count(3)
    expect(page.locator("#summary")).to_contain_text("3 of 3 matching events")
    scores = [
        float(text) for text in page.locator(".card .score").all_text_contents()
    ]
    assert scores == sorted(scores, reverse=True)
    ids = page.evaluate(
        "() => Array.from(document.querySelectorAll('.card .meta')).map(n => n.textContent)"
    )
    assert len(ids) == 9  # three metadata lines per card, one card per event


def test_a_hostile_camera_name_renders_as_text(page):
    _search_text(page, "a bus")
    expect(page.locator(".card")).to_have_count(3)
    assert page.evaluate("() => window.__xss === undefined")
    assert page.locator("img[src='x']").count() == 0
    assert XSS_NAME in page.locator("#results").inner_text()


def test_filters_narrow_the_candidate_set(page, world):
    _server, _fake, events = world
    page.click("#filters summary")
    page.select_option("#camera", events[0].camera_id)
    _search_text(page, "anything")
    expect(page.locator("#summary")).to_contain_text("2 of 2 matching events")
    expect(page.locator(".card")).to_have_count(2)

    page.select_option("#camera", "")
    page.select_option("#klass", "bus")
    _search_text(page, "anything")
    expect(page.locator("#summary")).to_contain_text("1 of 1 matching events")


def test_a_minimum_score_can_empty_the_result_set(page):
    page.click("#filters summary")
    page.fill("#min-score", "1")
    _search_text(page, "a red car")
    expect(page.locator(".empty")).to_contain_text("No event scored above")


def test_the_detail_drawer_shows_crop_frame_and_score_breakdown(page):
    _search_text(page, "a blue vehicle")
    page.locator(".card").first.click()
    expect(page.locator("#drawer")).to_be_visible()
    body = page.locator("#drawer-body")
    expect(body.locator("img")).to_have_count(2)
    text = body.inner_text()
    assert "Crop score" in text and "Frame score" in text
    assert "Event id" in text and "evt_" in text
    page.click("#drawer-close")
    expect(page.locator("#drawer")).to_be_hidden()


def test_recording_lookup_reports_each_component4_state(page, world):
    _server, fake, _events = world
    _search_text(page, "a blue vehicle")
    page.locator(".card").first.click()

    fake.recording_status = "NOT_FOUND"
    fake.recording_reason = "no_containing_recording"
    page.get_by_role("button", name="View recording").click()
    expect(page.locator("#drawer-body .meta").last).to_contain_text("No recording covers")

    fake.recording_status = "UNAVAILABLE"
    fake.recording_reason = "vms_unreachable"
    page.get_by_role("button", name="View recording").click()
    expect(page.locator("#drawer-body .meta").last).to_contain_text("vms_unreachable")

    fake.recording_status = "AVAILABLE"
    fake.recording_reason = None
    navigated: list[str] = []
    page.context.route(
        "http://localhost:9996/**",
        lambda route: (navigated.append(route.request.url), route.abort()),
    )
    with page.context.expect_page() as opened:
        page.get_by_role("button", name="View recording").click()
    tab = opened.value
    page.wait_for_timeout(300)
    page.context.unroute("http://localhost:9996/**")
    # The browser navigated to exactly the URL Component 4 returned; this
    # service neither rewrote it nor proxied it.
    assert navigated, "the playback URL was never opened"
    assert navigated[0] == fake.recording_playback_url
    tab.close()
    page.click("#drawer-close")


def test_an_invalid_upload_is_reported_and_search_stays_usable(page):
    page.check('input[name="mode"][value="image"]')
    page.set_input_files("#image", {
        "name": "not-an-image.jpg", "mimeType": "image/jpeg",
        "buffer": b"GIF89a" + b"\x00" * 128,
    })
    page.click("#search-button")
    expect(page.locator("#message")).to_contain_text("Only JPEG and PNG")

    page.set_input_files("#image", {
        "name": "query.jpg", "mimeType": "image/jpeg", "buffer": jpeg_bytes(),
    })
    page.click("#search-button")
    expect(page.locator(".card")).to_have_count(3)


def test_image_search_returns_results(page):
    page.check('input[name="mode"][value="image"]')
    page.set_input_files("#image", {
        "name": "query.jpg", "mimeType": "image/jpeg",
        "buffer": jpeg_bytes(colour=(220, 30, 30)),
    })
    page.click("#search-button")
    expect(page.locator(".card")).to_have_count(3)
    expect(page.locator("#image-preview")).to_be_visible()


def test_a_component4_outage_shows_placeholders_while_search_keeps_working(page, world):
    _server, fake, _events = world
    fake.outage = "down"
    try:
        _search_text(page, "a blue vehicle")
        # Search itself is local and unaffected.
        expect(page.locator(".card")).to_have_count(3)
        # Only the pictures degrade; every card keeps its metadata.
        expect(page.locator(".thumb-missing").first).to_be_visible()
        expect(page.locator(".card .meta").first).not_to_be_empty()
        page.wait_for_timeout(200)
    finally:
        fake.outage = None
    # Recovery needs no page reload.
    page.reload()
    page.wait_for_selector("#status-search.ok")
    _search_text(page, "a blue vehicle")
    expect(page.locator(".card")).to_have_count(3)


def test_a_stale_response_cannot_overwrite_a_newer_search(page):
    # The first request is delayed inside the browser; the second must win.
    page.route("**/api/search/text", lambda route: (
        page.wait_for_timeout(400), route.continue_()
    ) if "__slow" not in route.request.post_data else route.continue_())
    _search_text(page, "a blue vehicle")
    expect(page.locator(".card")).to_have_count(3)
    page.unroute("**/api/search/text")

    page.click("#filters summary")
    page.select_option("#klass", "bus")
    _search_text(page, "a bus")
    expect(page.locator("#summary")).to_contain_text("1 of 1 matching events")
    expect(page.locator(".card")).to_have_count(1)


def test_the_keyboard_alone_can_run_a_search_and_open_a_result(page):
    page.locator("#query").focus()
    page.keyboard.type("a blue vehicle")
    page.keyboard.press("Enter")
    page.wait_for_selector(".card")
    page.locator(".card").first.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#drawer")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#drawer")).to_be_hidden()


def test_the_layout_is_usable_at_390_pixels(browser, world):
    server, _fake, _events = world
    context = browser.new_context(viewport={"width": 390, "height": 780})
    page = context.new_page()
    try:
        page.goto(server.base + "/")
        page.wait_for_selector("#status-search.ok")
        page.fill("#query", "a blue vehicle")
        page.click("#search-button")
        page.wait_for_selector(".card")
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0, f"the page overflows horizontally by {overflow}px"
        assert page.locator(".card").count() == 3
    finally:
        context.close()
