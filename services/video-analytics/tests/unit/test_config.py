"""Settings validation (PLAN sections 22.1 and 23.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import (
    DEFAULT_MODEL_SHA256,
    MIN_CURSOR_KEY_BYTES,
    Settings,
    build_test_settings,
)

pytestmark = pytest.mark.unit

VALID_KEY = "x" * MIN_CURSOR_KEY_BYTES


def make(**overrides) -> Settings:
    values = {"cursor_signing_key": VALID_KEY}
    values.update(overrides)
    return Settings(**values)


# -- defaults ---------------------------------------------------------------


def test_defaults_match_the_documented_environment_table():
    settings = make()
    assert settings.analytics_http_port == 8100
    assert settings.analytics_data_dir == Path("/data")
    assert settings.analytics_db_path == Path("/data/analytics.db")
    assert settings.analytics_event_dir == Path("/data/events")
    assert settings.analytics_model_path == Path("/opt/models/yolo11n.pt")
    assert settings.analytics_model_sha256 == DEFAULT_MODEL_SHA256
    assert settings.analytics_torch_threads == 2
    assert settings.analytics_jpeg_quality == 90
    assert settings.analytics_stall_seconds == 10.0
    assert settings.analytics_stop_timeout_seconds == 10.0
    assert settings.analytics_log_level == "INFO"
    assert settings.vms_api_base_url == "http://host.docker.internal:8090"


def test_settings_are_frozen():
    settings = make()
    with pytest.raises(ValidationError):
        settings.analytics_torch_threads = 4  # type: ignore[misc]


def test_environment_overrides_are_read(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYTICS_TORCH_THREADS", "5")
    monkeypatch.setenv("ANALYTICS_LOG_LEVEL", "debug")
    monkeypatch.setenv("CURSOR_SIGNING_KEY", VALID_KEY)
    settings = Settings()
    assert settings.analytics_torch_threads == 5
    assert settings.analytics_log_level == "DEBUG"


# -- path validation --------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("analytics_data_dir", "data"),
        ("analytics_db_path", "analytics.db"),
        ("analytics_event_dir", "events"),
        ("analytics_model_path", "models/yolo11n.pt"),
    ],
)
def test_relative_paths_are_rejected(field: str, value: str):
    with pytest.raises(ValidationError):
        make(**{field: Path(value)})


@pytest.mark.parametrize(
    "field,value",
    [
        ("analytics_db_path", "/elsewhere/analytics.db"),
        ("analytics_event_dir", "/var/events"),
    ],
)
def test_paths_outside_the_data_root_are_rejected(field: str, value: str):
    with pytest.raises(ValidationError):
        make(**{field: Path(value)})


def test_a_data_path_may_not_be_the_data_root_itself():
    with pytest.raises(ValidationError):
        make(analytics_event_dir=Path("/data"))


def test_dot_dot_segments_are_rejected():
    with pytest.raises(ValidationError):
        make(analytics_event_dir=Path("/data/../etc"))


def test_nested_data_paths_are_accepted():
    settings = make(
        analytics_data_dir=Path("/srv/state"),
        analytics_db_path=Path("/srv/state/db/analytics.db"),
        analytics_event_dir=Path("/srv/state/events"),
    )
    assert settings.event_relative_root() == "events"
    assert settings.health_probe_dir == Path("/srv/state/.health")


# -- numeric boundaries -----------------------------------------------------


@pytest.mark.parametrize(
    "field,inside,outside",
    [
        ("analytics_http_port", (1, 65535), (0, 65536)),
        ("analytics_torch_threads", (1, 8), (0, 9)),
        ("analytics_jpeg_quality", (70, 95), (69, 96)),
        ("analytics_stall_seconds", (5.0, 60.0), (4.999, 60.001)),
        ("analytics_stop_timeout_seconds", (2.0, 30.0), (1.999, 30.001)),
    ],
)
def test_numeric_boundaries(field: str, inside: tuple, outside: tuple):
    for value in inside:
        assert getattr(make(**{field: value}), field) == value
    for value in outside:
        with pytest.raises(ValidationError):
            make(**{field: value})


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_non_finite_durations_are_rejected(value: float):
    with pytest.raises(ValidationError):
        make(analytics_stall_seconds=value)


def test_log_level_must_be_one_of_four():
    for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
        assert make(analytics_log_level=level.lower()).analytics_log_level == level
    with pytest.raises(ValidationError):
        make(analytics_log_level="TRACE")


# -- model digest -----------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["", "0" * 63, "0" * 65, "A" * 64, "g" * 64, DEFAULT_MODEL_SHA256.upper()],
)
def test_model_digest_must_be_lowercase_hex_64(value: str):
    with pytest.raises(ValidationError):
        make(analytics_model_sha256=value)


def test_model_digest_accepts_the_pinned_asset():
    assert make(analytics_model_sha256="a" * 64).analytics_model_sha256 == "a" * 64


# -- cursor signing key -----------------------------------------------------


def test_cursor_key_is_required_outside_explicit_test_mode():
    with pytest.raises(ValidationError):
        Settings()


def test_short_cursor_key_is_rejected():
    with pytest.raises(ValidationError):
        make(cursor_signing_key="x" * (MIN_CURSOR_KEY_BYTES - 1))


def test_explicit_test_settings_supply_a_key():
    settings = build_test_settings()
    assert len(settings.cursor_signing_key.encode()) >= MIN_CURSOR_KEY_BYTES


# -- VMS base URL -----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://vms:8090",
        "https://vms.example.test",
        "http://host.docker.internal:8090",
        "http://10.0.0.5:8090/base",
        "http://[::1]:8090",
    ],
)
def test_valid_vms_base_urls(url: str):
    assert make(vms_api_base_url=url).vms_api_base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "ftp://vms:8090",
        "rtsp://vms:8554",
        "http://",
        "http://user:pass@vms:8090",
        "http://vms:8090?x=1",
        "http://vms:8090#frag",
        "http://vms:0",
        "http://vms:70000",
        "http://vms:8090/\nX-Injected: 1",
    ],
)
def test_invalid_vms_base_urls(url: str):
    with pytest.raises(ValidationError):
        make(vms_api_base_url=url)


def test_vms_base_url_trailing_slash_is_normalised():
    settings = make(vms_api_base_url="http://vms:8090/")
    assert settings.vms_api_base_url == "http://vms:8090"
    assert settings.vms_recordings_url == "http://vms:8090/api/recordings"
