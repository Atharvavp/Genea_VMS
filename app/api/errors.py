"""API error type and exception handlers producing a stable error envelope.

```json
{"error": {"code": "camera_not_found", "message": "...", "details": {}}}
```

Two deliberate absences compared with a fuller taxonomy:

* there is no `mediamtx_unavailable` failure for mutations. A MediaMTX outage
  does not reject a camera change - the desired state is stored and reconciled
  when MediaMTX returns - so the outage is reported by `/health` and by each
  camera's `UNKNOWN` health, not by a 503 on the write;
* a source MediaMTX refuses is likewise not a request failure. The camera is
  registered and reported `OFFLINE`, which is the PRD's semantics for "the VMS
  cannot currently obtain this source".

The recording errors below are historical-playback failures only. A
`recording_unavailable` response says the playback server could not answer; it
does not mean the camera has stopped recording, and it never changes the
camera's recording state.

Every message and detail here is safe to show: nothing that reaches this module
carries an RTSP password.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.security.rtsp_url import sanitize_text
from app.services.camera_manager import CameraNotFound
from app.services.recording_manager import (
    CameraNotFound as RecordingCameraNotFound,
    DisabledCameraHistory,
    InvalidDate,
)
from app.services.mediamtx_client import RecordingUnavailable

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """An error that maps directly onto an HTTP response."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content={
                "error": {
                    "code": self.code,
                    "message": self.message,
                    "details": self.details,
                }
            },
        )


class CameraNotFoundError(ApiError):
    def __init__(self, camera_id: str) -> None:
        super().__init__(
            404,
            "camera_not_found",
            f"No camera with id '{sanitize_text(camera_id)[:64]}'.",
        )


class RecordingUnavailableError(ApiError):
    """History could not be served. Not a statement about live recording."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            503,
            "recording_unavailable",
            message
            or (
                "Recorded history is not available right now. This does not "
                "affect live viewing or ongoing recording."
            ),
        )


class DisabledCameraHistoryUnavailableError(ApiError):
    def __init__(self, camera_id: str) -> None:
        super().__init__(
            409,
            "disabled_camera_history_unavailable",
            "Recorded history is only available while a camera is enabled. "
            "The recordings are still on disk; enable the camera to browse them.",
            {"camera_id": sanitize_text(camera_id)[:64]},
        )


class ValidationFailed(ApiError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(422, "validation_error", message, details)


def _pydantic_details(exc: ValidationError | RequestValidationError) -> dict[str, Any]:
    fields = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        fields.append(
            {
                "field": location or "body",
                # A validation message quotes the offending value, which for
                # rtsp_url is a URL that may carry a password.
                "message": sanitize_text(str(error.get("msg", "invalid"))),
            }
        )
    return {"fields": fields}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return exc.to_response()

    @app.exception_handler(CameraNotFound)
    async def _handle_camera_not_found(_: Request, exc: CameraNotFound) -> JSONResponse:
        return CameraNotFoundError(str(exc.args[0] if exc.args else "?")).to_response()

    @app.exception_handler(RecordingCameraNotFound)
    async def _handle_recording_camera_not_found(
        _: Request, exc: RecordingCameraNotFound
    ) -> JSONResponse:
        return CameraNotFoundError(str(exc.args[0] if exc.args else "?")).to_response()

    @app.exception_handler(DisabledCameraHistory)
    async def _handle_disabled_history(
        _: Request, exc: DisabledCameraHistory
    ) -> JSONResponse:
        return DisabledCameraHistoryUnavailableError(
            str(exc.args[0] if exc.args else "?")
        ).to_response()

    @app.exception_handler(InvalidDate)
    async def _handle_invalid_date(_: Request, exc: InvalidDate) -> JSONResponse:
        return ValidationFailed(
            "Request payload failed validation.",
            {"fields": [{"field": "date", "message": sanitize_text(str(exc))}]},
        ).to_response()

    @app.exception_handler(RecordingUnavailable)
    async def _handle_recording_unavailable(
        _: Request, exc: RecordingUnavailable
    ) -> JSONResponse:
        # Already sanitised by the playback client; sanitised again on the way
        # out because this is free text from another process.
        logger.warning("recording_unavailable error=%s", sanitize_text(str(exc)))
        return RecordingUnavailableError().to_response()

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return ValidationFailed(
            "Request payload failed validation.", _pydantic_details(exc)
        ).to_response()

    @app.exception_handler(ValidationError)
    async def _handle_model_validation(_: Request, exc: ValidationError) -> JSONResponse:
        return ValidationFailed(
            "Request payload failed validation.", _pydantic_details(exc)
        ).to_response()

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Logged with a traceback, but never echoed: an unexpected exception
        # from the MediaMTX layer could quote a source URL.
        logger.exception("unhandled_error error=%s", type(exc).__name__)
        return ApiError(
            500, "internal_error", "An unexpected internal error occurred."
        ).to_response()
