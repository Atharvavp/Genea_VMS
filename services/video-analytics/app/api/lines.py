"""Singleton line GET / PUT / DELETE for one camera."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.api.cameras import get_manager
from app.api.errors import CameraNotFoundError, LineNotConfiguredError
from app.domain.models import LinePutRequest, LineResponse
from app.services.camera_manager import (
    AnalyticsCameraManager,
    CameraNotFound,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/cameras/{camera_id}/line", tags=["lines"])


@router.get("", response_model=LineResponse)
async def get_line(
    camera_id: str, manager: AnalyticsCameraManager = Depends(get_manager)
) -> LineResponse:
    try:
        record = await manager.get_line(camera_id)
    except CameraNotFound as exc:
        raise CameraNotFoundError() from exc
    if record is None:
        raise LineNotConfiguredError()
    return LineResponse.from_record(record)


@router.put("", response_model=LineResponse)
async def put_line(
    camera_id: str,
    payload: LinePutRequest,
    response: Response,
    manager: AnalyticsCameraManager = Depends(get_manager),
) -> LineResponse:
    try:
        record, created = await manager.put_line(camera_id, payload)
    except CameraNotFound as exc:
        raise CameraNotFoundError() from exc
    if created:
        response.status_code = status.HTTP_201_CREATED
        response.headers["Location"] = f"/api/cameras/{camera_id}/line"
    return LineResponse.from_record(record)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_line(
    camera_id: str, manager: AnalyticsCameraManager = Depends(get_manager)
) -> Response:
    try:
        await manager.delete_line(camera_id)
    except CameraNotFound as exc:
        raise CameraNotFoundError() from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
