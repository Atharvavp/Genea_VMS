"""The adapter against a real, running Component 4.

Read-only: this tier lists events, fetches committed artifacts, and asks for a
recording. It never creates, updates, or deletes anything upstream, and it never
touches the VMS, MediaMTX, or any filesystem outside this repository.

Skipped unless COMPONENT4_API_BASE_URL answers /health.
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.domain.models import RepresentationKind, is_event_id
from app.integrations.component4 import (
    Component4Client,
    UpstreamEventNotFound,
    build_client,
)

BASE = os.environ.get("COMPONENT4_API_BASE_URL", "http://host.docker.internal:8100")


def _reachable() -> bool:
    try:
        return httpx.get(f"{BASE}/health", timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason=f"no live Component 4 at {BASE}"
)


@pytest.fixture()
async def client():
    http = build_client()
    try:
        yield Component4Client(base_url=BASE, client=http)
    finally:
        await http.aclose()


async def test_full_traversal_is_unique_and_strictly_ordered(client):
    seen: list[str] = []
    order: list[tuple] = []
    cursor = None
    pages = 0
    while True:
        page = await client.list_events(cursor=cursor)
        assert page.malformed == 0, "live Component 4 returned an item we cannot parse"
        for event in page.events:
            assert is_event_id(event.event_id)
            seen.append(event.event_id)
            order.append((event.crossed_at, event.event_id))
        pages += 1
        cursor = page.next_cursor
        if cursor is None or pages > 200:
            break
    assert len(seen) == len(set(seen)), "the traversal returned a duplicate event"
    assert order == sorted(order, reverse=True), "ordering is not crossed_at DESC, id DESC"
    print(f"\nlive traversal: pages={pages} events={len(seen)}")


async def test_real_crop_and_frame_are_decodable_jpegs(client):
    page = await client.list_events()
    if not page.events:
        pytest.skip("live Component 4 has no events")
    event = page.events[0]
    crop = await client.fetch_artifact(event.event_id, RepresentationKind.CROP)
    frame = await client.fetch_artifact(event.event_id, RepresentationKind.FRAME)
    assert crop.startswith(b"\xff\xd8\xff") and frame.startswith(b"\xff\xd8\xff")
    assert 0 < len(crop) < 16 * 1024 * 1024
    print(f"\nlive artifacts: crop={len(crop)}B frame={len(frame)}B")


async def test_recording_lookup_returns_a_known_state(client):
    page = await client.list_events()
    if not page.events:
        pytest.skip("live Component 4 has no events")
    lookup = await client.fetch_recording(page.events[0].event_id)
    assert lookup.status in {"AVAILABLE", "NOT_FOUND", "UNAVAILABLE"}
    if lookup.status == "AVAILABLE":
        assert lookup.recording.playback_url.startswith(("http://", "https://"))
    print(f"\nlive recording: status={lookup.status} reason={lookup.reason}")


async def test_an_unknown_event_id_is_permanent_not_retryable(client):
    unknown = "evt_" + "f" * 32
    with pytest.raises(UpstreamEventNotFound) as exc:
        await client.fetch_artifact(unknown, "crop")
    assert exc.value.retryable is False
