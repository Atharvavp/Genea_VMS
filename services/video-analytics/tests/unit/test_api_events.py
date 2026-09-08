"""Event filter, detail, image, and recording routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.analytics.types import CommitOutcome
from tests.fakes import factories, worker as worker_fakes
from tests.fakes.app_harness import build_harness

pytestmark = pytest.mark.unit

CROSSED_AT = datetime(2026, 9, 7, 9, 15, 14, 123000, tzinfo=UTC)

SUMMARY_KEYS = {
    "id",
    "camera_id",
    "vms_camera_id",
    "camera_name",
    "line_id",
    "line_name",
    "worker_session_id",
    "track_id",
    "object_category",
    "object_class",
    "direction",
    "confidence",
    "crossed_at",
    "bbox",
    "centroid",
    "frame_width",
    "frame_height",
    "frame_url",
    "crop_url",
}


@pytest.fixture(autouse=True)
def _reset():
    worker_fakes.reset()
    yield
    worker_fakes.reset()


def _recording_handler(items, camera_id="cam_0123abcd", status=200):
    def handler(_request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": {"code": "x"}})
        return httpx.Response(
            200,
            json={
                "camera_id": camera_id,
                "camera_name": "car_stream",
                "date": "2026-09-07",
                "items": items,
            },
        )

    return handler


async def _make_client(tmp_path: Path, recording_handler=None):
    harness = build_harness(tmp_path, recording_handler=recording_handler)
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    )
    return harness, http


@pytest.fixture
async def client(tmp_path: Path):
    harness, http = await _make_client(tmp_path)
    async with http:
        async with harness.app.router.lifespan_context(harness.app):
            yield http, harness


def seed(harness, count=5):
    records = []
    for index in range(count):
        record = factories.event(
            event_id=f"evt_{index:032x}",
            track_id=index,
            crossed_at=CROSSED_AT + timedelta(seconds=index),
            object_class="person" if index % 2 else "truck",
            category=(
                factories.ObjectCategory.PERSON
                if index % 2
                else factories.ObjectCategory.VEHICLE
            ),
            direction=(
                factories.CrossingDirection.B_TO_A
                if index % 2
                else factories.CrossingDirection.A_TO_B
            ),
        )
        harness.events.insert(record)
        records.append(record)
    return records


# -- listing ----------------------------------------------------------------


async def test_events_are_returned_newest_first(client):
    http, harness = client
    seed(harness)
    body = (await http.get("/api/events")).json()
    assert [item["track_id"] for item in body["items"]] == [4, 3, 2, 1, 0]
    assert body["next_cursor"] is None
    assert set(body["items"][0]) == SUMMARY_KEYS


async def test_no_total_count_is_returned(client):
    http, harness = client
    seed(harness)
    assert set((await http.get("/api/events")).json()) == {"items", "next_cursor"}


async def test_image_urls_are_route_paths_not_filesystem_paths(client):
    http, harness = client
    records = seed(harness, 1)
    item = (await http.get("/api/events")).json()["items"][0]
    assert item["frame_url"] == f"/api/events/{records[0].id}/frame"
    assert item["crop_url"] == f"/api/events/{records[0].id}/crop"
    assert "/data" not in (await http.get("/api/events")).text


@pytest.mark.parametrize(
    "query,expected",
    [
        ({"object_category": "person"}, {1, 3}),
        ({"object_class": "truck"}, {0, 2, 4}),
        ({"direction": "B_TO_A"}, {1, 3}),
        ({"camera_id": "acam_0123abcd"}, {0, 1, 2, 3, 4}),
        ({"camera_id": "acam_ffffffff"}, set()),
    ],
)
async def test_filters(client, query, expected):
    http, harness = client
    seed(harness)
    body = (await http.get("/api/events", params=query)).json()
    assert {item["track_id"] for item in body["items"]} == expected


async def test_the_time_range_is_half_open(client):
    http, harness = client
    records = seed(harness)
    body = (
        await http.get(
            "/api/events",
            params={
                "from": records[1].crossed_at.isoformat().replace("+00:00", "Z"),
                "to": records[3].crossed_at.isoformat().replace("+00:00", "Z"),
            },
        )
    ).json()
    assert {item["track_id"] for item in body["items"]} == {1, 2}


async def test_an_inverted_range_is_400(client):
    http, harness = client
    response = await http.get(
        "/api/events", params={"from": "2026-09-08T00:00:00Z", "to": "2026-09-07T00:00:00Z"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_time_range"


async def test_a_range_longer_than_31_days_is_400(client):
    http, harness = client
    response = await http.get(
        "/api/events", params={"from": "2026-01-01T00:00:00Z", "to": "2026-03-01T00:00:00Z"}
    )
    assert response.status_code == 400


async def test_an_unsupported_object_class_is_rejected(client):
    http, harness = client
    assert (await http.get("/api/events", params={"object_class": "train"})).status_code == 400


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_out_of_range_limits_are_422(client, limit):
    http, harness = client
    assert (await http.get("/api/events", params={"limit": limit})).status_code == 422


# -- cursor -----------------------------------------------------------------


async def test_cursor_pagination_covers_every_event_exactly_once(client):
    http, harness = client
    seed(harness, 5)
    seen: list[int] = []
    cursor = None
    for _ in range(5):
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = (await http.get("/api/events", params=params)).json()
        seen.extend(item["track_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == [4, 3, 2, 1, 0]
    assert len(seen) == len(set(seen))


async def test_a_tampered_cursor_is_400(client):
    http, harness = client
    seed(harness)
    body = (await http.get("/api/events", params={"limit": 2})).json()
    tampered = body["next_cursor"][:-2] + "AA"
    response = await http.get("/api/events", params={"cursor": tampered, "limit": 2})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


async def test_a_cursor_from_different_filters_is_400(client):
    http, harness = client
    seed(harness)
    body = (await http.get("/api/events", params={"limit": 2})).json()
    response = await http.get(
        "/api/events",
        params={"cursor": body["next_cursor"], "limit": 2, "object_category": "person"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.parametrize("cursor", ["", "not-a-cursor", "a.b", "///"])
async def test_a_malformed_cursor_is_400(client, cursor):
    http, harness = client
    response = await http.get("/api/events", params={"cursor": cursor})
    assert response.status_code == 400


# -- detail and images ------------------------------------------------------


async def test_event_detail(client):
    http, harness = client
    records = seed(harness, 1)
    response = await http.get(f"/api/events/{records[0].id}")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == SUMMARY_KEYS
    assert "source_pts_ns" not in body
    assert "snapshot_path" not in body


async def test_an_unknown_event_is_404(client):
    http, harness = client
    response = await http.get("/api/events/evt_" + "f" * 32)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "event_not_found"


async def test_images_are_served_with_immutable_cache_headers(client):
    http, harness = client
    result = harness.storage.commit_event(factories.candidate())
    assert result.outcome is CommitOutcome.INSERTED
    for kind in ("frame", "crop"):
        response = await http.get(f"/api/events/{result.event_id}/{kind}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.content[:2] == b"\xff\xd8"


async def test_a_missing_artifact_is_410(client):
    http, harness = client
    result = harness.storage.commit_event(factories.candidate())
    record = harness.events.get(result.event_id)
    (harness.settings.analytics_data_dir / record.snapshot_path).unlink()
    response = await http.get(f"/api/events/{result.event_id}/frame")
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "event_artifact_gone"
    # The row itself survives.
    assert (await http.get(f"/api/events/{result.event_id}")).status_code == 200


async def test_images_of_an_unknown_event_are_404(client):
    http, harness = client
    assert (await http.get("/api/events/evt_" + "f" * 32 + "/frame")).status_code == 404


async def test_there_is_no_path_query_parameter(client):
    http, harness = client
    result = harness.storage.commit_event(factories.candidate())
    response = await http.get(
        f"/api/events/{result.event_id}/frame", params={"path": "/etc/passwd"}
    )
    assert response.status_code == 200
    assert response.content[:2] == b"\xff\xd8"


async def test_events_survive_the_deletion_of_their_camera(client):
    http, harness = client
    await http.post(
        "/api/cameras",
        json={
            "vms_camera_id": "cam_0123abcd",
            "name": "Loading Bay",
            "rtsp_url": "rtsp://cam:8554/x",
            "enabled": False,
        },
    )
    camera_id = (await http.get("/api/cameras")).json()[0]["id"]
    harness.events.insert(factories.event(camera_id=camera_id))
    await http.delete(f"/api/cameras/{camera_id}")
    body = (await http.get("/api/events", params={"camera_id": camera_id})).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["camera_name"] == "Loading Bay"


# -- recording lookup -------------------------------------------------------


async def _recording_client(tmp_path: Path, handler):
    harness, http = await _make_client(tmp_path, recording_handler=handler)
    return harness, http


async def test_an_available_recording_returns_200_with_the_vms_playback_url(tmp_path):
    start = CROSSED_AT - timedelta(seconds=10)
    items = [
        {
            "id": "rec_1",
            "start_time": start.isoformat().replace("+00:00", "Z"),
            "end_time": (start + timedelta(seconds=60))
            .isoformat()
            .replace("+00:00", "Z"),
            "duration_seconds": 60.0,
            "playback_url": "http://localhost:9996/get?path=vms_cam_0123abcd",
            "source": "mediamtx",
        }
    ]
    harness, http = await _recording_client(tmp_path, _recording_handler(items))
    async with http:
        async with harness.app.router.lifespan_context(harness.app):
            record = factories.event(crossed_at=CROSSED_AT)
            harness.events.insert(record)
            response = await http.get(f"/api/events/{record.id}/recording")
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "AVAILABLE"
            assert body["event_id"] == record.id
            assert body["reason"] is None
            assert (
                body["recording"]["playback_url"]
                == "http://localhost:9996/get?path=vms_cam_0123abcd"
            )


async def test_no_containing_recording_is_200_not_found(tmp_path):
    harness, http = await _recording_client(tmp_path, _recording_handler([]))
    async with http:
        async with harness.app.router.lifespan_context(harness.app):
            record = factories.event(crossed_at=CROSSED_AT)
            harness.events.insert(record)
            body = (await http.get(f"/api/events/{record.id}/recording")).json()
            assert body["status"] == "NOT_FOUND"
            assert body["reason"] == "no_containing_recording"
            assert body["recording"] is None


@pytest.mark.parametrize("status,reason", [(404, "vms_rejected_request"), (503, "vms_unavailable")])
async def test_an_upstream_failure_is_200_unavailable(tmp_path, status, reason):
    harness, http = await _recording_client(
        tmp_path, _recording_handler([], status=status)
    )
    async with http:
        async with harness.app.router.lifespan_context(harness.app):
            record = factories.event(crossed_at=CROSSED_AT)
            harness.events.insert(record)
            response = await http.get(f"/api/events/{record.id}/recording")
            assert response.status_code == 200
            assert response.json()["status"] == "UNAVAILABLE"
            assert response.json()["reason"] == reason


async def test_the_recording_route_is_404_only_for_an_absent_event(client):
    http, harness = client
    response = await http.get("/api/events/evt_" + "f" * 32 + "/recording")
    assert response.status_code == 404


async def test_a_recording_lookup_does_not_change_any_worker_state(tmp_path):
    harness, http = await _recording_client(tmp_path, _recording_handler([], status=503))
    async with http:
        async with harness.app.router.lifespan_context(harness.app):
            await http.post(
                "/api/cameras",
                json={
                    "vms_camera_id": "cam_0123abcd",
                    "name": "Loading Bay",
                    "rtsp_url": "rtsp://cam:8554/x",
                },
            )
            camera_id = (await http.get("/api/cameras")).json()[0]["id"]
            before = (await http.get(f"/api/cameras/{camera_id}")).json()["runtime"]
            record = factories.event(camera_id=camera_id, crossed_at=CROSSED_AT)
            harness.events.insert(record)
            await http.get(f"/api/events/{record.id}/recording")
            after = (await http.get(f"/api/cameras/{camera_id}")).json()["runtime"]
            assert before["state"] == after["state"]
            assert before["worker_session_id"] == after["worker_session_id"]
            assert before["applied_config_revision"] == after["applied_config_revision"]
