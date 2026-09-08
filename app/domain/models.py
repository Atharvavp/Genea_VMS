"""Identifiers, timestamps, enums, records, and the public request/response types.

Two conventions are load-bearing:

* Every timestamp crossing a boundary - SQLite, HTTP, cursor comparison - is a
  timezone-aware UTC instant serialized as exactly ``YYYY-MM-DDTHH:MM:SS.mmmZ``.
  Millisecond truncation is applied on the way in, so a value read back compares
  byte-for-byte with the value that was stored.
* Identifiers keep Component 4's formats and are validated on the way in, but
  are otherwise opaque strings. Component 5 never parses meaning out of them and
  never derives an upstream path from anything except a validated event id.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "EVENT_ID_PATTERN", "CAMERA_ID_PATTERN", "VMS_CAMERA_ID_PATTERN",
    "ObjectCategory", "ObjectClass", "Direction", "RepresentationKind",
    "RepresentationState", "EventState", "UpstreamState",
    "EventRecord", "RepresentationRecord", "SearchFilters", "ScoredEvent",
    "format_utc", "parse_utc", "utc_now", "new_request_id",
    "is_event_id", "TextSearchRequest", "SearchFiltersModel", "SearchResponse",
    "SearchResultItem", "EventDetailResponse", "RecordingLookupResponse",
    "RecordingPayload", "IndexStatusResponse", "FacetsResponse", "HealthResponse",
]

# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

EVENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^evt_[0-9a-f]{32}$")
CAMERA_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^acam_[0-9a-f]{8}$")
VMS_CAMERA_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^cam_[0-9a-f]{8}$")
_REQUEST_ID_BYTES: Final[int] = 16


def is_event_id(value: object) -> bool:
    """True only for Component 4's exact event-id shape."""
    return isinstance(value, str) and EVENT_ID_PATTERN.fullmatch(value) is not None


def new_request_id() -> str:
    return f"req_{secrets.token_hex(_REQUEST_ID_BYTES)}"


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

_TIMESTAMP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|z|[+-]\d{2}:?\d{2})$"
)


def utc_now() -> datetime:
    """Millisecond-truncated aware UTC now."""
    moment = datetime.now(timezone.utc)
    return moment.replace(microsecond=(moment.microsecond // 1000) * 1000)


def format_utc(moment: datetime) -> str:
    """Serialize as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` - the one stored form."""
    if moment.tzinfo is None:
        raise ValueError("timestamp must be timezone aware")
    utc = moment.astimezone(timezone.utc)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{utc.microsecond // 1000:03d}Z"


def parse_utc(value: str | datetime) -> datetime:
    """Parse an ISO-8601 instant into millisecond-truncated aware UTC.

    A naive value is rejected rather than assumed to be UTC: a timestamp with no
    zone is a contract violation upstream, and silently guessing would corrupt
    the discovery high-water mark.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone aware")
        moment = value.astimezone(timezone.utc)
    else:
        text = value.strip()
        if not _TIMESTAMP_PATTERN.fullmatch(text):
            raise ValueError("timestamp is not a supported ISO-8601 UTC instant")
        normalized = text.replace(" ", "T")
        if normalized.endswith(("Z", "z")):
            normalized = normalized[:-1] + "+00:00"
        moment = datetime.fromisoformat(normalized).astimezone(timezone.utc)
    return moment.replace(microsecond=(moment.microsecond // 1000) * 1000)


# ---------------------------------------------------------------------------
# Enumerations - exactly Component 4's implemented value sets
# ---------------------------------------------------------------------------


class ObjectCategory(StrEnum):
    PERSON = "person"
    VEHICLE = "vehicle"


class ObjectClass(StrEnum):
    PERSON = "person"
    BICYCLE = "bicycle"
    CAR = "car"
    MOTORCYCLE = "motorcycle"
    BUS = "bus"
    TRUCK = "truck"


class Direction(StrEnum):
    A_TO_B = "A_TO_B"
    B_TO_A = "B_TO_A"


class RepresentationKind(StrEnum):
    CROP = "crop"
    FRAME = "frame"


class RepresentationState(StrEnum):
    PENDING = "pending"
    INDEXING = "indexing"
    INDEXED = "indexed"
    RETRYABLE = "retryable"
    PERMANENT_ERROR = "permanent_error"


class EventState(StrEnum):
    """Computed from representation rows; never stored (PLAN section 9)."""

    UNINDEXED = "unindexed"
    SEARCHABLE = "searchable"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class UpstreamState(StrEnum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


#: Component 4's category/class pairing, used to answer contradictory filters
#: with an empty result instead of rewriting the caller's request.
CLASS_CATEGORY: Final[dict[ObjectClass, ObjectCategory]] = {
    ObjectClass.PERSON: ObjectCategory.PERSON,
    ObjectClass.BICYCLE: ObjectCategory.VEHICLE,
    ObjectClass.CAR: ObjectCategory.VEHICLE,
    ObjectClass.MOTORCYCLE: ObjectCategory.VEHICLE,
    ObjectClass.BUS: ObjectCategory.VEHICLE,
    ObjectClass.TRUCK: ObjectCategory.VEHICLE,
}


# ---------------------------------------------------------------------------
# Internal records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One locally mirrored Component 4 event. Metadata only - never an image."""

    event_id: str
    camera_id: str
    vms_camera_id: str
    camera_name: str
    crossed_at: datetime
    object_category: ObjectCategory
    object_class: ObjectClass
    direction: Direction
    discovered_at: datetime
    source_last_seen_at: datetime

    def __post_init__(self) -> None:
        if not is_event_id(self.event_id):
            raise ValueError("event_id is not a valid Component 4 event id")
        if not CAMERA_ID_PATTERN.fullmatch(self.camera_id):
            raise ValueError("camera_id is not a valid analytics camera id")
        if not VMS_CAMERA_ID_PATTERN.fullmatch(self.vms_camera_id):
            raise ValueError("vms_camera_id is not a valid VMS camera id")
        if not self.camera_name or len(self.camera_name) > 200:
            raise ValueError("camera_name must be 1..200 characters")
        for field_name in ("crossed_at", "discovered_at", "source_last_seen_at"):
            moment = getattr(self, field_name)
            if moment.tzinfo is None:
                raise ValueError(f"{field_name} must be timezone aware")


@dataclass(frozen=True, slots=True)
class RepresentationRecord:
    """One (event, kind, model) vector identity and its durable state."""

    event_id: str
    kind: RepresentationKind
    model_id: str
    state: RepresentationState
    embedding: bytes | None = None
    dimension: int | None = None
    dtype: str | None = None
    l2_norm: float | None = None
    embedding_sha256: str | None = None
    attempt_count: int = 0
    next_retry_at: datetime | None = None
    last_error_code: str | None = None
    last_error_at: datetime | None = None
    indexed_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_indexed(self) -> bool:
        return self.state is RepresentationState.INDEXED


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Metadata predicate applied BEFORE any vector is scored (PLAN 14.1)."""

    camera_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    object_category: ObjectCategory | None = None
    object_class: ObjectClass | None = None
    direction: Direction | None = None

    def __post_init__(self) -> None:
        if self.camera_id is not None and not CAMERA_ID_PATTERN.fullmatch(
            self.camera_id
        ):
            raise ValueError("camera_id is not a valid analytics camera id")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("'from' must be strictly before 'to'")

    @property
    def is_contradictory(self) -> bool:
        """A class that cannot belong to the requested category matches nothing.

        The filter is answered as empty rather than rewritten: silently dropping
        one half would return results the caller did not ask for.
        """
        if self.object_class is None or self.object_category is None:
            return False
        return CLASS_CATEGORY[self.object_class] is not self.object_category

    @property
    def is_empty(self) -> bool:
        return all(
            value is None
            for value in (
                self.camera_id, self.start, self.end,
                self.object_category, self.object_class, self.direction,
            )
        )


@dataclass(frozen=True, slots=True)
class ScoredEvent:
    """One ranked event. Never one per representation (PLAN section 6.4)."""

    event_id: str
    score: float
    crop_score: float | None
    frame_score: float | None


# ---------------------------------------------------------------------------
# Public API models
# ---------------------------------------------------------------------------

_STRICT = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchFiltersModel(BaseModel):
    """Filters shared by both search modes. All optional, AND-combined."""

    model_config = _STRICT

    camera_id: str | None = Field(default=None, max_length=64)
    start: datetime | None = Field(default=None, alias="from")
    end: datetime | None = Field(default=None, alias="to")
    object_category: ObjectCategory | None = None
    object_class: ObjectClass | None = None
    direction: Direction | None = None

    @field_validator("camera_id")
    @classmethod
    def _camera(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not CAMERA_ID_PATTERN.fullmatch(value):
            raise ValueError("is not a valid analytics camera id")
        return value

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("must include a UTC offset")
        return parse_utc(value)

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("'from' must be strictly before 'to'")
        return self

    def to_filters(self) -> SearchFilters:
        return SearchFilters(
            camera_id=self.camera_id,
            start=self.start,
            end=self.end,
            object_category=self.object_category,
            object_class=self.object_class,
            direction=self.direction,
        )


class TextSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    top_k: int = Field(default=20, ge=1, le=100)
    min_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    filters: SearchFiltersModel = Field(default_factory=SearchFiltersModel)


class RepresentationScores(BaseModel):
    crop: float | None = None
    frame: float | None = None


class SearchResultItem(BaseModel):
    rank: int
    event_id: str
    score: float
    representation_scores: RepresentationScores
    camera_id: str
    vms_camera_id: str
    camera_name: str
    crossed_at: str
    object_category: ObjectCategory
    object_class: ObjectClass
    direction: Direction
    crop_url: str
    frame_url: str
    detail_url: str
    recording_url: str


class SearchResponse(BaseModel):
    mode: Literal["text", "image"]
    model_id: str
    index_revision: int
    candidate_count: int
    elapsed_ms: float
    results: list[SearchResultItem]


class EventDetailResponse(BaseModel):
    """Local metadata only, so event detail survives a Component 4 outage."""

    event_id: str
    camera_id: str
    vms_camera_id: str
    camera_name: str
    crossed_at: str
    object_category: ObjectCategory
    object_class: ObjectClass
    direction: Direction
    discovered_at: str
    source_last_seen_at: str
    index_state: EventState
    representations: dict[str, str]
    crop_url: str
    frame_url: str
    recording_url: str


class RecordingPayload(BaseModel):
    id: str
    start_time: str
    end_time: str
    duration_seconds: float
    playback_url: str
    source: str


class RecordingLookupResponse(BaseModel):
    status: Literal["AVAILABLE", "NOT_FOUND", "UNAVAILABLE"]
    event_id: str
    recording: RecordingPayload | None = None
    reason: str | None = None


class CameraFacet(BaseModel):
    camera_id: str
    camera_name: str
    count: int


class FacetsResponse(BaseModel):
    cameras: list[CameraFacet]
    object_categories: list[str]
    object_classes: list[str]
    directions: list[str]
    earliest_crossed_at: str | None
    latest_crossed_at: str | None
    searchable_events: int


class IndexStatusResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    model_revision: str
    model_sha256: str
    embedding_dim: int
    embedding_dtype: str
    index_revision: int
    known_events: int
    searchable_events: int
    complete_events: int
    partial_events: int
    failed_events: int
    crop_indexed: int
    frame_indexed: int
    pending_representations: int
    retryable_representations: int
    permanent_error_representations: int
    oldest_pending_at: str | None
    backfill_complete: bool
    backfill_phase: str
    high_water_crossed_at: str | None
    last_successful_poll_at: str | None
    last_full_reconcile_at: str | None
    upstream_state: UpstreamState
    upstream_error_code: str | None
    indexing: str
    search: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    search: str
    model: str
    database: str
    index: str
    upstream: str
    indexing: str


def error_details(**fields: Any) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}
