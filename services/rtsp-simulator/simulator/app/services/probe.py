"""ffprobe wrapper: validates a source video and extracts its metadata."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from app.domain.models import SourceMetadata

logger = logging.getLogger(__name__)


class ProbeError(Exception):
    """The source file could not be probed or is not a usable video."""


class ProbeService:
    def __init__(self, ffprobe_binary: str = "ffprobe", timeout_seconds: float = 20.0) -> None:
        self._binary = ffprobe_binary
        self._timeout = timeout_seconds

    def build_command(self, source_path: Path) -> list[str]:
        return [
            self._binary,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(source_path),
        ]

    async def probe(self, source_path: Path, display_filename: str) -> SourceMetadata:
        command = self.build_command(source_path)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ProbeError(
                f"'{self._binary}' was not found. Install FFmpeg in the simulator image."
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ProbeError("ffprobe timed out while inspecting the video.") from exc

        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip().splitlines()
            message = detail[-1] if detail else "unknown ffprobe error"
            # Never leak the internal staging/storage path to the client.
            message = message.replace(str(source_path), display_filename)
            raise ProbeError(f"Not a readable video file: {message}")

        try:
            payload = json.loads(stdout.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise ProbeError("ffprobe returned output that could not be parsed.") from exc

        size_bytes = source_path.stat().st_size if source_path.exists() else None
        return parse_probe_payload(payload, display_filename, size_bytes)


def parse_probe_payload(
    payload: dict[str, Any], filename: str, size_bytes: int | None = None
) -> SourceMetadata:
    """Turn ffprobe JSON into :class:`SourceMetadata`, or raise ``ProbeError``."""
    streams = payload.get("streams") or []
    video_stream = next(
        (s for s in streams if s.get("codec_type") == "video" and not _is_cover_art(s)),
        None,
    )
    if video_stream is None:
        raise ProbeError("The file contains no usable video stream.")

    fmt = payload.get("format") or {}
    return SourceMetadata(
        filename=filename,
        format=fmt.get("format_name"),
        duration_seconds=_as_float(fmt.get("duration") or video_stream.get("duration")),
        video_codec=video_stream.get("codec_name"),
        width=_as_int(video_stream.get("width")),
        height=_as_int(video_stream.get("height")),
        fps=_parse_frame_rate(video_stream.get("avg_frame_rate"))
        or _parse_frame_rate(video_stream.get("r_frame_rate")),
        bitrate_kbps=_as_kbps(fmt.get("bit_rate") or video_stream.get("bit_rate")),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        size_bytes=size_bytes if size_bytes is not None else _as_int(fmt.get("size")),
    )


def _is_cover_art(stream: dict[str, Any]) -> bool:
    """Attached pictures (album art) are video streams but not playable video."""
    return bool((stream.get("disposition") or {}).get("attached_pic"))


def _parse_frame_rate(value: Any) -> float | None:
    if not value or not isinstance(value, str) or "/" not in value:
        return _as_float(value)
    numerator, _, denominator = value.partition("/")
    try:
        num, den = float(numerator), float(denominator)
    except ValueError:
        return None
    if den == 0:
        return None
    return round(num / den, 3)


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # drop NaN


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_kbps(value: Any) -> int | None:
    bits = _as_float(value)
    return round(bits / 1000) if bits else None
