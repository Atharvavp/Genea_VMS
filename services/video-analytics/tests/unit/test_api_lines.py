"""Singleton line routes."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.fakes import worker as worker_fakes
from tests.fakes.app_harness import build_harness

pytestmark = pytest.mark.unit


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
            created = await http.post(
                "/api/cameras",
                json={
                    "vms_camera_id": "cam_0123abcd",
                    "name": "Loading Bay",
                    "rtsp_url": "rtsp://host.docker.internal:8555/vms_cam_0123abcd",
                },
            )
            yield http, created.json()["id"]


def line_body(**over):
    body = {
        "name": "Entry line",
        "a": {"x": 0.2, "y": 0.5},
        "b": {"x": 0.8, "y": 0.5},
        "direction": "A_TO_B",
        "enabled": True,
    }
    body.update(over)
    return body


async def test_get_before_configuration_is_404_line_not_configured(client):
    http, camera_id = client
    response = await http.get(f"/api/cameras/{camera_id}/line")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "line_not_configured"


async def test_the_first_put_creates_and_returns_201_with_location(client):
    http, camera_id = client
    response = await http.put(f"/api/cameras/{camera_id}/line", json=line_body())
    assert response.status_code == 201
    assert response.headers["Location"] == f"/api/cameras/{camera_id}/line"
    body = response.json()
    assert body["id"].startswith("line_")
    assert body["camera_id"] == camera_id
    assert body["a"] == {"x": 0.2, "y": 0.5}
    assert body["direction"] == "A_TO_B"


async def test_a_second_put_updates_and_returns_200(client):
    http, camera_id = client
    first = (await http.put(f"/api/cameras/{camera_id}/line", json=line_body())).json()
    second = await http.put(
        f"/api/cameras/{camera_id}/line",
        json=line_body(name="Moved", direction="BOTH", b={"x": 0.9, "y": 0.9}),
    )
    assert second.status_code == 200
    assert "Location" not in second.headers
    assert second.json()["id"] == first["id"]
    assert second.json()["created_at"] == first["created_at"]
    assert second.json()["direction"] == "BOTH"


async def test_get_returns_the_stored_geometry(client):
    http, camera_id = client
    await http.put(f"/api/cameras/{camera_id}/line", json=line_body())
    body = (await http.get(f"/api/cameras/{camera_id}/line")).json()
    assert body["b"] == {"x": 0.8, "y": 0.5}
    assert body["enabled"] is True


async def test_configuring_a_line_flips_line_configured_on_the_camera(client):
    http, camera_id = client
    await http.put(f"/api/cameras/{camera_id}/line", json=line_body())
    camera = (await http.get(f"/api/cameras/{camera_id}")).json()
    assert camera["line_configured"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"a": {"x": -0.1, "y": 0.5}},
        {"b": {"x": 1.1, "y": 0.5}},
        {"a": {"x": 0.5, "y": 0.5}, "b": {"x": 0.5, "y": 0.5}},
        {"a": {"x": 0.5, "y": 0.5}, "b": {"x": 0.52, "y": 0.5}},
        {"direction": "a_to_b"},
        {"direction": "SIDEWAYS"},
        {"a": {"x": 0.2}},
        {"extra": 1},
    ],
)
async def test_invalid_line_bodies_are_422(client, body):
    http, camera_id = client
    response = await http.put(f"/api/cameras/{camera_id}/line", json=line_body(**body))
    assert response.status_code == 422


async def test_a_line_of_exactly_the_minimum_length_is_accepted(client):
    http, camera_id = client
    response = await http.put(
        f"/api/cameras/{camera_id}/line",
        json=line_body(a={"x": 0.5, "y": 0.5}, b={"x": 0.55, "y": 0.5}),
    )
    assert response.status_code == 201


async def test_delete_is_idempotent_and_returns_204(client):
    http, camera_id = client
    await http.put(f"/api/cameras/{camera_id}/line", json=line_body())
    first = await http.delete(f"/api/cameras/{camera_id}/line")
    second = await http.delete(f"/api/cameras/{camera_id}/line")
    assert first.status_code == second.status_code == 204
    assert first.content == b""


@pytest.mark.parametrize("method", ["get", "put", "delete"])
async def test_line_routes_on_an_unknown_camera_are_404(client, method):
    http, _camera_id = client
    call = getattr(http, method)
    kwargs = {"json": line_body()} if method == "put" else {}
    response = await call("/api/cameras/acam_ffffffff/line", **kwargs)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "camera_not_found"
