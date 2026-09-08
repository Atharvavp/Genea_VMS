"""Adapter contract: parsing, isolation of malformed items, and error mapping."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.domain.models import RepresentationKind, utc_now
from app.integrations.component4 import (
    Component4Client,
    InvalidCursor,
    UpstreamContractError,
    UpstreamEventNotFound,
    UpstreamGone,
    UpstreamRejected,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from tests.fakes.component4_server import FakeComponent4
from tests.fakes.factories import event_payload, make_event, make_events

BASE = "http://component4.test:8100"


def _client(fake: FakeComponent4) -> Component4Client:
    return Component4Client(base_url=BASE, client=fake.client())


async def test_a_page_is_parsed_into_event_records():
    fake = FakeComponent4()
    fake.add(make_events(3))
    page = await _client(fake).list_events()
    assert len(page.events) == 3
    assert page.malformed == 0
    assert page.events[0].crossed_at > page.events[-1].crossed_at


async def test_one_malformed_item_does_not_discard_its_valid_peers():
    fake = FakeComponent4()
    fake.add(make_events(2))
    fake.inject_items = [
        {"id": "not-an-event"},
        event_payload(make_event(99), crossed_at="not-a-time"),
        event_payload(make_event(98), object_class="unicorn"),
        event_payload(make_event(97), crop_url="http://evil.test/steal"),
        "a string, not an object",
    ]
    page = await _client(fake).list_events()
    assert len(page.events) == 2
    assert page.malformed == 5
    assert page.raw_count == 7


async def test_a_declared_artifact_path_pointing_elsewhere_is_rejected():
    fake = FakeComponent4()
    record = make_event(1)
    fake.inject_items = [
        event_payload(record, frame_url=f"http://elsewhere/api/events/{record.event_id}/frame")
    ]
    page = await _client(fake).list_events()
    assert page.events == () and page.malformed == 1


async def test_a_malformed_envelope_is_a_contract_error_not_an_empty_page():
    fake = FakeComponent4()
    fake.malformed_envelope = True
    with pytest.raises(UpstreamContractError):
        await _client(fake).list_events()


async def test_cursor_traversal_yields_every_event_exactly_once():
    fake = FakeComponent4()
    fake.add(make_events(250))
    client = _client(fake)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = await client.list_events(cursor=cursor)
        seen.extend(event.event_id for event in page.events)
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
    assert pages == 3
    assert len(seen) == 250 == len(set(seen))


async def test_duplicate_timestamps_do_not_break_pagination():
    fake = FakeComponent4()
    moment = utc_now()
    fake.add([make_event(index, crossed_at=moment) for index in range(150)])
    client = _client(fake)
    seen: set[str] = set()
    cursor = None
    while True:
        page = await client.list_events(cursor=cursor)
        seen.update(event.event_id for event in page.events)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == 150


async def test_a_stale_cursor_is_reported_as_invalid_cursor():
    fake = FakeComponent4()
    fake.add(make_events(150))
    client = _client(fake)
    first = await client.list_events()
    fake.cursor_epoch = "epoch-2"  # upstream restarted with a new signing key
    with pytest.raises(InvalidCursor):
        await client.list_events(cursor=first.next_cursor)


async def test_a_from_filter_is_sent_as_an_exact_utc_instant():
    fake = FakeComponent4()
    events = make_events(5, step_seconds=3600)
    fake.add(events)
    page = await _client(fake).list_events(from_=events[3].crossed_at)
    assert {event.event_id for event in page.events} == {
        events[3].event_id, events[4].event_id
    }
    assert "from=2026-09-07" in fake.request_log[-1]


async def test_artifacts_are_fetched_from_the_fixed_base_and_validated():
    fake = FakeComponent4()
    record = make_event(1)
    fake.add([record])
    payload = await _client(fake).fetch_artifact(record.event_id, RepresentationKind.CROP)
    assert payload.startswith(b"\xff\xd8\xff")
    assert f"crop:{record.event_id}" in fake.request_log


async def test_a_gone_artifact_is_permanent_and_a_missing_event_is_permanent():
    fake = FakeComponent4()
    records = make_events(2)
    fake.add(records)
    fake.gone_artifacts.add(records[0].event_id)
    fake.missing_artifacts.add(records[1].event_id)
    client = _client(fake)
    with pytest.raises(UpstreamGone) as gone:
        await client.fetch_artifact(records[0].event_id, "crop")
    assert gone.value.retryable is False
    with pytest.raises(UpstreamEventNotFound) as missing:
        await client.fetch_artifact(records[1].event_id, "crop")
    assert missing.value.retryable is False


async def test_a_transient_5xx_is_retryable():
    fake = FakeComponent4()
    record = make_event(1)
    fake.add([record])
    fake.transient_artifacts[record.event_id] = 1
    client = _client(fake)
    with pytest.raises(UpstreamRejected) as exc:
        await client.fetch_artifact(record.event_id, "crop")
    assert exc.value.retryable is True
    assert (await client.fetch_artifact(record.event_id, "crop")).startswith(b"\xff\xd8")


async def test_non_jpeg_and_oversized_artifacts_are_refused():
    fake = FakeComponent4()
    records = make_events(2)
    fake.add(records)
    fake.garbage_artifacts.add(records[0].event_id)
    fake.oversized_artifacts.add(records[1].event_id)
    client = _client(fake)
    with pytest.raises(UpstreamContractError):
        await client.fetch_artifact(records[0].event_id, "crop")
    with pytest.raises(UpstreamContractError):
        await client.fetch_artifact(records[1].event_id, "frame")


async def test_an_outage_maps_to_timeout_or_unreachable():
    fake = FakeComponent4()
    fake.add(make_events(1))
    client = _client(fake)
    fake.outage = "timeout"
    with pytest.raises(UpstreamTimeout):
        await client.list_events()
    fake.outage = "down"
    with pytest.raises(UpstreamUnavailable):
        await client.list_events()
    fake.outage = "500"
    with pytest.raises(UpstreamRejected):
        await client.list_events()


async def test_recording_states_pass_through_unchanged():
    fake = FakeComponent4()
    record = make_event(1)
    fake.add([record])
    client = _client(fake)

    available = await client.fetch_recording(record.event_id)
    assert available.status == "AVAILABLE"
    assert available.recording.playback_url == fake.recording_playback_url
    assert available.recording.source == "mediamtx"

    fake.recording_status = "NOT_FOUND"
    fake.recording_reason = "no_containing_recording"
    not_found = await client.fetch_recording(record.event_id)
    assert (not_found.status, not_found.reason) == ("NOT_FOUND", "no_containing_recording")

    fake.recording_status = "UNAVAILABLE"
    fake.recording_reason = "vms_unreachable"
    unavailable = await client.fetch_recording(record.event_id)
    assert (unavailable.status, unavailable.reason) == ("UNAVAILABLE", "vms_unreachable")
    assert unavailable.recording is None


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "rtsp://mediamtx:8554/vms_cam_0123abcd",
        "http://user:pw@host/x",
        "http:///nohost",
        "http://host/" + "a" * 3000,
        "http://host/\nInjected: header",
        "",
        None,
        123,
    ],
)
async def test_an_unsafe_playback_url_is_downgraded_never_forwarded(url):
    fake = FakeComponent4()
    record = make_event(1)
    fake.add([record])
    fake.recording_playback_url = url
    lookup = await _client(fake).fetch_recording(record.event_id)
    assert lookup.status == "UNAVAILABLE"
    assert lookup.reason == "unsafe_playback_url"
    assert lookup.recording is None


async def test_the_adapter_refuses_a_non_event_identifier():
    fake = FakeComponent4()
    client = _client(fake)
    for hostile in ("../../etc/passwd", "evt_../x", "http://evil/x", ""):
        with pytest.raises(UpstreamContractError):
            await client.fetch_artifact(hostile, "crop")
        with pytest.raises(UpstreamContractError):
            await client.fetch_recording(hostile)


async def test_the_adapter_refuses_an_unknown_artifact_kind():
    fake = FakeComponent4()
    record = make_event(1)
    fake.add([record])
    with pytest.raises(UpstreamContractError):
        await _client(fake).fetch_artifact(record.event_id, "recording")


async def test_redirects_are_never_followed():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://evil.test/steal"})

    client = Component4Client(
        base_url=BASE,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ),
    )
    with pytest.raises(UpstreamContractError):
        await client.list_events()


async def test_every_outbound_url_is_built_from_the_configured_base():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    client = Component4Client(
        base_url=BASE,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await client.list_events()
    assert seen == [f"{BASE}/api/events?limit=100"]
