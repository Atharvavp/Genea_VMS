"""ffprobe command, JSON parsing and (when available) a real probe."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.probe import ProbeError, ProbeService, parse_probe_payload
from tests.conftest import make_real_video, requires_ffmpeg

PAYLOAD = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
            "r_frame_rate": "30/1",
            "bit_rate": "2400000",
        },
        {"codec_type": "audio", "codec_name": "aac"},
    ],
    "format": {
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration": "222.4",
        "bit_rate": "2500000",
        "size": "69500000",
    },
}


def test_build_command_uses_json_output_and_no_shell() -> None:
    command = ProbeService("ffprobe").build_command(Path("/data/videos/a.mp4"))
    assert command == [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "/data/videos/a.mp4",
    ]


def test_parse_full_payload() -> None:
    metadata = parse_probe_payload(PAYLOAD, "parking.mp4", size_bytes=1234)
    assert metadata.filename == "parking.mp4"
    assert metadata.format == "mov,mp4,m4a,3gp,3g2,mj2"
    assert metadata.duration_seconds == pytest.approx(222.4)
    assert metadata.video_codec == "h264"
    assert (metadata.width, metadata.height) == (1920, 1080)
    assert metadata.fps == pytest.approx(29.97, abs=0.01)
    assert metadata.bitrate_kbps == 2500
    assert metadata.has_audio is True
    assert metadata.size_bytes == 1234


def test_missing_optional_fields_become_none() -> None:
    metadata = parse_probe_payload(
        {"streams": [{"codec_type": "video"}], "format": {}}, "clip.mkv"
    )
    assert metadata.video_codec is None
    assert metadata.width is None
    assert metadata.fps is None
    assert metadata.duration_seconds is None
    assert metadata.has_audio is False


def test_zero_denominator_frame_rate_is_ignored() -> None:
    metadata = parse_probe_payload(
        {
            "streams": [{"codec_type": "video", "avg_frame_rate": "0/0", "r_frame_rate": "0/0"}],
            "format": {},
        },
        "clip.mp4",
    )
    assert metadata.fps is None


def test_audio_only_file_is_rejected() -> None:
    with pytest.raises(ProbeError):
        parse_probe_payload({"streams": [{"codec_type": "audio"}], "format": {}}, "a.m4a")


def test_cover_art_does_not_count_as_video() -> None:
    payload = {
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            {"codec_type": "audio"},
        ],
        "format": {},
    }
    with pytest.raises(ProbeError):
        parse_probe_payload(payload, "song.m4a")


def test_empty_payload_is_rejected() -> None:
    with pytest.raises(ProbeError):
        parse_probe_payload({}, "empty.mp4")


@requires_ffmpeg
async def test_probe_real_video(tmp_path: Path) -> None:
    video = make_real_video(tmp_path / "clip.mp4", seconds=1.0, size="320x240")
    metadata = await ProbeService().probe(video, "clip.mp4")
    assert metadata.video_codec == "h264"
    assert (metadata.width, metadata.height) == (320, 240)
    assert metadata.duration_seconds == pytest.approx(1.0, abs=0.3)
    assert metadata.has_audio is False
    assert metadata.size_bytes > 0


@requires_ffmpeg
async def test_probe_rejects_non_video_file(tmp_path: Path) -> None:
    junk = tmp_path / "staging_abc123.mp4"
    junk.write_text("this is definitely not a video")
    with pytest.raises(ProbeError) as excinfo:
        await ProbeService().probe(junk, "notes.mp4")
    # The internal staging path must not leak into the client-facing message.
    assert str(junk) not in str(excinfo.value)
    assert "notes.mp4" in str(excinfo.value)


async def test_probe_reports_missing_binary(tmp_path: Path) -> None:
    service = ProbeService("ffprobe-that-does-not-exist")
    with pytest.raises(ProbeError, match="not found"):
        await service.probe(tmp_path, "x.mp4")
