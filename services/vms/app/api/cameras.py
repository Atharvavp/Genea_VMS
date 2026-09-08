"""Camera REST routes.

The smallest surface that covers the PRD. There is no playback/session
endpoint: MediaMTX's own WHEP endpoint is the signalling channel, and each
camera view carries its `webrtc_url`, so the browser needs nothing else.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.domain.models import CameraCreate, CameraHealth, CameraUpdate, CameraView
from app.services.camera_manager import CameraManager

router = APIRouter()


def _manager(request: Request) -> CameraManager:
    return request.app.state.camera_manager


@router.get("/cameras", response_model=list[CameraView])
async def list_cameras(request: Request) -> list[CameraView]:
    return await _manager(request).list_cameras()


@router.post("/cameras", response_model=CameraView, status_code=status.HTTP_201_CREATED)
async def create_camera(request: Request, payload: CameraCreate) -> CameraView:
    """Register a camera.

    A syntactically valid RTSP URL is accepted even when the camera is
    unreachable; it is then reported OFFLINE rather than rejected.
    """
    return await _manager(request).create_camera(payload)


@router.get("/cameras/{camera_id}", response_model=CameraView)
async def get_camera(request: Request, camera_id: str) -> CameraView:
    return await _manager(request).get_camera(camera_id)


@router.patch("/cameras/{camera_id}", response_model=CameraView)
async def update_camera(
    request: Request, camera_id: str, payload: CameraUpdate
) -> CameraView:
    """Partial update.

    Omit `rtsp_url` to keep the stored source - which is what the edit form does
    for a camera whose URL carries credentials, since the raw URL is never sent
    back to the browser.
    """
    return await _manager(request).update_camera(camera_id, payload)


@router.delete("/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(request: Request, camera_id: str) -> Response:
    await _manager(request).delete_camera(camera_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/cameras/{camera_id}/enable", response_model=CameraView)
async def enable_camera(request: Request, camera_id: str) -> CameraView:
    return await _manager(request).set_enabled(camera_id, True)


@router.post("/cameras/{camera_id}/disable", response_model=CameraView)
async def disable_camera(request: Request, camera_id: str) -> CameraView:
    return await _manager(request).set_enabled(camera_id, False)


@router.get("/cameras/{camera_id}/status", response_model=CameraHealth)
async def camera_status(request: Request, camera_id: str) -> CameraHealth:
    """Source availability only. The browser's player state is not tracked here."""
    return await _manager(request).get_health(camera_id)
