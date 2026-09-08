"""Validation rules for names, stream paths and video configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.models import (
    CameraCreate,
    CameraUpdate,
    SourceSpec,
    VideoConfig,
    slugify_stream_path,
    validate_stream_path,
)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Parking Entrance", "parking-entrance"),
        ("  Lobby  Cam 2 ", "lobby-cam-2"),
        ("Café Nord", "cafe-nord"),
        ("front/back", "front-back"),
        ("A" * 80, "a" * 64),
    ],
)
def test_slugify_stream_path(name: str, expected: str) -> None:
    assert slugify_stream_path(name) == expected


def test_slugify_falls_back_for_unusable_names() -> None:
    slug = slugify_stream_path("!!!")
    assert slug.startswith("camera-")
    assert validate_stream_path(slug) == slug


@pytest.mark.parametrize("value", ["parking-entrance", "cam_1", "a", "0abc"])
def test_valid_stream_paths(value: str) -> None:
    assert validate_stream_path(value) == value


@pytest.mark.parametrize(
    "value", ["-leading", "has space", "UPPER-ONLY!", "with/slash", "", "a" * 65]
)
def test_invalid_stream_paths(value: str) -> None:
    with pytest.raises(ValueError):
        validate_stream_path(value)


def test_stream_path_is_lowercased() -> None:
    assert CameraCreate(name="x", stream_path="Front-Door").stream_path == "front-door"


def test_name_is_trimmed_and_required() -> None:
    assert CameraCreate(name="  Lobby  ").name == "Lobby"
    with pytest.raises(ValidationError):
        CameraCreate(name="   ")


def test_defaults_are_h264_source_auto() -> None:
    camera = CameraCreate(name="Lobby")
    assert camera.video.codec.value == "h264"
    assert camera.video.resolution.mode == "source"
    assert camera.video.fps.mode == "source"
    assert camera.video.bitrate.mode == "auto"
    assert camera.loop is True
    assert camera.auto_start is False


@pytest.mark.parametrize(
    "video",
    [
        {"resolution": {"mode": "fixed", "width": 1281, "height": 720}},  # odd width
        {"resolution": {"mode": "fixed", "width": 0, "height": 720}},
        {"resolution": {"mode": "fixed", "width": 8000, "height": 720}},
        {"fps": {"mode": "fixed", "value": 0}},
        {"fps": {"mode": "fixed", "value": 121}},
        {"bitrate": {"mode": "fixed", "kbps": 10}},
        {"bitrate": {"mode": "fixed", "kbps": 60000}},
        {"codec": "av1"},
        {"resolution": {"mode": "nonsense"}},
    ],
)
def test_invalid_video_configs(video: dict) -> None:
    with pytest.raises(ValidationError):
        VideoConfig.model_validate({**VideoConfig().model_dump(mode="json"), **video})


def test_valid_fixed_video_config() -> None:
    config = VideoConfig.model_validate(
        {
            "codec": "h265",
            "resolution": {"mode": "fixed", "width": 1280, "height": 720},
            "fps": {"mode": "fixed", "value": 15},
            "bitrate": {"mode": "fixed", "kbps": 2500},
        }
    )
    assert config.resolution.width == 1280
    assert config.fps.value == 15
    assert config.bitrate.kbps == 2500


def test_local_source_requires_path() -> None:
    with pytest.raises(ValidationError):
        SourceSpec(kind="local")
    assert SourceSpec(kind="local", local_path="a.mp4").local_path == "a.mp4"


def test_upload_source_rejects_local_path() -> None:
    with pytest.raises(ValidationError):
        SourceSpec(kind="upload", local_path="a.mp4")


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CameraCreate(name="x", unexpected=True)


def test_update_allows_partial_video() -> None:
    update = CameraUpdate.model_validate({"video": {"fps": {"mode": "fixed", "value": 10}}})
    assert update.video.fps.value == 10
    assert update.video.codec is None
    assert update.name is None
