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
    recording_id,
    utcnow_iso,
)
from app.services.recording_manager import InvalidDate, parse_utc_day


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


# --- recording fields -------------------------------------------------------


def test_create_defaults_recording_to_off():
    """Registering a camera must never start writing to disk by surprise."""
    camera = CameraCreate(name="Lobby", rtsp_url="rtsp://cam:8554/live")
    assert camera.recording_enabled is False


def test_create_accepts_recording_on():
    camera = CameraCreate(
        name="Lobby", rtsp_url="rtsp://cam:8554/live", recording_enabled=True
    )
    assert camera.recording_enabled is True


def test_update_records_recording_false_as_provided():
    update = CameraUpdate(recording_enabled=False)
    assert update.provided_fields == {"recording_enabled"}
    assert update.recording_enabled is False


def test_update_without_recording_leaves_it_alone():
    assert "recording_enabled" not in CameraUpdate(name="x").provided_fields


def test_create_rejects_a_caller_supplied_recording_state():
    """`state` is derived; only the preference is settable."""
    with pytest.raises(ValidationError):
        CameraCreate(
            name="ok", rtsp_url="rtsp://cam/live", recording="RECORDING"
        )


# --- recording ids ----------------------------------------------------------


def test_recording_id_is_stable_and_opaque():
    first = recording_id("cam_ab12cd34", "2026-09-05T22:39:16.193314Z", 11.06)
    second = recording_id("cam_ab12cd34", "2026-09-05T22:39:16.193314Z", 11.06)
    assert first == second
    assert first.startswith("rec_")
    # Nothing that could be turned into a filesystem path.
    assert "/" not in first and ".." not in first


def test_recording_ids_differ_per_camera_and_span():
    base = recording_id("cam_ab12cd34", "2026-09-05T22:39:16Z", 11.06)
    assert base != recording_id("cam_ffffffff", "2026-09-05T22:39:16Z", 11.06)
    assert base != recording_id("cam_ab12cd34", "2026-09-05T22:39:17Z", 11.06)
    assert base != recording_id("cam_ab12cd34", "2026-09-05T22:39:16Z", 12.0)


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


# --- recording settings -----------------------------------------------------


def test_settings_derive_the_verified_record_path_template():
    settings = Settings(recording_storage_path="/recordings")
    # Verified on 1.20.1 to produce <root>/<path>/<date>/<time>.mp4.
    assert settings.record_path_template == (
        "/recordings/%path/%Y-%m-%d/%H-%M-%S-%f"
    )


def test_settings_trim_a_trailing_slash_from_the_storage_path():
    assert Settings(recording_storage_path="/media/").record_path_template.startswith(
        "/media/%path/"
    )


def test_settings_reject_a_relative_storage_path():
    with pytest.raises(ValidationError):
        Settings(recording_storage_path="recordings")


def test_settings_render_retention_as_a_mediamtx_duration():
    assert Settings(recording_retention_hours=24).record_delete_after_duration == "24h"
    assert Settings(recording_retention_hours=0.01).record_delete_after_duration == (
        "0.01h"
    )
    assert Settings(recording_retention_hours=1.5).record_delete_after_duration == "1.5h"


@pytest.mark.parametrize("value", ["5m", "30s", "1h", "500ms", "2s"])
def test_settings_accept_mediamtx_segment_durations(value):
    assert Settings(recording_segment_duration=value).recording_segment_duration == value


@pytest.mark.parametrize("value", ["", "5", "5 m", "abc", "-5m", "5minutes", "0s"])
def test_settings_reject_bad_segment_durations(value):
    with pytest.raises(ValidationError):
        Settings(recording_segment_duration=value)


@pytest.mark.parametrize("value", [0, -1, -0.5])
def test_settings_reject_non_positive_retention(value):
    with pytest.raises(ValidationError):
        Settings(recording_retention_hours=value)


def test_settings_derive_public_playback_urls():
    settings = Settings(public_playback_host="localhost", public_playback_port=9996)
    assert settings.public_playback_base_url == "http://localhost:9996"

    url = settings.playback_url(
        "vms_cam_ab12cd34", "2026-09-05T22:39:16.193314Z", 11.06
    )
    assert url.startswith("http://localhost:9996/get?")
    assert "path=vms_cam_ab12cd34" in url
    # RFC3339 colons must be percent-encoded, as MediaMTX documents.
    assert "start=2026-09-05T22%3A39%3A16.193314Z" in url
    assert "duration=11.06" in url
    assert url.endswith("format=mp4")


def test_the_playback_url_is_public_not_compose_internal():
    """The browser is not on the compose network and must never be sent to it."""
    settings = Settings(
        mediamtx_playback_url="http://vms-mediamtx:9996",
        public_playback_host="localhost",
    )
    url = settings.playback_url("vms_cam_1", "2026-09-05T00:00:00Z", 1.0)
    assert "vms-mediamtx" not in url


# --- recording dates --------------------------------------------------------


def test_parse_utc_day_returns_the_day_bounds():
    assert parse_utc_day("2026-09-05") == (
        "2026-09-05T00:00:00Z",
        "2026-09-06T00:00:00Z",
    )


def test_parse_utc_day_crosses_a_month_boundary():
    assert parse_utc_day("2026-09-30")[1] == "2026-10-01T00:00:00Z"


@pytest.mark.parametrize(
    "value",
    ["", "2026-9-5", "05-09-2026", "2026-13-01", "2026-02-30", "yesterday",
     "2026-09-05T00:00:00Z", "../../etc/passwd", "2026-09-05/../04"],
)
def test_parse_utc_day_rejects_anything_else(value):
    with pytest.raises(InvalidDate):
        parse_utc_day(value)


def test_parse_utc_day_tolerates_surrounding_whitespace():
    assert parse_utc_day(" 2026-09-05 ")[0] == "2026-09-05T00:00:00Z"
