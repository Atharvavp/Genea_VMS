"""HTTP routes for camera CRUD and lifecycle operations."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.api.errors import BadRequest, ValidationFailed
from app.domain.models import CameraCreate, CameraUpdate, CameraView
from app.services.camera_manager import CameraManager

logger = logging.getLogger(__name__)

router = APIRouter()


def _manager(request: Request) -> CameraManager:
    return request.app.state.camera_manager


async def _parse_payload(request: Request) -> tuple[dict[str, Any], Any | None]:
    """Accept either multipart/form-data (payload + optional file) or plain JSON.

    Returns the decoded payload dict and the uploaded file, if any. For
    multipart requests the caller must keep the request form open while the
    upload is being consumed, which is why the form is not closed here.
    """
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        request.state.form = form  # closed by the route's finally block
        raw = form.get("payload")
        upload = form.get("file")
        if upload is not None and not hasattr(upload, "read"):
            raise BadRequest("The 'file' part must be an uploaded file.")
        if upload is not None and not getattr(upload, "filename", None):
            upload = None
        if raw is None:
            payload: dict[str, Any] = {}
        elif isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise BadRequest("The 'payload' part must contain valid JSON.") from exc
        else:
            raise BadRequest("The 'payload' part must be a JSON string, not a file.")
        if not isinstance(payload, dict):
            raise BadRequest("The 'payload' part must be a JSON object.")
        return payload, upload

    if content_type.startswith("application/json"):
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise BadRequest("Request body must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise BadRequest("Request body must be a JSON object.")
        return payload, None

    raise BadRequest(
        "Unsupported content type. Use multipart/form-data (payload + file) "
        "or application/json."
    )


async def _close_form(request: Request) -> None:
    form = getattr(request.state, "form", None)
    if form is not None:
        await form.close()
        request.state.form = None


def _validate(model: type, payload: dict[str, Any]):
    from pydantic import ValidationError

    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        fields = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())) or "body",
                "message": error.get("msg", "invalid"),
            }
            for error in exc.errors()
        ]
        raise ValidationFailed("Camera configuration is invalid.", {"fields": fields}) from exc


@router.get("/cameras", response_model=list[CameraView])
async def list_cameras(request: Request) -> list[CameraView]:
    return await _manager(request).list_cameras()


@router.get("/cameras/{camera_id}", response_model=CameraView)
async def get_camera(camera_id: str, request: Request) -> CameraView:
    return await _manager(request).get_camera(camera_id)


@router.post("/cameras", response_model=CameraView, status_code=201)
async def create_camera(request: Request) -> CameraView:
    try:
        payload, upload = await _parse_payload(request)
        model = _validate(CameraCreate, payload)
        return await _manager(request).create_camera(model, upload)
    finally:
        await _close_form(request)


@router.patch("/cameras/{camera_id}", response_model=CameraView)
async def update_camera(camera_id: str, request: Request) -> CameraView:
    try:
        payload, upload = await _parse_payload(request)
        model = _validate(CameraUpdate, payload)
        return await _manager(request).update_camera(camera_id, model, upload)
    finally:
        await _close_form(request)


@router.delete("/cameras/{camera_id}", status_code=204)
async def delete_camera(camera_id: str, request: Request) -> Response:
    await _manager(request).delete_camera(camera_id)
    return Response(status_code=204)


@router.post("/cameras/{camera_id}/start", response_model=CameraView)
async def start_camera(camera_id: str, request: Request) -> CameraView:
    return await _manager(request).start_camera(camera_id)


@router.post("/cameras/{camera_id}/stop", response_model=CameraView)
async def stop_camera(camera_id: str, request: Request) -> CameraView:
    return await _manager(request).stop_camera(camera_id)


@router.post("/cameras/{camera_id}/restart", response_model=CameraView)
async def restart_camera(camera_id: str, request: Request) -> CameraView:
    return await _manager(request).restart_camera(camera_id)


@router.get("/local-sources")
async def list_local_sources(request: Request) -> JSONResponse:
    """Files available in the Docker-mounted source directory."""
    storage = request.app.state.source_storage
    settings = request.app.state.settings
    return JSONResponse(
        {
            "directory": str(settings.source_video_dir),
            "files": storage.list_local_sources(),
        }
    )
