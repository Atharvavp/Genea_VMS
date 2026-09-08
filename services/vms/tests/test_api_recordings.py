"""The recordings API: listing, its four refusals, and what it must not leak.

The invariant that runs through this module: a historical-playback failure is
reported as one and never changes what the camera says about recording. A camera
can be recording perfectly while the playback server is unreachable.
"""

from __future__ import annotations

import pytest

from tests.conftest import CREDENTIALED_URL, SIMULATOR_URL

DAY = "2026-09-05"
SPAN_START = "2026-09-05T22:39:16.193314Z"


async def create(client, name="Lobby", url=SIMULATOR_URL, enabled=True, recording=True):
    response = await client.post(
        "/api/cameras",
        json={
            "name": name,
            "rtsp_url": url,
            "enabled": enabled,
            "recording_enabled": recording,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def list_recordings(client, camera_id, date=DAY):
    return await client.get(f"/api/recordings?camera_id={camera_id}&date={date}")


# --- listing ----------------------------------------------------------------


async def test_lists_recorded_timespans(client, fake_mediamtx):
    camera = await create(client)
    fake_mediamtx.record_history(camera["mediamtx_path"], SPAN_START, 11.060184555)

    response = await list_recordings(client, camera["id"])

    assert response.status_code == 200
    body = response.json()
    assert body["camera_id"] == camera["id"]
    assert body["camera_name"] == "Lobby"
    assert body["date"] == DAY
    assert len(body["items"]) == 1

    item = body["items"][0]
    assert item["start_time"] == SPAN_START
    assert item["end_time"] == "2026-09-05T22:39:27.253Z"
    assert item["duration_seconds"] == 11.060184555
    assert item["source"] == "mediamtx"
    assert item["id"].startswith("rec_")


async def test_playback_url_points_at_the_public_playback_server(client, fake_mediamtx):
    camera = await create(client)
    fake_mediamtx.record_history(camera["mediamtx_path"], SPAN_START, 11.06)

    item = (await list_recordings(client, camera["id"])).json()["items"][0]

    assert item["playback_url"].startswith("http://localhost:9996/get?")
    # The browser cannot reach the compose network, and must never be sent the
    # Control API's address either.
    assert "vms-mediamtx" not in item["playback_url"]
    assert "9997" not in item["playback_url"]
    # `format=mp4` is what makes the file start with its moov box, so a native
    # <video> element can play and seek it.
    assert "format=mp4" in item["playback_url"]
    assert f"path={camera['mediamtx_path']}" in item["playback_url"]


async def test_items_are_chronological(client, fake_mediamtx):
    camera = await create(client)
    for start in (
        "2026-09-05T22:00:00Z",
        "2026-09-05T06:00:00Z",
        "2026-09-05T14:00:00Z",
    ):
        fake_mediamtx.record_history(camera["mediamtx_path"], start, 30.0)

    items = (await list_recordings(client, camera["id"])).json()["items"]

    assert [item["start_time"] for item in items] == [
        "2026-09-05T06:00:00Z",
        "2026-09-05T14:00:00Z",
        "2026-09-05T22:00:00Z",
    ]


async def test_recording_ids_are_stable_and_carry_no_path(client, fake_mediamtx):
    camera = await create(client)
    fake_mediamtx.record_history(camera["mediamtx_path"], SPAN_START, 11.06)

    first = (await list_recordings(client, camera["id"])).json()["items"][0]["id"]
    second = (await list_recordings(client, camera["id"])).json()["items"][0]["id"]

    assert first == second
    assert "/" not in first and "recordings" not in first


async def test_an_empty_day_is_an_empty_list_not_an_error(client, fake_mediamtx):
    """A camera that simply has not recorded today is not a failure."""
    camera = await create(client)

    response = await list_recordings(client, camera["id"], date="2026-09-04")

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_only_the_requested_day_is_returned(client, fake_mediamtx):
    camera = await create(client)
    fake_mediamtx.record_history(camera["mediamtx_path"], "2026-09-04T23:59:59Z", 5.0)
    fake_mediamtx.record_history(camera["mediamtx_path"], SPAN_START, 11.06)
    fake_mediamtx.record_history(camera["mediamtx_path"], "2026-09-06T00:00:00Z", 5.0)

    items = (await list_recordings(client, camera["id"])).json()["items"]

    assert [item["start_time"] for item in items] == [SPAN_START]


async def test_cameras_do_not_see_each_others_recordings(client, fake_mediamtx):
    first = await create(client, name="First")
    second = await create(
        client, name="Second", url="rtsp://host.docker.internal:8554/simulator/dock"
    )
    fake_mediamtx.record_history(first["mediamtx_path"], SPAN_START, 11.06)

    assert len((await list_recordings(client, first["id"])).json()["items"]) == 1
    assert (await list_recordings(client, second["id"])).json()["items"] == []


async def test_recording_off_still_lists_existing_history(client, fake_mediamtx):
    """Turning recording off stops new segments; it does not hide old ones."""
    camera = await create(client)
    fake_mediamtx.record_history(camera["mediamtx_path"], SPAN_START, 11.06)

    await client.patch(
        f"/api/cameras/{camera['id']}", json={"recording_enabled": False}
    )

    response = await list_recordings(client, camera["id"])
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


# --- refusals ---------------------------------------------------------------


async def test_unknown_camera_is_a_404(client):
    response = await list_recordings(client, "cam_deadbeef")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "camera_not_found"


async def test_disabled_camera_history_is_a_409(client, fake_mediamtx):
    """Component 2 disable removes the path, and MediaMTX needs it configured."""
    camera = await create(client)
    fake_mediamtx.record_history(camera["mediamtx_path"], SPAN_START, 11.06)
    await client.post(f"/api/cameras/{camera['id']}/disable")

    response = await list_recordings(client, camera["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "disabled_camera_history_unavailable"


async def test_re_enabling_makes_the_history_visible_again(client, fake_mediamtx):
    camera = await create(client)
    fake_mediamtx.record_history(camera["mediamtx_path"], SPAN_START, 11.06)
    await client.post(f"/api/cameras/{camera['id']}/disable")
    await client.post(f"/api/cameras/{camera['id']}/enable")

    response = await list_recordings(client, camera["id"])

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


@pytest.mark.parametrize(
    "date", ["", "05-09-2026", "2026-9-5", "2026-13-01", "2026-02-30", "yesterday",
             "2026-09-05T00:00:00Z", "../../etc/passwd"]
)
async def test_a_bad_date_is_a_422(client, date):
    camera = await create(client)
    response = await list_recordings(client, camera["id"], date=date)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_missing_query_parameters_are_a_422(client):
    assert (await client.get("/api/recordings")).status_code == 422
    assert (await client.get("/api/recordings?camera_id=cam_1")).status_code == 422


async def test_playback_server_down_is_a_503(client, fake_mediamtx):
    camera = await create(client)
    fake_mediamtx.playback_up = False

    response = await list_recordings(client, camera["id"])

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "recording_unavailable"


async def test_a_configured_but_unrecorded_path_is_not_a_failure(client, fake_mediamtx):
    camera = await create(client, recording=False)
    response = await list_recordings(client, camera["id"])
    assert response.status_code == 200
    assert response.json()["items"] == []


# --- the two axes stay apart ------------------------------------------------


async def test_a_playback_outage_does_not_change_recording_state(
    client, manager, fake_mediamtx
):
    """The central separation: playback failing is not the camera failing."""
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    camera = await create(client)
    await manager.refresh_health()
    assert (await client.get(f"/api/cameras/{camera['id']}")).json()["recording"][
        "state"
    ] == "RECORDING"

    fake_mediamtx.playback_up = False
    assert (await list_recordings(client, camera["id"])).status_code == 503

    after = (await client.get(f"/api/cameras/{camera['id']}")).json()
    assert after["recording"]["state"] == "RECORDING"
    assert after["recording"]["last_error"] is None
    assert after["health"]["state"] == "ONLINE"


async def test_a_playback_outage_does_not_change_camera_health(
    client, manager, fake_mediamtx
):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    camera = await create(client)
    await manager.refresh_health()

    fake_mediamtx.playback_up = False
    await list_recordings(client, camera["id"])
    await manager.refresh_health()

    body = (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["mediamtx"]["reachable"] is True


# --- what must never appear -------------------------------------------------


async def test_the_response_never_carries_credentials_or_host_paths(
    client, fake_mediamtx
):
    camera = await create(client, url=CREDENTIALED_URL)
    fake_mediamtx.record_history(camera["mediamtx_path"], SPAN_START, 11.06)

    text = (await list_recordings(client, camera["id"])).text

    assert "hunter2" not in text
    assert "rtsp://" not in text
    # No filesystem path, and no Control API address.
    assert "/recordings/" not in text
    assert "9997" not in text


async def test_the_recording_schema_exposes_no_file_path(client):
    schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]
    properties = schemas["RecordingItem"]["properties"]
    assert set(properties) == {
        "id",
        "start_time",
        "end_time",
        "duration_seconds",
        "playback_url",
        "source",
    }
    for forbidden in ("path", "file", "file_path", "mediamtx_path", "storage_path"):
        assert forbidden not in properties
