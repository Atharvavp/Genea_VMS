"""HTTP API behaviour, exercised through FastAPI's TestClient."""

from __future__ import annotations

import json
import time

import pytest

VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"payload" * 512


def upload_files(filename: str = "parking.mp4"):
    return {"file": (filename, VIDEO_BYTES, "video/mp4")}


def create(client, payload: dict, filename: str = "parking.mp4"):
    return client.post(
        "/api/cameras",
        data={"payload": json.dumps(payload)},
        files=upload_files(filename),
    )


def wait_for_status(client, camera_id: str, expected: str, timeout: float = 6.0) -> str:
    deadline = time.time() + timeout
    status = ""
    while time.time() < deadline:
        status = client.get(f"/api/cameras/{camera_id}").json()["status"]
        if status == expected:
            return status
        time.sleep(0.05)
    return status


# --- health & basics ----------------------------------------------------


def test_health_reports_dependencies(client) -> None:
    response = client.get("/health")
    body = response.json()
    assert body["database"] is True
    assert set(body) >= {"status", "ffmpeg", "ffprobe", "database", "cameras"}


def test_index_page_is_served(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "RTSP Camera Simulator" in response.text


def test_empty_camera_list(client) -> None:
    assert client.get("/api/cameras").json() == []


# --- create -------------------------------------------------------------


def test_create_camera_with_upload(client) -> None:
    response = create(client, {"name": "Parking Entrance"})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Parking Entrance"
    assert body["stream_path"] == "parking-entrance"
    assert body["rtsp_url"] == "rtsp://localhost:8554/simulator/parking-entrance"
    assert body["status"] == "STOPPED"
    assert body["source"]["filename"] == "parking.mp4"
    assert body["source"]["width"] == 1920
    assert body["video"] == {
        "codec": "h264",
        "resolution": {"mode": "source"},
        "fps": {"mode": "source"},
        "bitrate": {"mode": "auto"},
    }
    assert client.get(f"/api/cameras/{body['id']}").json()["id"] == body["id"]
    assert len(client.get("/api/cameras").json()) == 1


def test_create_with_full_video_config(client) -> None:
    payload = {
        "name": "Loading Bay",
        "stream_path": "loading-bay",
        "loop": False,
        "video": {
            "codec": "h265",
            "resolution": {"mode": "fixed", "width": 1280, "height": 720},
            "fps": {"mode": "fixed", "value": 15},
            "bitrate": {"mode": "fixed", "kbps": 2500},
        },
    }
    body = create(client, payload).json()
    assert body["video"]["codec"] == "h265"
    assert body["video"]["resolution"] == {"mode": "fixed", "width": 1280, "height": 720}
    assert body["loop"] is False


def test_create_with_auto_start_runs_the_publisher(client) -> None:
    body = create(client, {"name": "Auto Cam", "auto_start": True}).json()
    assert body["status"] == "RUNNING"
    assert body["runtime"]["pid"]


def test_create_with_auto_start_failure_still_returns_201(client, settings, stub_bin) -> None:
    settings.ffmpeg_binary = str(stub_bin["failing"])
    response = create(client, {"name": "Broken", "auto_start": True})
    assert response.status_code == 201
    assert response.json()["status"] == "ERROR"
    assert "Connection refused" in response.json()["last_error"]


def test_create_from_a_mounted_source(client, settings) -> None:
    (settings.source_video_dir / "lobby.mp4").write_bytes(VIDEO_BYTES)
    response = client.post(
        "/api/cameras",
        json={"name": "Lobby", "source": {"kind": "local", "local_path": "lobby.mp4"}},
    )
    assert response.status_code == 201
    assert response.json()["source_kind"] == "local"
    assert response.json()["source"]["filename"] == "lobby.mp4"


def test_local_sources_are_listed(client, settings) -> None:
    (settings.source_video_dir / "lobby.mp4").write_bytes(VIDEO_BYTES)
    body = client.get("/api/local-sources").json()
    assert body["files"] == ["lobby.mp4"]
    assert body["directory"] == str(settings.source_video_dir)


# --- create validation --------------------------------------------------


def test_duplicate_stream_path_conflicts(client) -> None:
    create(client, {"name": "A", "stream_path": "front-door"})
    response = create(client, {"name": "B", "stream_path": "front-door"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "duplicate_stream_path"


def test_missing_source_is_rejected(client) -> None:
    response = client.post("/api/cameras", json={"name": "No Source"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_unprobeable_video_is_rejected_and_leaves_no_files(client, settings) -> None:
    response = create(client, {"name": "Broken"}, filename="bad-clip.mp4")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_source"
    assert list(settings.upload_video_dir.iterdir()) == []
    assert list((settings.data_dir / "tmp").iterdir()) == []
    assert client.get("/api/cameras").json() == []


def test_unsupported_extension_is_rejected(client) -> None:
    response = create(client, {"name": "Doc"}, filename="notes.txt")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_source"


def test_invalid_video_configuration_is_rejected(client) -> None:
    response = create(
        client,
        {"name": "Odd", "video": {"resolution": {"mode": "fixed", "width": 1281, "height": 720}}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["details"]["fields"]


def test_invalid_stream_path_is_rejected(client) -> None:
    assert create(client, {"name": "X", "stream_path": "Bad Path"}).status_code == 422


def test_local_path_outside_the_mount_is_rejected(client) -> None:
    response = client.post(
        "/api/cameras",
        json={"name": "Escape", "source": {"kind": "local", "local_path": "../../etc/passwd"}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_source"


def test_upload_and_local_source_together_is_rejected(client, settings) -> None:
    (settings.source_video_dir / "lobby.mp4").write_bytes(VIDEO_BYTES)
    response = client.post(
        "/api/cameras",
        data={
            "payload": json.dumps(
                {"name": "Both", "source": {"kind": "local", "local_path": "lobby.mp4"}}
            )
        },
        files=upload_files(),
    )
    assert response.status_code == 400


def test_malformed_multipart_payload_is_rejected(client) -> None:
    response = client.post(
        "/api/cameras", data={"payload": "{not json"}, files=upload_files()
    )
    assert response.status_code == 400


def test_unsupported_content_type_is_rejected(client) -> None:
    response = client.post(
        "/api/cameras", content="name=x", headers={"content-type": "text/plain"}
    )
    assert response.status_code == 400


# --- read / update / delete --------------------------------------------


def test_unknown_camera_returns_404(client) -> None:
    assert client.get("/api/cameras/cam_missing").status_code == 404
    assert client.post("/api/cameras/cam_missing/start").status_code == 404
    assert client.delete("/api/cameras/cam_missing").status_code == 404
    assert client.patch("/api/cameras/cam_missing", json={"name": "x"}).status_code == 404
    assert client.get("/api/cameras/cam_missing").json()["error"]["code"] == "camera_not_found"


def test_patch_stopped_camera_persists_without_starting(client) -> None:
    camera = create(client, {"name": "Dock"}).json()
    response = client.patch(
        f"/api/cameras/{camera['id']}",
        json={"name": "Dock North", "video": {"codec": "h265"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Dock North"
    assert body["video"]["codec"] == "h265"
    assert body["status"] == "STOPPED"
    assert body["runtime"]["pid"] is None


def test_patch_running_camera_restarts_it(client) -> None:
    camera = create(client, {"name": "Gate"}).json()
    running = client.post(f"/api/cameras/{camera['id']}/start").json()
    assert running["status"] == "RUNNING"

    updated = client.patch(
        f"/api/cameras/{camera['id']}",
        json={"video": {"fps": {"mode": "fixed", "value": 12}}},
    ).json()
    assert updated["status"] == "RUNNING"
    assert updated["runtime"]["pid"] != running["runtime"]["pid"]


def test_patch_name_only_keeps_the_same_process(client) -> None:
    camera = create(client, {"name": "Gate"}).json()
    running = client.post(f"/api/cameras/{camera['id']}/start").json()
    updated = client.patch(f"/api/cameras/{camera['id']}", json={"name": "Gate B"}).json()
    assert updated["runtime"]["pid"] == running["runtime"]["pid"]


def test_patch_to_a_taken_stream_path_conflicts(client) -> None:
    create(client, {"name": "A", "stream_path": "a-path"})
    other = create(client, {"name": "B", "stream_path": "b-path"}).json()
    response = client.patch(f"/api/cameras/{other['id']}", json={"stream_path": "a-path"})
    assert response.status_code == 409


def test_patch_can_replace_the_source_file(client) -> None:
    camera = create(client, {"name": "Gate"}).json()
    response = client.patch(
        f"/api/cameras/{camera['id']}",
        data={"payload": json.dumps({"source": {"kind": "upload"}})},
        files=upload_files("lobby.mp4"),
    )
    assert response.status_code == 200
    assert response.json()["source"]["filename"] == "lobby.mp4"


def test_delete_removes_the_camera(client, settings) -> None:
    camera = create(client, {"name": "Temp"}).json()
    client.post(f"/api/cameras/{camera['id']}/start")
    assert client.delete(f"/api/cameras/{camera['id']}").status_code == 204
    assert client.get("/api/cameras").json() == []
    assert list(settings.upload_video_dir.iterdir()) == []


# --- lifecycle endpoints ------------------------------------------------


def test_start_stop_restart_endpoints(client) -> None:
    camera = create(client, {"name": "Yard"}).json()

    started = client.post(f"/api/cameras/{camera['id']}/start")
    assert started.status_code == 200
    assert started.json()["status"] == "RUNNING"
    first_pid = started.json()["runtime"]["pid"]

    stopped = client.post(f"/api/cameras/{camera['id']}/stop").json()
    assert stopped["status"] == "STOPPED"
    assert stopped["runtime"]["pid"] is None

    restarted = client.post(f"/api/cameras/{camera['id']}/restart").json()
    assert restarted["status"] == "RUNNING"
    assert restarted["runtime"]["pid"] != first_pid
    client.post(f"/api/cameras/{camera['id']}/stop")


def test_start_failure_surfaces_as_error_status(client, settings, stub_bin) -> None:
    camera = create(client, {"name": "Doomed"}).json()
    settings.ffmpeg_binary = str(stub_bin["failing"])
    body = client.post(f"/api/cameras/{camera['id']}/start").json()
    assert body["status"] == "ERROR"
    assert body["last_error"]


def test_unexpected_exit_moves_camera_to_error(client, settings, stub_bin) -> None:
    settings.ffmpeg_binary = str(stub_bin["short_failure"])
    camera = create(client, {"name": "Flaky"}).json()
    client.post(f"/api/cameras/{camera['id']}/start")
    assert wait_for_status(client, camera["id"], "ERROR") == "ERROR"
    assert client.get(f"/api/cameras/{camera['id']}").json()["runtime"]["pid"] is None


def test_non_looping_end_of_stream_moves_camera_to_stopped(client, settings, stub_bin) -> None:
    settings.ffmpeg_binary = str(stub_bin["short_success"])
    camera = create(client, {"name": "Clip", "loop": False}).json()
    client.post(f"/api/cameras/{camera['id']}/start")
    assert wait_for_status(client, camera["id"], "STOPPED") == "STOPPED"
    assert client.get(f"/api/cameras/{camera['id']}").json()["last_error"] is None


def test_multiple_cameras_are_independent(client) -> None:
    first = create(client, {"name": "Cam One"}).json()
    second = create(client, {"name": "Cam Two"}).json()
    client.post(f"/api/cameras/{first['id']}/start")
    client.post(f"/api/cameras/{second['id']}/start")

    client.post(f"/api/cameras/{first['id']}/stop")
    assert client.get(f"/api/cameras/{first['id']}").json()["status"] == "STOPPED"
    assert client.get(f"/api/cameras/{second['id']}").json()["status"] == "RUNNING"
    client.post(f"/api/cameras/{second['id']}/stop")


def test_openapi_documents_the_api(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/cameras" in schema["paths"]
    assert "/api/cameras/{camera_id}/start" in schema["paths"]
