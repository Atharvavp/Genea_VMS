"""API error type and exception handlers producing a stable error envelope."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

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


class CameraNotFound(ApiError):
    def __init__(self, camera_id: str) -> None:
        super().__init__(404, "camera_not_found", f"No camera with id '{camera_id}'.")


class DuplicateStreamPath(ApiError):
    def __init__(self, stream_path: str) -> None:
        super().__init__(
            409,
            "duplicate_stream_path",
            f"Stream path '{stream_path}' is already in use.",
            {"stream_path": stream_path},
        )


class OperationInProgress(ApiError):
    def __init__(self, camera_id: str, status: str) -> None:
        super().__init__(
            409,
            "operation_in_progress",
            f"Camera is currently {status}; try again once it settles.",
            {"camera_id": camera_id, "status": status},
        )


class ValidationFailed(ApiError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(422, "validation_error", message, details)


class SourceRejected(ApiError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(422, "invalid_source", message, details)


class BadRequest(ApiError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(400, "bad_request", message, details)


def _pydantic_details(exc: ValidationError | RequestValidationError) -> dict[str, Any]:
    fields = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        fields.append({"field": location or "body", "message": error.get("msg", "invalid")})
    return {"fields": fields}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return exc.to_response()

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
        logger.exception("unhandled_error error=%s", exc)
        return ApiError(
            500, "internal_error", "An unexpected internal error occurred."
        ).to_response()
