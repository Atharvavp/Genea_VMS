"""Camera and snapshot routes: statuses, bodies, headers, and sanitisation."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.fakes import worker as worker_fakes
from tests.fakes.app_harness import build_harness

pytestmark = pytest.mark.unit

SECRET = "hunter2"
CREDENTIALED = f"rtsp://admin:{SECRET}@10.0.0.9:554/live"

CAMERA_KEYS = {
    "id",
    "vms_camera_id",
    "name",
    "rtsp_url_masked",
    "rtsp_has_credentials",
    "enabled",
    "inference_fps",
    "confidence_threshold",
    "enabled_classes",
    "line_configured",
    "runtime",
    "created_at",
    "updated_at",
}


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


def payload(**over):
    body = {
        "vms_camera_id": "cam_0123abcd",
        "name": "Loading Bay",
        "rtsp_url": "rtsp://host.docker.internal:8555/vms_cam_0123abcd",
    }
    body.update(over)
    return body


# -- create -----------------------------------------------------------------


async def test_create_returns_201_with_a_location_header(client):
    http, _harness = client
    response = await http.post("/api/cameras", json=payload())
    assert response.status_code == 201
    body = response.json()
    assert set(body) == CAMERA_KEYS
    assert response.headers["Location"] == f"/api/cameras/{body['id']}"
    assert body["enabled_classes"] == ["person", "vehicle"]
    assert body["line_configured"] is False
    assert body["runtime"]["state"] in {"STARTING", "RUNNING"}


async def test_defaults_match_the_documented_values(client):
    http, _harness = client
    body = (await http.post("/api/cameras", json=payload())).json()
    assert body["inference_fps"] == 5.0
    assert body["confidence_threshold"] == 0.25
    assert body["enabled"] is True


async def test_a_credentialed_url_is_masked_in_the_response(client):
    http, _harness = client
    body = (await http.post("/api/cameras", json=payload(rtsp_url=CREDENTIALED))).json()
    assert body["rtsp_has_credentials"] is True
    assert body["rtsp_url_masked"] == "rtsp://***:***@10.0.0.9:554/live"
    assert SECRET not in (await http.get("/api/cameras")).text


async def test_no_response_field_carries_the_raw_url(client):
    http, _harness = client
    await http.post("/api/cameras", json=payload(rtsp_url=CREDENTIALED))
    for path in ("/api/cameras", "/api/cameras/acam_0123abcd", "/openapi.json"):
        text = (await http.get(path)).text
        assert SECRET not in text
    schema = (await http.get("/openapi.json")).json()
    camera_schema = schema["components"]["schemas"]["CameraResponse"]["properties"]
    assert "rtsp_url" not in camera_schema


async def test_a_duplicate_vms_camera_id_is_409(client):
    http, _harness = client
    await http.post("/api/cameras", json=payload())
    response = await http.post("/api/cameras", json=payload(name="Second"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "duplicate_vms_camera_id"


@pytest.mark.parametrize(
    "body",
    [
        {"vms_camera_id": "acam_0123abcd"},
        {"vms_camera_id": "cam_ZZZZZZZZ"},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 101},
        {"rtsp_url": "http://cam/live"},
        {"rtsp_url": ""},
        {"inference_fps": 0.9},
        {"inference_fps": 10.1},
        {"confidence_threshold": 0.09},
        {"confidence_threshold": 0.96},
        {"enabled_classes": []},
        {"enabled_classes": ["person", "person"]},
        {"enabled_classes": ["train"]},
        {"unknown_field": 1},
    ],
)
async def test_invalid_create_payloads_are_422(client, body):
    http, _harness = client
    response = await http.post("/api/cameras", json=payload(**body))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_a_validation_error_never_echoes_the_rejected_secret(client):
    http, _harness = client
    response = await http.post(
        "/api/cameras", json=payload(rtsp_url=f"http://admin:{SECRET}@cam/live")
    )
    assert response.status_code == 422
    assert SECRET not in response.text


async def test_boundary_values_are_accepted(client):
    http, _harness = client
    for index, (fps, confidence) in enumerate(
        [(1.0, 0.10), (10.0, 0.95)]
    ):
        response = await http.post(
            "/api/cameras",
            json=payload(
                vms_camera_id=f"cam_0000000{index}",
                name=f"Camera {index}",
                inference_fps=fps,
                confidence_threshold=confidence,
            ),
        )
        assert response.status_code == 201


# -- read -------------------------------------------------------------------


async def test_list_and_get(client):
    http, _harness = client
    created = (await http.post("/api/cameras", json=payload())).json()
    listed = await http.get("/api/cameras")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [created["id"]]
    single = await http.get(f"/api/cameras/{created['id']}")
    assert single.status_code == 200
    assert set(single.json()) == CAMERA_KEYS


async def test_an_unknown_camera_is_404(client):
    http, _harness = client
    response = await http.get("/api/cameras/acam_ffffffff")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "camera_not_found"


# -- patch ------------------------------------------------------------------


async def test_patch_returns_the_post_operation_state(client):
    http, _harness = client
    created = (await http.post("/api/cameras", json=payload())).json()
    response = await http.patch(
        f"/api/cameras/{created['id']}", json={"name": "Renamed"}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"


@pytest.mark.parametrize("body", [{}, {"name": None}, {"enabled": None}])
async def test_an_empty_or_null_patch_is_422(client, body):
    http, _harness = client
    created = (await http.post("/api/cameras", json=payload())).json()
    response = await http.patch(f"/api/cameras/{created['id']}", json=body)
    assert response.status_code == 422


async def test_patching_an_unknown_camera_is_404(client):
    http, _harness = client
    response = await http.patch("/api/cameras/acam_ffffffff", json={"name": "x"})
    assert response.status_code == 404


async def test_a_patch_duplicate_vms_id_is_409(client):
    http, _harness = client
    first = (await http.post("/api/cameras", json=payload())).json()
    await http.post(
        "/api/cameras", json=payload(vms_camera_id="cam_00000002", name="Second")
    )
    response = await http.patch(
        f"/api/cameras/{first['id']}", json={"vms_camera_id": "cam_00000002"}
    )
    assert response.status_code == 409


# -- delete -----------------------------------------------------------------


async def test_delete_returns_204_with_an_empty_body(client):
    http, _harness = client
    created = (await http.post("/api/cameras", json=payload())).json()
    response = await http.delete(f"/api/cameras/{created['id']}")
    assert response.status_code == 204
    assert response.content == b""
    assert (await http.delete(f"/api/cameras/{created['id']}")).status_code == 404


# -- snapshot ---------------------------------------------------------------


async def test_snapshot_before_the_first_frame_is_409(client):
    http, _harness = client
    created = (await http.post("/api/cameras", json=payload())).json()
    response = await http.get(f"/api/cameras/{created['id']}/snapshot")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "snapshot_not_ready"


async def test_snapshot_returns_a_jpeg_with_the_frame_headers(client):
    import numpy as np

    from app.analytics.types import LatestFrame
    from app.domain.models import utc_now

    http, _harness = client
    created = (await http.post("/api/cameras", json=payload())).json()
    worker_fakes.FakeWorker.created[0].latest_frame = LatestFrame(
        sequence=12,
        rgb=np.zeros((24, 32, 3), dtype=np.uint8),
        received_at_utc=utc_now(),
        width=32,
        height=24,
        age_seconds=0.25,
    )
    response = await http.get(f"/api/cameras/{created['id']}/snapshot")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-sequence"] == "12"
    assert response.headers["x-frame-age-ms"] == "250"
    assert response.headers["x-frame-received-at"].endswith("Z")
    assert response.content[:2] == b"\xff\xd8"


async def test_snapshot_of_an_unknown_camera_is_404(client):
    http, _harness = client
    assert (await http.get("/api/cameras/acam_ffffffff/snapshot")).status_code == 404


# -- envelope and request id ------------------------------------------------


async def test_every_error_uses_the_one_envelope(client):
    http, _harness = client
    body = (await http.get("/api/cameras/acam_ffffffff")).json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details", "request_id"}
    assert body["error"]["request_id"].startswith("req_")


async def test_an_inbound_request_id_is_echoed_when_it_is_safe(client):
    http, _harness = client
    response = await http.get("/api/cameras", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


async def test_an_unsafe_inbound_request_id_is_replaced(client):
    http, _harness = client
    response = await http.get("/api/cameras", headers={"X-Request-ID": "a" * 200})
    assert response.headers["X-Request-ID"].startswith("req_")


async def test_security_headers_are_present(client):
    http, _harness = client
    response = await http.get("/api/cameras")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
