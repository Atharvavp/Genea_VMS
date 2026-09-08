"""FFmpeg argv construction."""

from __future__ import annotations

import pytest

from app.domain.models import CameraRecord, CameraStatus, SourceMetadata, VideoConfig, utcnow
from app.services.ffmpeg_command import build_publish_command, build_video_filter

TARGET = "rtsp://mediamtx:8554/simulator/parking-entrance"


def make_camera(video: dict | None = None, loop: bool = True, **overrides) -> CameraRecord:
    metadata = SourceMetadata(
        filename="parking.mp4", width=1920, height=1080, fps=25.0, video_codec="h264"
    )
    metadata = metadata.model_copy(update=overrides.pop("metadata", {}))
    now = utcnow()
    return CameraRecord(
        id="cam_ab12cd34",
        name="Parking Entrance",
        stream_path="parking-entrance",
        source_kind="upload",
        source_path="/data/videos/cam_ab12cd34_x.mp4",
        source_original_filename="parking.mp4",
        source_stored_filename="cam_ab12cd34_x.mp4",
        source_metadata=metadata,
        video=VideoConfig.model_validate(video or {}),
        loop=loop,
        auto_start=False,
        status=CameraStatus.STOPPED,
        created_at=now,
        updated_at=now,
        **overrides,
    )


def test_command_is_an_argv_list_without_shell_strings() -> None:
    command = build_publish_command(make_camera(), TARGET)
    assert isinstance(command, list)
    assert all(isinstance(part, str) for part in command)
    assert not any(" " in part and part != "-vf" for part in command[:6])


def test_looping_h264_source_defaults() -> None:
    command = build_publish_command(make_camera(), TARGET, "ffmpeg")
    assert command == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-re",
        "-stream_loop",
        "-1",
        "-i",
        "/data/videos/cam_ab12cd34_x.mp4",
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "23",
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        TARGET,
    ]


def test_stream_loop_precedes_input_and_is_absent_when_not_looping() -> None:
    looping = build_publish_command(make_camera(loop=True), TARGET)
    assert looping.index("-stream_loop") < looping.index("-i")

    once = build_publish_command(make_camera(loop=False), TARGET)
    assert "-stream_loop" not in once


def test_h265_codec_arguments() -> None:
    command = build_publish_command(make_camera({"codec": "h265"}), TARGET)
    assert "libx265" in command
    assert "libx264" not in command
    assert command[command.index("-x265-params") + 1] == "log-level=error"
    assert "-tune" not in command


def test_fixed_bitrate_arguments() -> None:
    command = build_publish_command(
        make_camera({"bitrate": {"mode": "fixed", "kbps": 2500}}), TARGET
    )
    assert "-crf" not in command
    assert command[command.index("-b:v") + 1] == "2500k"
    assert command[command.index("-maxrate") + 1] == "2500k"
    assert command[command.index("-bufsize") + 1] == "5000k"


def test_source_resolution_and_fps_need_no_filter() -> None:
    assert build_video_filter(make_camera()) is None
    assert "-vf" not in build_publish_command(make_camera(), TARGET)


def test_fixed_resolution_filter() -> None:
    camera = make_camera({"resolution": {"mode": "fixed", "width": 1280, "height": 720}})
    assert build_video_filter(camera) == (
        "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )


def test_fixed_fps_filter() -> None:
    assert build_video_filter(make_camera({"fps": {"mode": "fixed", "value": 15}})) == "fps=15"
    assert (
        build_video_filter(make_camera({"fps": {"mode": "fixed", "value": 29.97}}))
        == "fps=29.97"
    )


def test_fixed_resolution_and_fps_combine_in_order() -> None:
    camera = make_camera(
        {
            "resolution": {"mode": "fixed", "width": 640, "height": 480},
            "fps": {"mode": "fixed", "value": 10},
        }
    )
    video_filter = build_video_filter(camera)
    assert video_filter.startswith("scale=640:480")
    assert video_filter.endswith("setsar=1,fps=10")
    command = build_publish_command(camera, TARGET)
    assert command[command.index("-vf") + 1] == video_filter


def test_odd_source_dimensions_are_evened_out() -> None:
    camera = make_camera(metadata={"width": 1281, "height": 721})
    assert build_video_filter(camera) == "scale=trunc(iw/2)*2:trunc(ih/2)*2"


def test_audio_is_always_dropped_and_transport_is_tcp() -> None:
    command = build_publish_command(make_camera(), TARGET)
    assert "-an" in command
    assert command[-5:] == ["-f", "rtsp", "-rtsp_transport", "tcp", TARGET]
