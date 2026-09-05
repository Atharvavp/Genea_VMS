"""Request-model validation and settings-derived URLs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.domain.models import (
    CAMERA_ID_PATTERN,
    CameraCreate,
    CameraHealth,
    CameraHealthState,
    CameraUpdate,
    new_camera_id,
    utcnow_iso,
)


# --- CameraCreate -----------------------------------------------------------


def test_create_defaults_to_enabled():
    camera = CameraCreate(name="Lobby", rtsp_url="rtsp://cam:8554/live")
    assert camera.enabled is True


def test_create_trims_name_and_url():
    camera = CameraCreate(name="  Lobby  ", rtsp_url="  rtsp://cam:8554/live  ")
    assert camera.name == "Lobby"
    assert camera.rtsp_url == "rtsp://cam:8554/live"


def test_create_accepts_credentialed_url_verbatim():
    url = "rtsp://admin:hunter2@10.0.0.9:554/Streaming"
    assert CameraCreate(name="Gate", rtsp_url=url).rtsp_url == url


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "", "rtsp_url": "rtsp://cam/live"},
        {"name": "   ", "rtsp_url": "rtsp://cam/live"},
        {"name": "x" * 101, "rtsp_url": "rtsp://cam/live"},
        {"name": "ok", "rtsp_url": "http://cam/live"},
        {"name": "ok", "rtsp_url": ""},
        {"name": "ok", "rtsp_url": "rtsp://"},
        {"name": "ok"},
        {"rtsp_url": "rtsp://cam/live"},
        {"name": "ok", "rtsp_url": "rtsp://cam/live", "mediamtx_path": "custom"},
        {"name": "ok", "rtsp_url": "rtsp://cam/live", "id": "cam_00000001"},
    ],
)
def test_create_rejects_bad_payloads(payload):
    with pytest.raises(ValidationError):
        CameraCreate(**payload)


def test_create_rejects_user_supplied_mediamtx_path():
    """Users do not choose MediaMTX paths; the VMS owns them."""
    with pytest.raises(ValidationError):
        CameraCreate(name="ok", rtsp_url="rtsp://cam/live", mediamtx_path="anything")


# --- CameraUpdate -----------------------------------------------------------


def test_update_tracks_provided_fields_only():
    update = CameraUpdate(name="Renamed")
    assert update.provided_fields == {"name"}
    assert update.rtsp_url is None
    assert update.enabled is None


def test_update_can_be_empty():
    assert CameraUpdate().provided_fields == set()


def test_update_enabled_false_is_recorded_as_provided():
    update = CameraUpdate(enabled=False)
    assert update.provided_fields == {"enabled"}
    assert update.enabled is False


def test_update_rejects_blank_url_rather_than_ignoring_it():
    # The edit form omits rtsp_url to keep the current source; sending "" is a bug.
    with pytest.raises(ValidationError):
        CameraUpdate(rtsp_url="")


def test_update_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        CameraUpdate(health="ONLINE")


# --- ids, timestamps, health ------------------------------------------------


def test_new_camera_id_shape_and_uniqueness():
    ids = {new_camera_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(CAMERA_ID_PATTERN.match(value) for value in ids)


def test_utcnow_iso_is_zulu():
    assert utcnow_iso().endswith("Z")


def test_health_defaults_to_unknown():
    health = CameraHealth()
    assert health.state is CameraHealthState.UNKNOWN
    assert health.mediamtx_available is False
    assert health.last_error is None


# --- Settings ---------------------------------------------------------------


def test_settings_derive_public_webrtc_urls():
    settings = Settings(public_webrtc_host="localhost", public_webrtc_port=8889)
    assert settings.public_webrtc_base_url == "http://localhost:8889"
    assert settings.whep_url("vms_cam_ab12cd34") == (
        "http://localhost:8889/vms_cam_ab12cd34/whep"
    )


def test_settings_map_camera_id_to_managed_path():
    settings = Settings()
    assert settings.mediamtx_path_for("cam_ab12cd34") == "vms_cam_ab12cd34"
    assert settings.is_managed_path("vms_cam_ab12cd34") is True
    assert settings.is_managed_path("simulator/lobby") is False


def test_settings_read_environment(monkeypatch):
    monkeypatch.setenv("PUBLIC_WEBRTC_HOST", "10.1.2.3")
    monkeypatch.setenv("CAMERA_HEALTH_POLL_SECONDS", "5")
    settings = Settings()
    assert settings.public_webrtc_host == "10.1.2.3"
    assert settings.camera_health_poll_seconds == 5.0
