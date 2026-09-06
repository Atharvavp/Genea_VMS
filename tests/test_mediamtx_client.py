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
    MediaMTXPlaybackClient,
    MediaMTXUnavailable,
    PathNotFound,
    PathRejected,
    PathRuntime,
    RecordingPathConfig,
    RecordingUnavailable,
)

BASE = "http://vms-mediamtx:9997"
PLAYBACK_BASE = "http://vms-mediamtx:9996"
SECRET_URL = "rtsp://admin:hunter2@10.0.0.9:554/Streaming"

RECORDING = RecordingPathConfig(
    record_path="/recordings/%path/%Y-%m-%d/%H-%M-%S-%f",
    segment_duration="5m",
    delete_after="24h",
)


def make_client(handler):
    transport = httpx.MockTransport(handler)
    return MediaMTXClient(BASE, client=httpx.AsyncClient(transport=transport))


def make_playback_client(handler):
    transport = httpx.MockTransport(handler)
    return MediaMTXPlaybackClient(
        PLAYBACK_BASE, client=httpx.AsyncClient(transport=transport)
    )


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

    await make_client(handler).ensure_path(
        "vms_cam_00000001", SECRET_URL, record=True, recording=RECORDING
    )
    assert captured["method"] == "POST"
    assert captured["path"] == "/v3/config/paths/replace/vms_cam_00000001"
    assert captured["body"] == {
        "source": SECRET_URL,
        "sourceOnDemand": False,
        "rtspTransport": "tcp",
        "record": True,
        "recordPath": "/recordings/%path/%Y-%m-%d/%H-%M-%S-%f",
        "recordFormat": "fmp4",
        "recordPartDuration": "1s",
        "recordMaxPartSize": "50M",
        "recordSegmentDuration": "5m",
        "recordDeleteAfter": "24h",
    }


async def test_ensure_path_uses_pull_always():
    """Health must not depend on a viewer being connected."""
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok"})

    await make_client(handler).ensure_path(
        "vms_cam_00000001", "rtsp://cam/live", record=False, recording=RECORDING
    )
    assert captured["sourceOnDemand"] is False
    assert captured["record"] is False


async def test_recording_off_still_sends_the_whole_recording_block():
    """The regression that makes existing history invisible.

    Verified against 1.20.1: a payload without the recording keys resets
    `recordPath` to MediaMTX's default, and the playback server then finds
    nothing under the configured root. So `record: false` must still carry the
    full block - turning recording off must not hide what was already recorded.
    """
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok"})

    await make_client(handler).ensure_path(
        "vms_cam_00000001", "rtsp://cam/live", record=False, recording=RECORDING
    )
    assert captured["record"] is False
    assert captured["recordPath"] == "/recordings/%path/%Y-%m-%d/%H-%M-%S-%f"
    assert captured["recordSegmentDuration"] == "5m"
    assert captured["recordDeleteAfter"] == "24h"


async def test_ensure_path_carries_the_configured_segment_and_retention():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok"})

    await make_client(handler).ensure_path(
        "vms_cam_00000001",
        "rtsp://cam/live",
        record=True,
        recording=RecordingPathConfig(
            record_path="/media/%path/%Y-%m-%d/%H-%M-%S-%f",
            segment_duration="2s",
            delete_after="0.01h",
        ),
    )
    assert captured["recordPath"] == "/media/%path/%Y-%m-%d/%H-%M-%S-%f"
    assert captured["recordSegmentDuration"] == "2s"
    assert captured["recordDeleteAfter"] == "0.01h"


# --- config comparison ------------------------------------------------------


def _config_payload(**overrides):
    payload = {
        "name": "vms_cam_00000001",
        "source": SECRET_URL,
        "sourceOnDemand": False,
        "rtspTransport": "tcp",
        "record": True,
        "recordPath": "/recordings/%path/%Y-%m-%d/%H-%M-%S-%f",
        "recordFormat": "fmp4",
        "recordPartDuration": "1s",
        "recordMaxPartSize": "50M",
        # MediaMTX normalises durations on read-back, verified on 1.20.1.
        "recordSegmentDuration": "5m0s",
        "recordDeleteAfter": "1d",
    }
    payload.update(overrides)
    return payload


async def test_config_matches_tolerates_normalised_durations():
    """`5m` reads back as `5m0s` and `24h` as `1d`; that is still a match."""

    def handler(request):
        assert request.url.path == "/v3/config/paths/get/vms_cam_00000001"
        return httpx.Response(200, json=_config_payload())

    assert (
        await make_client(handler).path_config_matches(
            "vms_cam_00000001", SECRET_URL, record=True, recording=RECORDING
        )
        is True
    )


@pytest.mark.parametrize(
    "override",
    [
        {"source": "rtsp://other/live"},
        {"record": False},
        {"recordPath": "./recordings/%path/%Y-%m-%d_%H-%M-%S-%f"},
        {"recordSegmentDuration": "1h0m0s"},
        {"recordDeleteAfter": "2d"},
        {"recordFormat": "mpegts"},
        {"sourceOnDemand": True},
        {"rtspTransport": "udp"},
    ],
)
async def test_config_does_not_match_when_anything_differs(override):
    def handler(request):
        return httpx.Response(200, json=_config_payload(**override))

    assert (
        await make_client(handler).path_config_matches(
            "vms_cam_00000001", SECRET_URL, record=True, recording=RECORDING
        )
        is False
    )


async def test_config_of_a_missing_path_does_not_match():
    def handler(request):
        return httpx.Response(404, json={"status": "error", "error": "path not found"})

    assert (
        await make_client(handler).path_config_matches(
            "vms_cam_gone", SECRET_URL, record=True, recording=RECORDING
        )
        is False
    )


async def test_config_comparison_returns_a_bool_not_the_credentialed_payload():
    """The config endpoint echoes the password, so only a bool may come back."""

    def handler(request):
        return httpx.Response(200, json=_config_payload())

    result = await make_client(handler).path_config_matches(
        "vms_cam_00000001", SECRET_URL, record=True, recording=RECORDING
    )
    assert isinstance(result, bool)
    assert "hunter2" not in json.dumps(result)


# --- playback server --------------------------------------------------------


async def test_playback_list_parses_timespans():
    def handler(request):
        assert request.url.path == "/list"
        assert request.url.params["path"] == "vms_cam_00000001"
        assert request.url.params["start"] == "2026-09-05T00:00:00Z"
        assert request.url.params["end"] == "2026-09-06T00:00:00Z"
        return httpx.Response(
            200,
            json=[
                {
                    "start": "2026-09-05T22:39:16.193314Z",
                    "duration": 11.060184555,
                    # MediaMTX's own URL reflects the Host header it was asked
                    # on, i.e. a compose-internal address. It must be ignored.
                    "url": "http://vms-mediamtx:9996/get?path=vms_cam_00000001",
                },
                {"start": "2026-09-05T10:00:00Z", "duration": 30.0},
            ],
        )

    spans = await make_playback_client(handler).list_timespans(
        "vms_cam_00000001", "2026-09-05T00:00:00Z", "2026-09-06T00:00:00Z"
    )
    # Sorted chronologically, whatever order MediaMTX answered in.
    assert [span.start for span in spans] == [
        "2026-09-05T10:00:00Z",
        "2026-09-05T22:39:16.193314Z",
    ]
    assert spans[1].duration_seconds == 11.060184555


async def test_playback_timespan_derives_its_end():
    def handler(request):
        return httpx.Response(
            200,
            json=[{"start": "2026-09-05T22:39:16.193314Z", "duration": 11.060184555}],
        )

    spans = await make_playback_client(handler).list_timespans("p", "a", "b")
    assert spans[0].end == "2026-09-05T22:39:27.253Z"


async def test_playback_list_treats_no_segments_as_an_empty_history():
    """A configured path with nothing that day is a normal, empty answer."""

    def handler(request):
        return httpx.Response(
            404, json={"status": "error", "error": "no recording segments found"}
        )

    assert await make_playback_client(handler).list_timespans("p", "a", "b") == []


async def test_a_path_that_has_never_recorded_is_an_empty_history():
    """MediaMTX only creates the directory on the first write.

    Verified on 1.20.1: `/list` for a configured path that has never recorded
    fails to stat the directory and answers 400. A camera whose recording was
    just switched on is in exactly that state, and it is empty, not broken.
    """

    def handler(request):
        return httpx.Response(
            400,
            json={
                "status": "error",
                "error": "lstat /recordings/vms_cam_00000001: no such file or directory",
            },
        )

    assert await make_playback_client(handler).list_timespans("p", "a", "b") == []


async def test_playback_list_of_an_unconfigured_path_is_unavailable():
    """What a disabled camera looks like: the files are there, the path is not."""

    def handler(request):
        return httpx.Response(
            400,
            json={"status": "error", "error": "path 'vms_cam_x' is not configured"},
        )

    with pytest.raises(RecordingUnavailable):
        await make_playback_client(handler).list_timespans("vms_cam_x", "a", "b")


async def test_playback_server_being_down_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(RecordingUnavailable):
        await make_playback_client(handler).list_timespans("p", "a", "b")


async def test_playback_errors_are_sanitised():
    def handler(request):
        return httpx.Response(500, text=f"boom {SECRET_URL}\nSet-Cookie: evil=1")

    with pytest.raises(RecordingUnavailable) as excinfo:
        await make_playback_client(handler).list_timespans("p", "a", "b")
    message = str(excinfo.value)
    assert "hunter2" not in message
    assert "\n" not in message


async def test_playback_list_skips_unparsable_entries():
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {"start": "not-a-date", "duration": 5.0},
                {"start": "2026-09-05T10:00:00Z"},
                {"start": "2026-09-05T11:00:00Z", "duration": 30.0},
            ],
        )

    spans = await make_playback_client(handler).list_timespans("p", "a", "b")
    assert [span.start for span in spans] == ["2026-09-05T11:00:00Z"]


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
        await make_client(handler).ensure_path(
            "vms_cam_00000001", "rtsp://cam/live", record=False, recording=RECORDING
        )


async def test_error_messages_never_carry_the_password():
    def handler(request):
        # Worst case: MediaMTX quotes the whole source URL back at us.
        return httpx.Response(
            400, json={"status": "error", "error": f"invalid source: '{SECRET_URL}'"}
        )

    with pytest.raises(PathRejected) as excinfo:
        await make_client(handler).ensure_path(
            "vms_cam_00000001", SECRET_URL, record=True, recording=RECORDING
        )
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
