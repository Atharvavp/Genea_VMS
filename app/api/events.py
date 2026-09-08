"""Local event detail, the fixed-base image proxy, and recording delegation.

The proxy is the one place Component 5 fetches bytes on a user's behalf, so it
is deliberately narrow: the event must already exist in the LOCAL store, the id
must match Component 4's exact format, the kind is one of two literals, and the
destination is always ``{configured base}/api/events/{id}/{kind}``. No part of
the request - path, query, header, or upstream response - can redirect it.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response

from app.api.errors import (
    EventArtifactGoneError,
    EventNotFoundError,
    UpstreamInvalidResponseError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from app.domain.models import (
    EventDetailResponse,
    RecordingLookupResponse,
    RecordingPayload,
    format_utc,
    is_event_id,
)
from app.integrations.component4 import (
    UpstreamContractError,
    UpstreamEventNotFound,
    UpstreamGone,
    UpstreamRejected,
    UpstreamTimeout,
    UpstreamUnavailable,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/events", tags=["events"])

_IMAGE_HEADERS = {
    # Component 4's immutable policy is preserved: an event's artifact never
    # changes, so a browser may keep it and survive an upstream outage.
    "Cache-Control": "private, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
}


def _local_event(request: Request, event_id: str):
    if not is_event_id(event_id):
        raise EventNotFoundError()
    record = request.app.state.events.get(event_id)
    if record is None:
        raise EventNotFoundError()
    return record


@router.get("/{event_id}", response_model=EventDetailResponse)
async def get_event(request: Request, event_id: str) -> EventDetailResponse:
    """Local metadata only - this route works during a Component 4 outage."""
    import asyncio

    record = await asyncio.to_thread(_local_event, request, event_id)
    states = await asyncio.to_thread(
        request.app.state.events.representation_states, event_id
    )
    state = request.app.state.events.event_state(states)
    return EventDetailResponse(
        event_id=record.event_id,
        camera_id=record.camera_id,
        vms_camera_id=record.vms_camera_id,
        camera_name=record.camera_name,
        crossed_at=format_utc(record.crossed_at),
        object_category=record.object_category,
        object_class=record.object_class,
        direction=record.direction,
        discovered_at=format_utc(record.discovered_at),
        source_last_seen_at=format_utc(record.source_last_seen_at),
        index_state=state,
        representations=states,
        crop_url=f"/api/events/{event_id}/crop",
        frame_url=f"/api/events/{event_id}/frame",
        recording_url=f"/api/events/{event_id}/recording",
    )


async def _proxy(request: Request, event_id: str, kind: Literal["crop", "frame"]) -> Response:
    import asyncio

    await asyncio.to_thread(_local_event, request, event_id)
    client = request.app.state.component4
    try:
        payload = await client.fetch_artifact(event_id, kind)
    except UpstreamGone as exc:
        raise EventArtifactGoneError() from exc
    except UpstreamEventNotFound as exc:
        raise EventNotFoundError() from exc
    except UpstreamTimeout as exc:
        raise UpstreamTimeoutError() from exc
    except (UpstreamUnavailable, UpstreamRejected) as exc:
        raise UpstreamUnavailableError() from exc
    except UpstreamContractError as exc:
        raise UpstreamInvalidResponseError() from exc
    return Response(payload, media_type="image/jpeg", headers=dict(_IMAGE_HEADERS))


@router.get("/{event_id}/crop")
async def get_crop(request: Request, event_id: str) -> Response:
    return await _proxy(request, event_id, "crop")


@router.get("/{event_id}/frame")
async def get_frame(request: Request, event_id: str) -> Response:
    return await _proxy(request, event_id, "frame")


@router.get("/{event_id}/recording", response_model=RecordingLookupResponse)
async def get_recording(request: Request, event_id: str) -> RecordingLookupResponse:
    """Always 200 for a locally known event: an upstream failure is data.

    Component 5 resolves nothing itself. It asks Component 4, validates the
    shape, and passes the answer through - it never builds, rewrites, proxies,
    or stores a playback URL.
    """
    import asyncio

    await asyncio.to_thread(_local_event, request, event_id)
    client = request.app.state.component4
    try:
        lookup = await client.fetch_recording(event_id)
    except UpstreamEventNotFound:
        return RecordingLookupResponse(
            status="UNAVAILABLE", event_id=event_id, recording=None,
            reason="component4_event_not_found",
        )
    except UpstreamTimeout:
        return RecordingLookupResponse(
            status="UNAVAILABLE", event_id=event_id, recording=None,
            reason="component4_timeout",
        )
    except UpstreamUnavailable:
        return RecordingLookupResponse(
            status="UNAVAILABLE", event_id=event_id, recording=None,
            reason="component4_unreachable",
        )
    except UpstreamRejected:
        return RecordingLookupResponse(
            status="UNAVAILABLE", event_id=event_id, recording=None,
            reason="component4_rejected_request",
        )
    except UpstreamContractError:
        return RecordingLookupResponse(
            status="UNAVAILABLE", event_id=event_id, recording=None,
            reason="component4_invalid_response",
        )

    recording = None
    if lookup.recording is not None:
        recording = RecordingPayload(
            id=lookup.recording.id,
            start_time=lookup.recording.start_time,
            end_time=lookup.recording.end_time,
            duration_seconds=lookup.recording.duration_seconds,
            playback_url=lookup.recording.playback_url,
            source=lookup.recording.source,
        )
    return RecordingLookupResponse(
        status=lookup.status, event_id=event_id, recording=recording,
        reason=lookup.reason,
    )
