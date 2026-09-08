"""Camera CRUD and snapshot routes. All behaviour is delegated."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.errors import (
    CameraNotFoundError,
    DuplicateVmsCameraIdError,
    SnapshotNotReadyError,
    WorkerStopTimeoutError,
)
from app.domain.models import (
    CameraCreateRequest,
    CameraPatchRequest,
    CameraResponse,
)
from app.services.camera_manager import (
    AnalyticsCameraManager,
    CameraNotFound,
    CameraView,
    DuplicateVmsCameraId,
    SnapshotNotReady,
    WorkerStopTimeout,
)
from app.services.presentation import camera_response, encode_snapshot

__all__ = ["router", "get_manager"]

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


def get_manager(request: Request) -> AnalyticsCameraManager:
    return request.app.state.manager


@router.get("", response_model=list[CameraResponse])
async def list_cameras(
    manager: AnalyticsCameraManager = Depends(get_manager),
) -> list[CameraResponse]:
    views = await manager.list_cameras()
    return [camera_response(view) for view in views]


@router.post("", response_model=CameraResponse, status_code=status.HTTP_201_CREATED)
async def create_camera(
    payload: CameraCreateRequest,
    response: Response,
    manager: AnalyticsCameraManager = Depends(get_manager),
) -> CameraResponse:
    try:
        view = await manager.create_camera(payload)
    except DuplicateVmsCameraId as exc:
        raise DuplicateVmsCameraIdError() from exc
    response.headers["Location"] = f"/api/cameras/{view.camera.id}"
    return camera_response(view)


@router.get("/{camera_id}", response_model=CameraResponse)
async def get_camera(
    camera_id: str, manager: AnalyticsCameraManager = Depends(get_manager)
) -> CameraResponse:
    return camera_response(await _view(manager, camera_id))


@router.patch("/{camera_id}", response_model=CameraResponse)
async def patch_camera(
    camera_id: str,
    payload: CameraPatchRequest,
    manager: AnalyticsCameraManager = Depends(get_manager),
) -> CameraResponse:
    try:
        view = await manager.patch_camera(camera_id, payload)
    except CameraNotFound as exc:
        raise CameraNotFoundError() from exc
    except DuplicateVmsCameraId as exc:
        raise DuplicateVmsCameraIdError() from exc
    except WorkerStopTimeout as exc:
        raise WorkerStopTimeoutError() from exc
    return camera_response(view)


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(
    camera_id: str, manager: AnalyticsCameraManager = Depends(get_manager)
) -> Response:
    try:
        await manager.delete_camera(camera_id)
    except CameraNotFound as exc:
        raise CameraNotFoundError() from exc
    except WorkerStopTimeout as exc:
        raise WorkerStopTimeoutError() from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{camera_id}/snapshot")
async def get_snapshot(
    camera_id: str, manager: AnalyticsCameraManager = Depends(get_manager)
) -> Response:
    try:
        frame = await manager.get_snapshot(camera_id)
    except CameraNotFound as exc:
        raise CameraNotFoundError() from exc
    except SnapshotNotReady as exc:
        raise SnapshotNotReadyError() from exc
    payload, headers = await encode_snapshot(frame, quality=manager_quality(manager))
    return Response(content=payload, media_type="image/jpeg", headers=headers)


def manager_quality(manager: AnalyticsCameraManager) -> int:
    return getattr(manager, "jpeg_quality", 90)


async def _view(manager: AnalyticsCameraManager, camera_id: str) -> CameraView:
    try:
        return await manager.get_camera(camera_id)
    except CameraNotFound as exc:
        raise CameraNotFoundError() from exc
