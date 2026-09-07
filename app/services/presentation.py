"""Record -> HTTP response mapping.

The one place raw persisted values become public ones. A raw RTSP URL is masked
here and never leaves the process in any other shape.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

from app.analytics.types import LatestFrame, WorkerRuntime
from app.domain.models import (
    CameraResponse,
    EventSummaryResponse,
    RecordingItemResponse,
    RecordingLookupResponse,
    RuntimeErrorView,
    RuntimeView,
    to_iso_ms,
)
from app.security.rtsp_url import has_credentials, mask_rtsp_url
from app.services.camera_manager import CameraView
from app.services.vms_recordings import RecordingLookup

__all__ = [
    "camera_response",
    "runtime_view",
    "event_summary",
    "recording_response",
    "encode_snapshot",
]


def runtime_view(runtime: WorkerRuntime) -> RuntimeView:
    error = (
        RuntimeErrorView(
            code=runtime.last_error_code, message=runtime.last_error_message or ""
        )
        if runtime.last_error_code
        else None
    )
    return RuntimeView(
        state=runtime.state,
        stopping=runtime.stopping,
        worker_session_id=runtime.worker_session_id,
        last_frame_at=to_iso_ms(runtime.last_frame_at) if runtime.last_frame_at else None,
        last_inference_at=(
            to_iso_ms(runtime.last_inference_at) if runtime.last_inference_at else None
        ),
        last_event_at=to_iso_ms(runtime.last_event_at) if runtime.last_event_at else None,
        reconnect_attempt=runtime.reconnect_attempt,
        applied_config_revision=runtime.applied_config_revision,
        last_error=error,
    )


def camera_response(view: CameraView) -> CameraResponse:
    camera = view.camera
    return CameraResponse(
        id=camera.id,
        vms_camera_id=camera.vms_camera_id,
        name=camera.name,
        rtsp_url_masked=mask_rtsp_url(camera.rtsp_url),
        rtsp_has_credentials=has_credentials(camera.rtsp_url),
        enabled=camera.enabled,
        inference_fps=camera.inference_fps,
        confidence_threshold=camera.confidence_threshold,
        enabled_classes=camera.sorted_classes(),
        line_configured=view.line is not None,
        runtime=runtime_view(view.runtime),
        created_at=to_iso_ms(camera.created_at),
        updated_at=to_iso_ms(camera.updated_at),
    )


def event_summary(record) -> EventSummaryResponse:
    return EventSummaryResponse.from_record(record)


def recording_response(event_id: str, lookup: RecordingLookup) -> RecordingLookupResponse:
    recording = (
        RecordingItemResponse(
            id=lookup.recording.id,
            start_time=to_iso_ms(lookup.recording.start_time),
            end_time=to_iso_ms(lookup.recording.end_time),
            duration_seconds=lookup.recording.duration_seconds,
            playback_url=lookup.recording.playback_url,
            source=lookup.recording.source,
        )
        if lookup.recording is not None
        else None
    )
    return RecordingLookupResponse(
        status=lookup.status,
        event_id=event_id,
        recording=recording,
        reason=lookup.reason,
    )


def _encode(frame: LatestFrame, quality: int) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.fromarray(frame.rgb).save(
        buffer, format="JPEG", quality=quality, optimize=False, exif=b""
    )
    return buffer.getvalue()


async def encode_snapshot(
    frame: LatestFrame, *, quality: int = 90
) -> tuple[bytes, dict[str, Any]]:
    """Encode off the event loop; the array was already copied under the lock."""
    payload = await asyncio.to_thread(_encode, frame, quality)
    headers = {
        "Cache-Control": "no-store",
        "X-Frame-Sequence": str(frame.sequence),
        "X-Frame-Received-At": to_iso_ms(frame.received_at_utc),
        "X-Frame-Age-Ms": str(int(frame.age_seconds * 1000)),
        "X-Content-Type-Options": "nosniff",
    }
    return payload, headers
