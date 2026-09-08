"""Event filter, detail, image, and recording-lookup routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from app.api.errors import (
    EventArtifactGoneError,
    EventNotFoundError,
    InvalidCursorError,
    InvalidTimeRangeError,
)
from app.domain.models import (
    EventDetailResponse,
    EventPageResponse,
    EventSummaryResponse,
    RecordingLookupResponse,
)
from app.services.event_service import (
    ArtifactUnavailable,
    EventNotFound,
    EventService,
    InvalidCursor,
    InvalidTimeRange,
)
from app.services.presentation import recording_response

__all__ = ["router", "get_event_service"]

router = APIRouter(prefix="/api/events", tags=["events"])

_IMAGE_HEADERS = {
    "Cache-Control": "private, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
}


def get_event_service(request: Request) -> EventService:
    return request.app.state.event_service


@router.get("", response_model=EventPageResponse)
async def list_events(
    camera_id: str | None = Query(default=None),
    object_category: str | None = Query(default=None),
    object_class: str | None = Query(default=None),
    direction: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=100),
    cursor: str | None = Query(default=None),
    service: EventService = Depends(get_event_service),
) -> EventPageResponse:
    import asyncio

    try:
        query = service.build_query(
            camera_id=camera_id,
            object_category=object_category,
            object_class=object_class,
            direction=direction,
            start=from_,
            end=to,
            limit=limit,
            cursor=cursor,
        )
    except InvalidTimeRange as exc:
        raise InvalidTimeRangeError(str(exc)) from exc
    except ValueError as exc:
        raise InvalidTimeRangeError(str(exc)) from exc

    try:
        records, next_cursor = await asyncio.to_thread(service.list_events, query)
    except InvalidCursor as exc:
        raise InvalidCursorError() from exc
    return EventPageResponse(
        items=[EventSummaryResponse.from_record(record) for record in records],
        next_cursor=next_cursor,
    )


@router.get("/{event_id}", response_model=EventDetailResponse)
async def get_event(
    event_id: str, service: EventService = Depends(get_event_service)
) -> EventDetailResponse:
    import asyncio

    try:
        record = await asyncio.to_thread(service.get_event, event_id)
    except EventNotFound as exc:
        raise EventNotFoundError() from exc
    return EventDetailResponse.from_record(record)


async def _image(service: EventService, event_id: str, kind: str) -> FileResponse:
    import asyncio

    try:
        _record, path = await asyncio.to_thread(service.resolve_image, event_id, kind)
    except EventNotFound as exc:
        raise EventNotFoundError() from exc
    except ArtifactUnavailable as exc:
        raise EventArtifactGoneError() from exc
    return FileResponse(path, media_type="image/jpeg", headers=dict(_IMAGE_HEADERS))


@router.get("/{event_id}/frame")
async def get_frame(
    event_id: str, service: EventService = Depends(get_event_service)
) -> FileResponse:
    return await _image(service, event_id, "frame")


@router.get("/{event_id}/crop")
async def get_crop(
    event_id: str, service: EventService = Depends(get_event_service)
) -> FileResponse:
    return await _image(service, event_id, "crop")


@router.get("/{event_id}/recording", response_model=RecordingLookupResponse)
async def get_recording(
    event_id: str, service: EventService = Depends(get_event_service)
) -> RecordingLookupResponse:
    """Always 200 when the event exists: a VMS failure is data, not an error."""
    try:
        record, lookup = await service.lookup_recording(event_id)
    except EventNotFound as exc:
        raise EventNotFoundError() from exc
    return recording_response(record.id, lookup)
