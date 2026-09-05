"""Exact Control API calls, error mapping and secret handling.

HTTP is faked with an httpx MockTransport, so these assert the wire format the
real 1.20.1 server was verified to accept, without needing it running. The
matching live checks are in tests/test_integration_mediamtx.py.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.mediamtx_client import (
    MediaMTXClient,
    MediaMTXError,
    MediaMTXUnavailable,
    PathNotFound,
    PathRejected,
    PathRuntime,
)

BASE = "http://vms-mediamtx:9997"
SECRET_URL = "rtsp://admin:hunter2@10.0.0.9:554/Streaming"


def make_client(handler):
    transport = httpx.MockTransport(handler)
    return MediaMTXClient(BASE, client=httpx.AsyncClient(transport=transport))


def path_item(name, available=False, ready=False, source_type="rtspSource"):
    return {
        "name": name,
        "confName": name,
        "ready": ready,
        "available": available,
        "online": True,
        "source": {"type": source_type, "id": ""},
        "tracks": ["H264"] if ready else [],
        "readers": [],
    }


# --- reads ------------------------------------------------------------------


async def test_get_info():
    def handler(request):
        assert request.url.path == "/v3/info"
        return httpx.Response(200, json={"version": "v1.20.1", "started": "2026-01-01"})

    info = await make_client(handler).get_info()
    assert info.version == "v1.20.1"
    assert info.started == "2026-01-01"


async def test_list_paths_maps_runtime_state():
    def handler(request):
        assert request.url.path == "/v3/paths/list"
        return httpx.Response(
            200,
            json={
                "itemCount": 2,
                "pageCount": 1,
                "items": [
                    path_item("vms_cam_00000001", available=True, ready=True),
                    path_item("vms_cam_00000002"),
                ],
            },
        )

    paths = await make_client(handler).list_paths()
    assert set(paths) == {"vms_cam_00000001", "vms_cam_00000002"}
    assert paths["vms_cam_00000001"].available is True
    assert paths["vms_cam_00000001"].tracks == ("H264",)
    assert paths["vms_cam_00000002"].available is False


async def test_list_paths_follows_pagination():
    seen_pages = []

    def handler(request):
        page = int(request.url.params["page"])
        seen_pages.append(page)
        return httpx.Response(
            200,
            json={
                "itemCount": 2,
                "pageCount": 2,
                "items": [path_item(f"vms_cam_page{page}")],
            },
        )

    paths = await make_client(handler).list_paths()
    assert seen_pages == [0, 1]
    assert set(paths) == {"vms_cam_page0", "vms_cam_page1"}


async def test_get_path():
    def handler(request):
        assert request.url.path == "/v3/paths/get/vms_cam_00000001"
        return httpx.Response(200, json=path_item("vms_cam_00000001", available=True))

    runtime = await make_client(handler).get_path("vms_cam_00000001")
    assert runtime.available is True
    assert runtime.source_type == "rtspSource"


async def test_get_path_missing_raises_path_not_found():
    def handler(request):
        return httpx.Response(404, json={"status": "error", "error": "path not found"})

    with pytest.raises(PathNotFound):
        await make_client(handler).get_path("vms_cam_gone")


async def test_list_configured_path_names_drops_the_credentialed_payload():
    def handler(request):
        assert request.url.path == "/v3/config/paths/list"
        return httpx.Response(
            200,
            json={
                "itemCount": 1,
                "pageCount": 1,
                # The real server echoes the source URL, password and all.
                "items": [{"name": "vms_cam_00000001", "source": SECRET_URL}],
            },
        )

    names = await make_client(handler).list_configured_path_names()
    assert names == ["vms_cam_00000001"]
    assert "hunter2" not in json.dumps(names)


# --- writes -----------------------------------------------------------------


async def test_ensure_path_sends_the_verified_payload():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok"})

    await make_client(handler).ensure_path("vms_cam_00000001", SECRET_URL)
    assert captured["method"] == "POST"
    assert captured["path"] == "/v3/config/paths/replace/vms_cam_00000001"
    assert captured["body"] == {
        "source": SECRET_URL,
        "sourceOnDemand": False,
        "rtspTransport": "tcp",
        "record": False,
    }


async def test_ensure_path_uses_pull_always_and_no_recording():
    """Health must not depend on a viewer, and recording is Component 3."""
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok"})

    await make_client(handler).ensure_path("vms_cam_00000001", "rtsp://cam/live")
    assert captured["sourceOnDemand"] is False
    assert captured["record"] is False


async def test_delete_path():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"status": "ok"})

    assert await make_client(handler).delete_path("vms_cam_00000001") is True
    assert captured == {
        "method": "DELETE",
        "path": "/v3/config/paths/delete/vms_cam_00000001",
    }


async def test_delete_path_missing_is_not_an_error():
    def handler(request):
        return httpx.Response(404, json={"status": "error", "error": "path not found"})

    assert await make_client(handler).delete_path("vms_cam_gone") is False


# --- errors -----------------------------------------------------------------


async def test_unreachable_api_raises_mediamtx_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(MediaMTXUnavailable):
        await make_client(handler).get_info()


async def test_timeout_raises_mediamtx_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(MediaMTXUnavailable):
        await make_client(handler).list_paths()


async def test_auth_error_raises_mediamtx_error():
    def handler(request):
        return httpx.Response(
            401, json={"status": "error", "error": "authentication error"}
        )

    with pytest.raises(MediaMTXError) as excinfo:
        await make_client(handler).get_info()
    assert "401" in str(excinfo.value)


async def test_rejected_source_raises_path_rejected():
    def handler(request):
        return httpx.Response(
            400, json={"status": "error", "error": "invalid source: 'notaurl'"}
        )

    with pytest.raises(PathRejected):
        await make_client(handler).ensure_path("vms_cam_00000001", "rtsp://cam/live")


async def test_error_messages_never_carry_the_password():
    def handler(request):
        # Worst case: MediaMTX quotes the whole source URL back at us.
        return httpx.Response(
            400, json={"status": "error", "error": f"invalid source: '{SECRET_URL}'"}
        )

    with pytest.raises(PathRejected) as excinfo:
        await make_client(handler).ensure_path("vms_cam_00000001", SECRET_URL)
    message = str(excinfo.value)
    assert "hunter2" not in message
    assert "admin:***@10.0.0.9" in message


async def test_error_messages_have_no_control_characters():
    def handler(request):
        return httpx.Response(500, text="boom\nSet-Cookie: evil=1")

    with pytest.raises(MediaMTXError) as excinfo:
        await make_client(handler).get_info()
    assert "\n" not in str(excinfo.value)


# --- model ------------------------------------------------------------------


def test_path_runtime_tolerates_a_sparse_payload():
    runtime = PathRuntime.from_api({"name": "vms_cam_00000001"})
    assert runtime.available is False
    assert runtime.ready is False
    assert runtime.source_type is None
    assert runtime.tracks == ()
