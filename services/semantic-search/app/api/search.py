"""Text and image search routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile

from app.api.errors import (
    ImageTooLargeError,
    InferenceQueueFullError,
    InvalidImageError,
    InvalidQueryError,
    InvalidTimeRangeError,
    SearchUnavailableError,
    UnsupportedImageTypeError,
)
from app.domain.models import (
    Direction,
    ObjectCategory,
    ObjectClass,
    SearchFiltersModel,
    SearchResponse,
    TextSearchRequest,
)
from app.security.images import (
    ImageRejected,
    ImageTooLarge,
    UnsupportedImageType,
    decode_query_image,
)
from app.services.inference import InferenceQueueFull
from app.services.search import QueryRejected, SearchService, normalize_query

__all__ = ["router"]

router = APIRouter(prefix="/api/search", tags=["search"])


def get_search_service(request: Request) -> SearchService:
    service: SearchService | None = getattr(request.app.state, "search_service", None)
    if service is None or not service.ready:
        raise SearchUnavailableError()
    return service


@router.post("/text", response_model=SearchResponse)
async def search_text(
    payload: TextSearchRequest,
    service: SearchService = Depends(get_search_service),
) -> SearchResponse:
    try:
        query = normalize_query(payload.query)
    except QueryRejected as exc:
        # The rejected text itself is never echoed back.
        raise InvalidQueryError(str(exc)) from exc
    try:
        filters = payload.filters.to_filters()
    except ValueError as exc:
        raise InvalidTimeRangeError(str(exc)) from exc
    try:
        return await service.search_text(
            query=query,
            filters=filters,
            top_k=payload.top_k,
            min_score=payload.min_score,
        )
    except InferenceQueueFull as exc:
        raise InferenceQueueFullError() from exc


@router.post("/image", response_model=SearchResponse)
async def search_image(
    request: Request,
    image: Annotated[UploadFile, File(description="JPEG or PNG query image")],
    top_k: int = Query(default=20, ge=1, le=100),
    min_score: float | None = Query(default=None, ge=-1.0, le=1.0),
    camera_id: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    object_category: ObjectCategory | None = Query(default=None),
    object_class: ObjectClass | None = Query(default=None),
    direction: Direction | None = Query(default=None),
    service: SearchService = Depends(get_search_service),
) -> SearchResponse:
    settings = request.app.state.settings
    try:
        model = SearchFiltersModel(
            camera_id=camera_id,
            object_category=object_category,
            object_class=object_class,
            direction=direction,
            **{"from": from_, "to": to},
        )
        filters = model.to_filters()
    except ValueError as exc:
        raise InvalidTimeRangeError(str(exc)) from exc

    # The upload is read with a hard cap: one byte over and it is refused
    # before any decoding is attempted. The filename is never consulted.
    limit = settings.semantic_upload_max_bytes
    payload = await image.read(limit + 1)
    await image.close()
    if len(payload) > limit:
        raise ImageTooLargeError()

    decoded = None
    try:
        decoded = decode_query_image(
            payload,
            max_bytes=limit,
            max_pixels=settings.semantic_upload_max_pixels,
            max_edge=settings.upload_max_edge,
        )
    except ImageTooLarge as exc:
        raise ImageTooLargeError() from exc
    except UnsupportedImageType as exc:
        raise UnsupportedImageTypeError() from exc
    except ImageRejected as exc:
        raise InvalidImageError() from exc
    finally:
        # The raw bytes are request-local and dropped as soon as possible.
        del payload

    try:
        return await service.search_image(
            image=decoded, filters=filters, top_k=top_k, min_score=min_score
        )
    except InferenceQueueFull as exc:
        raise InferenceQueueFullError() from exc
    finally:
        if decoded is not None:
            decoded.close()
