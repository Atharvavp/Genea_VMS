"""Component 3 recording lookup: strict parsing and half-open matching."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.domain.models import RecordingStatus
from app.services.vms_recordings import (
    NOT_FOUND_REASON,
    UNAVAILABLE_REASONS,
    VMSRecordingClient,
)
from tests.fakes import factories

pytestmark = pytest.mark.unit

BASE = "http://vms.invalid:8090"
CROSSED_AT = datetime(2026, 9, 7, 9, 15, 14, 123000, tzinfo=UTC)


def item(start: datetime, duration: float, item_id="rec_1", url=None, end=None):
    computed_end = end or (start + timedelta(seconds=duration))
    return {
        "id": item_id,
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time": computed_end.isoformat().replace("+00:00", "Z"),
        "duration_seconds": duration,
        "playback_url": url or "http://localhost:9996/get?path=vms_cam_0123abcd",
        "source": "mediamtx",
    }


def payload(items, camera_id="cam_0123abcd", date="2026-09-07"):
    return {
        "camera_id": camera_id,
        "camera_name": "car_stream",
        "date": date,
        "items": items,
    }


def client(handler) -> tuple[VMSRecordingClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_handler)
    http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(1.0))
    return VMSRecordingClient(base_url=BASE, client=http), seen


def responder(status=200, body=None, text=None):
    def _handler(_request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body if body is not None else payload([]))

    return _handler


# -- request shape ----------------------------------------------------------


async def test_the_request_targets_the_public_endpoint_with_the_utc_date():
    service, seen = client(responder(body=payload([])))
    await service.lookup(factories.event(crossed_at=CROSSED_AT))
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/api/recordings"
    assert dict(request.url.params) == {
        "camera_id": "cam_0123abcd",
        "date": "2026-09-07",
    }


@pytest.mark.parametrize(
    "moment,expected_date",
    [
        (datetime(2026, 9, 7, 0, 0, 0, tzinfo=UTC), "2026-09-07"),
        (datetime(2026, 9, 7, 23, 59, 59, 999000, tzinfo=UTC), "2026-09-07"),
        (datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC), "2026-09-08"),
    ],
)
async def test_the_date_is_the_utc_calendar_day(moment, expected_date):
    service, seen = client(responder(body=payload([])))
    await service.lookup(factories.event(crossed_at=moment))
    assert dict(seen[0].url.params)["date"] == expected_date


async def test_a_non_utc_timestamp_is_normalised_before_the_date_is_taken():
    from datetime import timezone

    service, seen = client(responder(body=payload([])))
    local = datetime(2026, 9, 8, 4, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    await service.lookup(factories.event(crossed_at=local))
    assert dict(seen[0].url.params)["date"] == "2026-09-07"


async def test_there_is_exactly_one_attempt_and_no_retry():
    calls = {"n": 0}

    def _handler(_request):
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    service, _seen = client(_handler)
    result = await service.lookup(factories.event())
    assert calls["n"] == 1
    assert result.status is RecordingStatus.UNAVAILABLE


# -- matching ---------------------------------------------------------------


async def test_a_containing_timespan_is_available():
    start = CROSSED_AT - timedelta(seconds=10)
    service, _seen = client(responder(body=payload([item(start, 60.0)])))
    result = await service.lookup(factories.event(crossed_at=CROSSED_AT))
    assert result.status is RecordingStatus.AVAILABLE
    assert result.recording.id == "rec_1"
    assert result.reason is None


async def test_the_start_is_inclusive():
    service, _seen = client(responder(body=payload([item(CROSSED_AT, 60.0)])))
    result = await service.lookup(factories.event(crossed_at=CROSSED_AT))
    assert result.status is RecordingStatus.AVAILABLE


async def test_the_end_is_exclusive_so_adjacent_spans_do_not_both_match():
    first_start = CROSSED_AT - timedelta(seconds=60)
    service, _seen = client(
        responder(
            body=payload(
                [
                    item(first_start, 60.0, "rec_first"),
                    item(CROSSED_AT, 60.0, "rec_second"),
                ]
            )
        )
    )
    result = await service.lookup(factories.event(crossed_at=CROSSED_AT))
    assert result.status is RecordingStatus.AVAILABLE
    assert result.recording.id == "rec_second"


async def test_no_containing_item_is_not_found():
    service, _seen = client(
        responder(body=payload([item(CROSSED_AT + timedelta(hours=1), 60.0)]))
    )
    result = await service.lookup(factories.event(crossed_at=CROSSED_AT))
    assert result.status is RecordingStatus.NOT_FOUND
    assert result.reason == NOT_FOUND_REASON
    assert result.recording is None


async def test_an_empty_item_list_is_not_found():
    service, _seen = client(responder(body=payload([])))
    result = await service.lookup(factories.event())
    assert result.status is RecordingStatus.NOT_FOUND


async def test_overlapping_spans_deterministically_choose_the_latest_start():
    early = CROSSED_AT - timedelta(seconds=100)
    late = CROSSED_AT - timedelta(seconds=10)
    service, _seen = client(
        responder(
            body=payload(
                [item(early, 300.0, "rec_early"), item(late, 300.0, "rec_late")]
            )
        )
    )
    result = await service.lookup(factories.event(crossed_at=CROSSED_AT))
    assert result.status is RecordingStatus.AVAILABLE
    assert result.recording.id == "rec_late"


# -- strict parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "broken",
    [
        {"duration_seconds": -60.0},
        {"duration_seconds": 0.0},
        {"start_time": "not-a-timestamp"},
        {"start_time": "2026-09-07T09:15:00"},  # naive
    ],
)
async def test_malformed_items_are_unavailable(broken):
    base = item(CROSSED_AT - timedelta(seconds=10), 60.0)
    base.update(broken)
    service, _seen = client(responder(body=payload([base])))
    result = await service.lookup(factories.event(crossed_at=CROSSED_AT))
    assert result.status is RecordingStatus.UNAVAILABLE
    assert result.reason == "vms_invalid_response"


async def test_an_end_time_that_disagrees_with_the_duration_is_unavailable():
    start = CROSSED_AT - timedelta(seconds=10)
    broken = item(start, 60.0, end=start + timedelta(seconds=90))
    service, _seen = client(responder(body=payload([broken])))
    result = await service.lookup(factories.event(crossed_at=CROSSED_AT))
    assert result.status is RecordingStatus.UNAVAILABLE
    assert result.reason == "vms_invalid_response"


async def test_a_one_millisecond_disagreement_is_tolerated():
    start = CROSSED_AT - timedelta(seconds=10)
    ok = item(start, 60.0, end=start + timedelta(seconds=60, milliseconds=1))
    service, _seen = client(responder(body=payload([ok])))
    result = await service.lookup(factories.event(crossed_at=CROSSED_AT))
    assert result.status is RecordingStatus.AVAILABLE


async def test_a_camera_id_mismatch_is_unavailable():
    service, _seen = client(
        responder(body=payload([item(CROSSED_AT, 60.0)], camera_id="cam_99999999"))
    )
    result = await service.lookup(factories.event(crossed_at=CROSSED_AT))
    assert result.status is RecordingStatus.UNAVAILABLE
    assert result.reason == "vms_invalid_response"


@pytest.mark.parametrize(
    "url",
    [
        "rtsp://localhost:9996/get",
        "file:///recordings/x.mp4",
        "http://user:pass@localhost:9996/get",
        "http:///get",
        "javascript:alert(1)",
    ],
)
async def test_an_unsafe_playback_url_is_refused(url):
    start = CROSSED_AT - timedelta(seconds=10)
    service, _seen = client(responder(body=payload([item(start, 60.0, url=url)])))
    result = await service.lookup(factories.event(crossed_at=CROSSED_AT))
    assert result.status is RecordingStatus.UNAVAILABLE
    assert result.reason == "unsafe_playback_url"


async def test_a_malformed_body_is_unavailable():
    service, _seen = client(responder(text="this is not json"))
    result = await service.lookup(factories.event())
    assert result.reason == "vms_invalid_response"


async def test_a_missing_field_is_unavailable():
    service, _seen = client(responder(body={"camera_id": "cam_0123abcd"}))
    result = await service.lookup(factories.event())
    assert result.reason == "vms_invalid_response"


# -- upstream failures ------------------------------------------------------


@pytest.mark.parametrize(
    "status,reason",
    [
        (404, "vms_rejected_request"),
        (409, "vms_rejected_request"),
        (422, "vms_rejected_request"),
        (500, "vms_unavailable"),
        (503, "vms_unavailable"),
    ],
)
async def test_each_upstream_status_maps_to_a_safe_reason(status, reason):
    service, _seen = client(responder(status=status, body={"error": {"code": "x"}}))
    result = await service.lookup(factories.event())
    assert result.status is RecordingStatus.UNAVAILABLE
    assert result.reason == reason
    assert result.reason in UNAVAILABLE_REASONS


async def test_a_timeout_is_reported_as_a_timeout():
    def _handler(_request):
        raise httpx.ReadTimeout("slow")

    service, _seen = client(_handler)
    result = await service.lookup(factories.event())
    assert result.reason == "vms_timeout"


async def test_a_network_error_is_reported_as_unreachable():
    def _handler(_request):
        raise httpx.ConnectError("refused")

    service, _seen = client(_handler)
    result = await service.lookup(factories.event())
    assert result.reason == "vms_unreachable"


async def test_every_reason_is_from_the_safe_vocabulary():
    assert UNAVAILABLE_REASONS == {
        "vms_timeout",
        "vms_unreachable",
        "vms_rejected_request",
        "vms_unavailable",
        "vms_invalid_response",
        "unsafe_playback_url",
    }


async def test_the_lookup_holds_no_reference_to_the_manager_or_a_worker():
    service, _seen = client(responder(body=payload([])))
    attributes = json.dumps(sorted(vars(service).keys()))
    assert "manager" not in attributes
    assert "worker" not in attributes
    assert "tracker" not in attributes
