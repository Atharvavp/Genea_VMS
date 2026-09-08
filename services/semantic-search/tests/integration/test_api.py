"""The complete public API against a fake Component 4 and a seeded local store."""

from __future__ import annotations

import asyncio
import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.models import Direction, ObjectClass, format_utc, utc_now
from app.main import create_app
from tests.fakes.component4_server import FakeComponent4
from tests.fakes.factories import (
    FakeEmbeddingRuntime,
    jpeg_bytes,
    make_event,
    make_events,
    png_bytes,
)
from tests.integration.test_discovery_indexing import Harness


def _seed(tmp_path, fake: FakeComponent4) -> None:
    async def run() -> None:
        harness = Harness(tmp_path, fake)
        try:
            await harness.discover_all()
            await harness.index_all()
        finally:
            await harness.aclose()

    asyncio.run(run())


@pytest.fixture()
def world(tmp_path):
    fake = FakeComponent4()
    events = [
        make_event(0, camera=0, camera_name="Loading Bay",
                   object_class=ObjectClass.CAR, direction=Direction.A_TO_B,
                   crossed_at=utc_now() - timedelta(minutes=30)),
        make_event(1, camera=0, camera_name="Loading Bay",
                   object_class=ObjectClass.PERSON, direction=Direction.B_TO_A,
                   crossed_at=utc_now() - timedelta(minutes=20)),
        make_event(2, camera=1, camera_name="Gate <script>",
                   object_class=ObjectClass.BUS, direction=Direction.A_TO_B,
                   crossed_at=utc_now() - timedelta(minutes=10)),
    ]
    fake.add(events)
    _seed(tmp_path, fake)

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
    with TestClient(app) as client:
        yield client, fake, events


def test_health_is_ok_with_a_ready_local_index(world):
    client, _fake, _events = world
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["search"] == "ok" and body["model"] == "ok" and body["index"] == "ok"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Request-ID"].startswith("req_")


def test_text_search_returns_one_ranked_result_per_event(world):
    client, _fake, events = world
    response = client.post("/api/search/text", json={"query": "a blue vehicle", "top_k": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "text"
    assert body["model_id"].startswith("siglip-base-p16-224@")
    assert body["candidate_count"] == 3
    assert len(body["results"]) == 3
    assert len({item["event_id"] for item in body["results"]}) == 3
    assert [item["rank"] for item in body["results"]] == [1, 2, 3]
    scores = [item["score"] for item in body["results"]]
    assert scores == sorted(scores, reverse=True)
    first = body["results"][0]
    assert first["crop_url"] == f"/api/events/{first['event_id']}/crop"
    assert first["recording_url"].endswith("/recording")
    assert first["representation_scores"]["crop"] is not None
    assert first["representation_scores"]["frame"] is not None


def test_search_filters_are_applied_before_ranking(world):
    client, _fake, events = world
    response = client.post(
        "/api/search/text",
        json={
            "query": "anything",
            "top_k": 10,
            "filters": {"camera_id": events[0].camera_id},
        },
    )
    body = response.json()
    assert body["candidate_count"] == 2
    assert {item["camera_id"] for item in body["results"]} == {events[0].camera_id}


def test_a_time_window_is_inclusive_from_and_exclusive_to(world):
    client, _fake, events = world
    response = client.post(
        "/api/search/text",
        json={
            "query": "anything",
            "filters": {
                "from": format_utc(events[0].crossed_at),
                "to": format_utc(events[2].crossed_at),
            },
        },
    )
    returned = {item["event_id"] for item in response.json()["results"]}
    assert returned == {events[0].event_id, events[1].event_id}


def test_a_contradictory_filter_returns_an_empty_result_not_an_error(world):
    client, _fake, _events = world
    response = client.post(
        "/api/search/text",
        json={
            "query": "car",
            "filters": {"object_category": "person", "object_class": "car"},
        },
    )
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert response.json()["candidate_count"] == 0


def test_an_inverted_time_range_is_rejected(world):
    client, _fake, _events = world
    response = client.post(
        "/api/search/text",
        json={"query": "car", "filters": {"from": "2026-09-08T00:00:00Z",
                                          "to": "2026-09-01T00:00:00Z"}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "query",
    ["", "   ", "\n\t ", "x" * 257, "bad\x00query", "bad\x07query"],
)
def test_an_invalid_query_is_rejected_without_being_echoed(world, query):
    client, _fake, _events = world
    response = client.post("/api/search/text", json={"query": query, "top_k": 5})
    assert response.status_code in (400, 422)
    assert query.strip() not in response.text or not query.strip()


def test_query_text_is_nfkc_normalised_and_whitespace_collapsed(world):
    client, _fake, _events = world
    plain = client.post("/api/search/text", json={"query": "blue car"}).json()
    noisy = client.post(
        "/api/search/text", json={"query": "  blue   car \n"}
    ).json()
    assert [r["event_id"] for r in plain["results"]] == [
        r["event_id"] for r in noisy["results"]
    ]


@pytest.mark.parametrize("top_k,expected", [(0, 422), (101, 422), (1, 200)])
def test_top_k_bounds_are_enforced(world, top_k, expected):
    client, _fake, _events = world
    response = client.post("/api/search/text", json={"query": "car", "top_k": top_k})
    assert response.status_code == expected


def test_min_score_filters_after_fusion(world):
    client, _fake, _events = world
    everything = client.post(
        "/api/search/text", json={"query": "grey", "top_k": 10}
    ).json()
    assert everything["results"]
    threshold = max(item["score"] for item in everything["results"]) + 0.001
    filtered = client.post(
        "/api/search/text", json={"query": "grey", "top_k": 10, "min_score": threshold}
    ).json()
    assert filtered["results"] == []
    assert filtered["candidate_count"] == everything["candidate_count"]


def test_image_search_accepts_jpeg_and_png(world):
    client, _fake, _events = world
    for payload, name in ((jpeg_bytes(), "q.jpg"), (png_bytes(), "q.png")):
        response = client.post(
            "/api/search/image",
            files={"image": (name, io.BytesIO(payload), "image/jpeg")},
            params={"top_k": 2},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mode"] == "image"
        assert len(body["results"]) == 2


def test_image_search_applies_the_same_filters(world):
    client, _fake, events = world
    response = client.post(
        "/api/search/image",
        files={"image": ("q.jpg", io.BytesIO(jpeg_bytes()), "image/jpeg")},
        params={"camera_id": events[2].camera_id, "top_k": 10},
    )
    body = response.json()
    assert body["candidate_count"] == 1
    assert body["results"][0]["event_id"] == events[2].event_id


@pytest.mark.parametrize(
    "payload,declared,expected_code",
    [
        (b"GIF89a" + b"\x00" * 64, "image/gif", "unsupported_image_type"),
        (b"%PDF-1.4" + b"\x00" * 64, "image/jpeg", "unsupported_image_type"),
        (b"", "image/jpeg", "invalid_image"),
    ],
)
def test_a_hostile_upload_is_refused_by_content_not_by_name(
    world, payload, declared, expected_code
):
    client, _fake, _events = world
    response = client.post(
        "/api/search/image",
        files={"image": ("innocent.jpg", io.BytesIO(payload), declared)},
    )
    assert response.status_code in (413, 415, 422)
    assert response.json()["error"]["code"] == expected_code
    assert "innocent.jpg" not in response.text


def test_an_oversized_upload_is_refused_before_decoding(world):
    client, _fake, _events = world
    response = client.post(
        "/api/search/image",
        files={"image": ("big.jpg", io.BytesIO(b"\xff\xd8\xff" + b"\x00" * (9 * 1024 * 1024)),
                         "image/jpeg")},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "image_too_large"


def test_local_event_detail_is_served_from_local_state(world):
    client, fake, events = world
    fake.outage = "down"  # Component 4 is gone; detail must still work.
    response = client.get(f"/api/events/{events[0].event_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["event_id"] == events[0].event_id
    assert body["camera_name"] == "Loading Bay"
    assert body["index_state"] == "complete"
    assert body["representations"] == {"crop": "indexed", "frame": "indexed"}


@pytest.mark.parametrize(
    "event_id",
    ["evt_" + "f" * 32, "not-an-event", "../../etc/passwd", "evt_short"],
)
def test_an_unknown_or_hostile_event_id_is_404(world, event_id):
    client, _fake, _events = world
    for suffix in ("", "/crop", "/frame", "/recording"):
        response = client.get(f"/api/events/{event_id}{suffix}")
        assert response.status_code == 404


def test_the_image_proxy_serves_component4_bytes_with_immutable_caching(world):
    client, fake, events = world
    response = client.get(f"/api/events/{events[0].event_id}/crop")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    assert response.content.startswith(b"\xff\xd8\xff")
    assert f"crop:{events[0].event_id}" in fake.request_log


def test_the_proxy_maps_upstream_410_and_outage_to_safe_statuses(world):
    client, fake, events = world
    fake.gone_artifacts.add(events[0].event_id)
    gone = client.get(f"/api/events/{events[0].event_id}/crop")
    assert gone.status_code == 410
    assert gone.json()["error"]["code"] == "event_artifact_gone"

    fake.outage = "down"
    unavailable = client.get(f"/api/events/{events[1].event_id}/crop")
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "upstream_unavailable"
    assert "component4.test" not in unavailable.text

    fake.outage = "timeout"
    slow = client.get(f"/api/events/{events[1].event_id}/crop")
    assert slow.status_code == 504
    assert slow.json()["error"]["code"] == "upstream_timeout"


def test_recording_states_pass_through_and_an_outage_is_data_not_an_error(world):
    client, fake, events = world
    available = client.get(f"/api/events/{events[0].event_id}/recording")
    assert available.status_code == 200
    body = available.json()
    assert body["status"] == "AVAILABLE"
    assert body["recording"]["playback_url"] == fake.recording_playback_url
    assert body["recording"]["source"] == "mediamtx"

    fake.recording_status = "NOT_FOUND"
    fake.recording_reason = "no_containing_recording"
    not_found = client.get(f"/api/events/{events[0].event_id}/recording").json()
    assert (not_found["status"], not_found["reason"]) == ("NOT_FOUND", "no_containing_recording")

    fake.outage = "down"
    outage = client.get(f"/api/events/{events[0].event_id}/recording")
    assert outage.status_code == 200
    assert outage.json() == {
        "status": "UNAVAILABLE",
        "event_id": events[0].event_id,
        "recording": None,
        "reason": "component4_unreachable",
    }


def test_an_unsafe_playback_url_is_never_forwarded(world):
    client, fake, events = world
    fake.recording_playback_url = "file:///etc/passwd"
    body = client.get(f"/api/events/{events[0].event_id}/recording").json()
    assert body["status"] == "UNAVAILABLE"
    assert body["reason"] == "unsafe_playback_url"
    assert "passwd" not in str(body)


def test_index_status_reports_local_counters_and_model_identity(world):
    client, _fake, _events = world
    body = client.get("/api/index/status").json()
    assert body["known_events"] == 3
    assert body["searchable_events"] == 3
    assert body["complete_events"] == 3
    assert body["crop_indexed"] == 3 and body["frame_indexed"] == 3
    assert body["pending_representations"] == 0
    assert body["embedding_dim"] == 768 and body["embedding_dtype"] == "float32"
    assert body["model_sha256"] == (
        "2c63cb7d1f2e95ba501893cbb8faeb4ea9a3af295498d35097126228659c2af8"
    )
    assert body["backfill_complete"] is True
    assert body["search"] == "ok"


def test_facets_come_from_local_metadata_and_survive_an_outage(world):
    client, fake, events = world
    fake.outage = "down"
    body = client.get("/api/index/facets").json()
    assert body["searchable_events"] == 3
    assert {facet["camera_id"] for facet in body["cameras"]} == {
        events[0].camera_id, events[2].camera_id
    }
    by_id = {facet["camera_id"]: facet for facet in body["cameras"]}
    assert by_id[events[0].camera_id]["count"] == 2
    assert by_id[events[2].camera_id]["camera_name"] == "Gate <script>"
    assert set(body["object_classes"]) == {"car", "person", "bus"}
    assert body["earliest_crossed_at"] < body["latest_crossed_at"]


def test_search_still_works_while_component4_is_down(world):
    client, fake, _events = world
    fake.outage = "down"
    response = client.post("/api/search/text", json={"query": "a red car", "top_k": 5})
    assert response.status_code == 200
    assert len(response.json()["results"]) == 3
    image = client.post(
        "/api/search/image",
        files={"image": ("q.jpg", io.BytesIO(jpeg_bytes()), "image/jpeg")},
    )
    assert image.status_code == 200
    assert client.get("/health").status_code == 200


def test_search_is_unavailable_when_the_model_is_not_loaded(tmp_path):
    fake = FakeComponent4()
    fake.add(make_events(2))
    _seed(tmp_path, fake)
    settings = Settings(
        semantic_data_dir=tmp_path,
        semantic_db_path=tmp_path / "semantic.db",
        component4_api_base_url="http://component4.test:8100",
    )
    broken = FakeEmbeddingRuntime()
    broken.mark_unavailable("model_unavailable")
    app = create_app(
        settings, embedding_runtime=broken, http_client=fake.client(),
        start_background=False,
    )
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 503
        assert health.json()["search"] == "unavailable"
        search = client.post("/api/search/text", json={"query": "car"})
        assert search.status_code == 503
        assert search.json()["error"]["code"] == "search_unavailable"


def test_an_incompatible_store_leaves_search_unavailable(tmp_path):
    fake = FakeComponent4()
    fake.add(make_events(2))
    _seed(tmp_path, fake)
    from app.persistence.database import Database

    database = Database(tmp_path / "semantic.db")
    with database.transaction() as conn:
        conn.execute("UPDATE system_state SET model_id = 'clip-vit-b32@old'")
    database.close()

    settings = Settings(
        semantic_data_dir=tmp_path,
        semantic_db_path=tmp_path / "semantic.db",
        component4_api_base_url="http://component4.test:8100",
    )
    app = create_app(
        settings, embedding_runtime=FakeEmbeddingRuntime(),
        http_client=fake.client(), start_background=False,
    )
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 503
        assert health.json()["database"] == "unavailable"


def test_the_dashboard_and_static_assets_are_served_same_origin(world):
    client, _fake, _events = world
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["Cache-Control"] == "no-store"
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
