"""One error envelope, one exception hierarchy, one sanitisation point.

No handler here ever echoes a rejected value: a validation error reports the
field path and a safe reason only, because a rejected RTSP URL may contain
credentials.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.domain.models import new_request_id

__all__ = [
    "ApiError",
    "CameraNotFoundError",
    "LineNotConfiguredError",
    "EventNotFoundError",
    "DuplicateVmsCameraIdError",
    "SnapshotNotReadyError",
    "EventArtifactGoneError",
    "WorkerRetiredError",
    "InvalidCursorError",
    "InvalidTimeRangeError",
    "WorkerStopTimeoutError",
    "ServiceDegradedError",
    "install_error_handlers",
    "error_body",
    "REQUEST_ID_HEADER",
    "sanitize_validation_errors",
]

logger = logging.getLogger("analytics.api")

REQUEST_ID_HEADER: Final[str] = "X-Request-ID"
_SAFE_REQUEST_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class ApiError(Exception):
    """Base class for every deliberately shaped HTTP failure."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "The request could not be completed."

    def __init__(self, message: str | None = None, **details: Any):
        super().__init__(message or self.message)
        self.detail_message = message or self.message
        self.details = details


class CameraNotFoundError(ApiError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "camera_not_found"
    message = "Analytics camera was not found."


class LineNotConfiguredError(ApiError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "line_not_configured"
    message = "This camera has no line configured."


class EventNotFoundError(ApiError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "event_not_found"
    message = "Event was not found."


class DuplicateVmsCameraIdError(ApiError):
    status_code = status.HTTP_409_CONFLICT
    code = "duplicate_vms_camera_id"
    message = "Another analytics camera already consumes this VMS camera."


class SnapshotNotReadyError(ApiError):
    status_code = status.HTTP_409_CONFLICT
    code = "snapshot_not_ready"
    message = "No frame has been decoded for this camera yet."


class EventArtifactGoneError(ApiError):
    status_code = status.HTTP_410_GONE
    code = "event_artifact_gone"
    message = "The stored image for this event is no longer available."


class WorkerRetiredError(ApiError):
    status_code = status.HTTP_410_GONE
    code = "worker_retired"
    message = "The analytics worker that produced this frame has been retired."


class InvalidCursorError(ApiError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid_cursor"
    message = "The supplied page cursor is not valid for this query."


class InvalidTimeRangeError(ApiError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid_time_range"
    message = "The supplied time range is not valid."


class WorkerStopTimeoutError(ApiError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "worker_stop_timeout"
    message = "The analytics worker did not stop in time; retry the operation."


class ServiceDegradedError(ApiError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_degraded"
    message = "A required dependency is unavailable."


def resolve_request_id(request: Request | None) -> str:
    """Accept a conservative inbound id, otherwise mint one."""
    if request is not None:
        inbound = request.headers.get(REQUEST_ID_HEADER)
        if inbound and _SAFE_REQUEST_ID.fullmatch(inbound):
            return inbound
        existing = getattr(request.state, "request_id", None)
        if isinstance(existing, str) and _SAFE_REQUEST_ID.fullmatch(existing):
            return existing
    return new_request_id()


def error_body(
    code: str, message: str, request_id: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }


def sanitize_validation_errors(errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Field paths and reasons only. The rejected input is never echoed."""
    fields: list[dict[str, str]] = []
    for error in errors:
        location = [str(part) for part in error.get("loc", ()) if part != "body"]
        reason = str(error.get("msg", "")).strip()
        reason = reason.replace("Value error, ", "")
        fields.append(
            {
                "field": ".".join(location) or "body",
                "reason": reason[:200] or "is not valid",
                "type": str(error.get("type", "value_error"))[:60],
            }
        )
    return {"fields": fields}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        request_id = resolve_request_id(request)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.detail_message, request_id, exc.details),
            headers={REQUEST_ID_HEADER: request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = resolve_request_id(request)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_body(
                "validation_error",
                "The request payload failed validation.",
                request_id,
                sanitize_validation_errors(list(exc.errors())),
            ),
            headers={REQUEST_ID_HEADER: request_id},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        request_id = resolve_request_id(request)
        mapping = {
            404: ("not_found", "The requested resource does not exist."),
            405: ("method_not_allowed", "That method is not allowed here."),
        }
        code, message = mapping.get(
            exc.status_code, ("request_failed", "The request could not be completed.")
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, message, request_id),
            headers={REQUEST_ID_HEADER: request_id},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        request_id = resolve_request_id(request)
        logger.exception(
            "unhandled_exception",
            extra={"request_id": request_id, "error_type": type(exc).__name__},
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body(
                "internal_error",
                "The request could not be completed.",
                request_id,
            ),
            headers={REQUEST_ID_HEADER: request_id},
        )
