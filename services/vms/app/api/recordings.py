"""Recording REST routes.

One endpoint: which finalised recordings exist for a camera on a UTC day, and
where the browser can play each of them.

The client supplies a *camera id*, never a MediaMTX path and never a filesystem
path. The VMS resolves the id to the path that camera owns and builds the
playback URL itself, so there is nothing here a caller can point at another
camera's recordings or at the host filesystem.

No media passes through this process: `playback_url` points straight at
MediaMTX's playback server, the same way `webrtc_url` points at its WHEP
endpoint for live.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.domain.models import RecordingListResponse
from app.services.recording_manager import RecordingManager

router = APIRouter()


def _manager(request: Request) -> RecordingManager:
    return request.app.state.recording_manager


@router.get("/recordings", response_model=RecordingListResponse)
async def list_recordings(
    request: Request,
    camera_id: str = Query(..., description="Camera whose recordings to list."),
    date: str = Query(..., description="UTC calendar day, as YYYY-MM-DD."),
) -> RecordingListResponse:
    """Finalised recordings for one camera on one UTC day.

    An enabled camera with nothing recorded that day is a `200` with an empty
    `items` list, not an error.

    A `503 recording_unavailable` means the playback server could not answer. It
    is a historical-playback failure: live viewing and any ongoing recording are
    unaffected, and the camera's own recording state does not change.
    """
    return await _manager(request).list_recordings(camera_id, date)
