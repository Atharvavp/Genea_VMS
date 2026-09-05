"""REST contracts, the error envelope, and what must never appear in a response."""

from __future__ import annotations

import pytest

from tests.conftest import CREDENTIALED_URL, SIMULATOR_URL

OTHER_URL = "rtsp://host.docker.internal:8554/simulator/dock"


async def create(client, name="Lobby", url=SIMULATOR_URL, enabled=True):
    response = await client.post(
        "/api/cameras", json={"name": name, "rtsp_url": url, "enabled": enabled}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- health -----------------------------------------------------------------


async def test_health_ok(client, manager, fake_mediamtx):
    await manager.refresh_health()
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mediamtx"]["reachable"] is True
    assert body["cameras"] == {"total": 0, "enabled": 0, "online": 0}
    assert body["webrtc_base_url"] == "http://localhost:8889"


async def test_health_503_when_mediamtx_is_unreachable(client, manager, fake_mediamtx):
    fake_mediamtx.go_down()
    await manager.refresh_health()
    response = await client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


# --- list / create / get ----------------------------------------------------


async def test_list_is_empty_initially(client):
    response = await client.get("/api/cameras")
    assert response.status_code == 200
    assert response.json() == []


async def test_create_returns_the_full_view(client, fake_mediamtx):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    body = await create(client)

    assert body["name"] == "Lobby"
    assert body["enabled"] is True
    assert body["has_credentials"] is False
    assert body["rtsp_url_display"] == SIMULATOR_URL
    assert body["mediamtx_path"] == f"vms_{body['id']}"
    assert body["webrtc_url"] == f"http://localhost:8889/vms_{body['id']}/whep"
    assert body["health"]["state"] == "ONLINE"
    assert set(body) == {
        "id",
        "name",
        "rtsp_url_display",
        "has_credentials",
        "enabled",
        "mediamtx_path",
        "webrtc_url",
        "health",
        "created_at",
        "updated_at",
    }


async def test_response_never_contains_the_raw_rtsp_url(client):
    body = await create(client, url=CREDENTIALED_URL)
    assert "hunter2" not in str(body)
    assert body["rtsp_url_display"].endswith("/Streaming/Channels/101")
    assert ":***@" in body["rtsp_url_display"]
    assert body["has_credentials"] is True
    assert "rtsp_url" not in body  # no field carries the real URL


async def test_get_camera_also_masks(client):
    created = await create(client, url=CREDENTIALED_URL)
    body = (await client.get(f"/api/cameras/{created['id']}")).json()
    assert "hunter2" not in str(body)


async def test_list_is_ordered_by_creation(client):
    first = await create(client, name="First")
    second = await create(client, name="Second", url=OTHER_URL)
    ids = [item["id"] for item in (await client.get("/api/cameras")).json()]
    assert ids == [first["id"], second["id"]]


async def test_create_disabled(client, fake_mediamtx):
    body = await create(client, enabled=False)
    assert body["enabled"] is False
    assert fake_mediamtx.paths == {}


async def test_offline_source_is_accepted(client):
    body = await create(client, url="rtsp://192.0.2.1:554/nothing")
    assert body["health"]["state"] == "OFFLINE"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "", "rtsp_url": SIMULATOR_URL},
        {"name": "ok", "rtsp_url": "http://cam/live"},
        {"name": "ok", "rtsp_url": ""},
        {"name": "ok"},
        {"rtsp_url": SIMULATOR_URL},
        {"name": "ok", "rtsp_url": SIMULATOR_URL, "mediamtx_path": "mine"},
    ],
)
async def test_create_validation_errors(client, payload):
    response = await client.post("/api/cameras", json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["fields"]


async def test_validation_error_does_not_echo_a_password(client):
    response = await client.post(
        "/api/cameras",
        json={"name": "ok", "rtsp_url": "http://admin:hunter2@cam/live"},
    )
    assert response.status_code == 422
    assert "hunter2" not in response.text


async def test_get_missing_camera(client):
    response = await client.get("/api/cameras/cam_deadbeef")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "camera_not_found"


# --- update -----------------------------------------------------------------


async def test_patch_name_only(client, fake_mediamtx):
    created = await create(client)
    fake_mediamtx.calls.clear()

    response = await client.patch(
        f"/api/cameras/{created['id']}", json={"name": "Front Door"}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Front Door"
    assert fake_mediamtx.calls == []


async def test_patch_url_keeps_the_path_and_webrtc_url(client, fake_mediamtx):
    created = await create(client)
    body = (
        await client.patch(f"/api/cameras/{created['id']}", json={"rtsp_url": OTHER_URL})
    ).json()
    assert body["mediamtx_path"] == created["mediamtx_path"]
    assert body["webrtc_url"] == created["webrtc_url"]
    assert fake_mediamtx.paths[created["mediamtx_path"]] == OTHER_URL


async def test_patch_without_url_keeps_the_credentialed_source(client, fake_mediamtx):
    created = await create(client, url=CREDENTIALED_URL)
    await client.patch(f"/api/cameras/{created['id']}", json={"name": "Renamed"})
    assert fake_mediamtx.paths[created["mediamtx_path"]] == CREDENTIALED_URL


async def test_patch_empty_url_is_rejected(client):
    created = await create(client)
    response = await client.patch(f"/api/cameras/{created['id']}", json={"rtsp_url": ""})
    assert response.status_code == 422


async def test_patch_unknown_field_is_rejected(client):
    created = await create(client)
    response = await client.patch(
        f"/api/cameras/{created['id']}", json={"health": "ONLINE"}
    )
    assert response.status_code == 422


async def test_patch_missing_camera(client):
    response = await client.patch("/api/cameras/cam_deadbeef", json={"name": "x"})
    assert response.status_code == 404


# --- enable / disable / status ---------------------------------------------


async def test_disable_then_enable(client, fake_mediamtx):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    created = await create(client)

    disabled = (await client.post(f"/api/cameras/{created['id']}/disable")).json()
    assert disabled["enabled"] is False
    assert fake_mediamtx.paths == {}

    enabled = (await client.post(f"/api/cameras/{created['id']}/enable")).json()
    assert enabled["enabled"] is True
    assert set(fake_mediamtx.paths) == {created["mediamtx_path"]}


async def test_enable_missing_camera(client):
    assert (await client.post("/api/cameras/cam_deadbeef/enable")).status_code == 404


async def test_status_endpoint(client, manager, fake_mediamtx):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    created = await create(client)
    await manager.refresh_health()

    body = (await client.get(f"/api/cameras/{created['id']}/status")).json()
    assert body["state"] == "ONLINE"
    assert body["mediamtx_available"] is True
    assert body["checked_at"]


async def test_status_of_missing_camera(client):
    assert (
        await client.get("/api/cameras/cam_deadbeef/status")
    ).status_code == 404


# --- delete -----------------------------------------------------------------


async def test_delete(client, fake_mediamtx):
    keep = await create(client, name="Keep")
    drop = await create(client, name="Drop", url=OTHER_URL)

    response = await client.delete(f"/api/cameras/{drop['id']}")
    assert response.status_code == 204
    assert response.content == b""

    ids = [item["id"] for item in (await client.get("/api/cameras")).json()]
    assert ids == [keep["id"]]
    assert set(fake_mediamtx.paths) == {keep["mediamtx_path"]}


async def test_delete_missing_camera(client):
    assert (await client.delete("/api/cameras/cam_deadbeef")).status_code == 404


# --- failure isolation ------------------------------------------------------


async def test_mediamtx_outage_does_not_fail_mutations(client, fake_mediamtx):
    """The registration is desired state; an outage is reported, not enforced."""
    fake_mediamtx.go_down()

    created = await create(client)
    assert created["health"]["state"] == "UNKNOWN"
    assert created["health"]["mediamtx_available"] is False

    patched = await client.patch(f"/api/cameras/{created['id']}", json={"name": "New"})
    assert patched.status_code == 200
    assert (await client.delete(f"/api/cameras/{created['id']}")).status_code == 204


async def test_one_camera_failing_leaves_the_others_listed(client, manager, fake_mediamtx):
    good = await create(client, name="Good")
    bad = await create(client, name="Bad", url="rtsp://192.0.2.1:554/nope")
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    await manager.refresh_health()

    by_id = {item["id"]: item for item in (await client.get("/api/cameras")).json()}
    assert by_id[good["id"]]["health"]["state"] == "ONLINE"
    assert by_id[bad["id"]]["health"]["state"] == "OFFLINE"


# --- app plumbing -----------------------------------------------------------


async def test_openapi_lists_the_camera_surface(client):
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert set(paths) >= {
        "/api/cameras",
        "/api/cameras/{camera_id}",
        "/api/cameras/{camera_id}/enable",
        "/api/cameras/{camera_id}/disable",
        "/api/cameras/{camera_id}/status",
        "/health",
    }


async def test_openapi_does_not_expose_a_raw_url_field(client):
    schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]
    assert "rtsp_url" not in schemas["CameraView"]["properties"]


async def test_ui_assets_are_not_cached_but_the_api_is_untouched(client):
    assert (await client.get("/")).headers["cache-control"] == "no-store, max-age=0"
    assert "cache-control" not in (await client.get("/api/cameras")).headers
    assert "cache-control" not in (await client.get("/health")).headers
