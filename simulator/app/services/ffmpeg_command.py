"""Builds the FFmpeg publish command for a camera.

Commands are always produced as ``list[str]`` and executed with
``create_subprocess_exec`` - never through a shell.
"""

from __future__ import annotations

from app.domain.models import CameraRecord, Codec


def build_publish_command(
    camera: CameraRecord, target_url: str, ffmpeg_binary: str = "ffmpeg"
) -> list[str]:
    command: list[str] = [
        ffmpeg_binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-re",
    ]
    if camera.loop:
        # Must precede -i to apply to the input file.
        command += ["-stream_loop", "-1"]

    command += ["-i", camera.source_path, "-map", "0:v:0", "-an"]

    video_filter = build_video_filter(camera)
    if video_filter:
        command += ["-vf", video_filter]

    command += _codec_args(camera)
    command += _bitrate_args(camera)
    command += ["-f", "rtsp", "-rtsp_transport", "tcp", target_url]
    return command


def build_video_filter(camera: CameraRecord) -> str | None:
    """Compose the -vf chain from resolution and FPS settings."""
    filters: list[str] = []
    resolution = camera.video.resolution

    if resolution.mode == "fixed":
        width, height = resolution.width, resolution.height
        filters.append(
            f"scale={width}:{height}"
            ":force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
        filters.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
        filters.append("setsar=1")
    elif _has_odd_dimensions(camera):
        # H.264/H.265 with yuv420p need even dimensions.
        filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    fps = camera.video.fps
    if fps.mode == "fixed":
        filters.append(f"fps={_format_number(fps.value)}")

    return ",".join(filters) if filters else None


def _has_odd_dimensions(camera: CameraRecord) -> bool:
    width = camera.source_metadata.width
    height = camera.source_metadata.height
    return bool((width and width % 2) or (height and height % 2))


def _codec_args(camera: CameraRecord) -> list[str]:
    if camera.video.codec is Codec.H265:
        return [
            "-c:v",
            "libx265",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-x265-params",
            "log-level=error",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
    ]


def _bitrate_args(camera: CameraRecord) -> list[str]:
    bitrate = camera.video.bitrate
    if bitrate.mode == "fixed":
        kbps = bitrate.kbps
        return [
            "-b:v",
            f"{kbps}k",
            "-maxrate",
            f"{kbps}k",
            "-bufsize",
            f"{kbps * 2}k",
        ]
    return ["-crf", "23"]


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
