"""Domain enums, identifiers, immutable records, and HTTP data contracts.

Nothing in this module performs I/O. Frozen dataclasses carry hot-path values
between threads; Pydantic models carry HTTP values.
"""

from __future__ import annotations

import math
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

__all__ = [
    "RuntimeState",
    "LineDirection",
    "CrossingDirection",
    "ObjectCategory",
    "RecordingStatus",
    "ImageKind",
    "SUPPORTED_COCO_CLASSES",
    "SUPPORTED_OBJECT_CLASSES",
    "CAMERA_ID_RE",
    "LINE_ID_RE",
    "EVENT_ID_RE",
    "VMS_CAMERA_ID_RE",
    "WORKER_SESSION_ID_RE",
    "new_camera_id",
    "new_line_id",
    "new_event_id",
    "new_worker_instance_id",
    "new_worker_session_id",
    "new_request_id",
    "utc_now",
    "to_iso_ms",
    "parse_iso_utc",
    "CameraRecord",
    "LineRecord",
    "EventRecord",
    "Point",
    "BoundingBox",
    "CameraCreateRequest",
    "CameraPatchRequest",
    "LinePutRequest",
    "RuntimeView",
    "CameraResponse",
    "LineResponse",
    "EventSummaryResponse",
    "EventDetailResponse",
    "EventPageResponse",
    "RecordingItemResponse",
    "RecordingLookupResponse",
    "HealthResponse",
    "WorkerCounts",
    "MIN_LINE_LENGTH",
]


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RuntimeState(str, Enum):
    """Public per-camera analytics runtime state.

    ``STOPPING`` is deliberately absent: it is an internal manager/worker
    transition exposed as the boolean ``stopping`` beside the last state.
    """

    DISABLED = "DISABLED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"


class LineDirection(str, Enum):
    """Configured crossing policy for one line."""

    A_TO_B = "A_TO_B"
    B_TO_A = "B_TO_A"
    BOTH = "BOTH"


class CrossingDirection(str, Enum):
    """An actual observed crossing. ``BOTH`` is never stored on an event."""

    A_TO_B = "A_TO_B"
    B_TO_A = "B_TO_A"


class ObjectCategory(str, Enum):
    PERSON = "person"
    VEHICLE = "vehicle"


class RecordingStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"


class ImageKind(str, Enum):
    FRAME = "frame"
    CROP = "crop"


#: The only COCO classes Component 4 accepts. ``train`` and ``boat`` are
#: deliberately absent: they are not remapped to ``vehicle``.
SUPPORTED_COCO_CLASSES: Final[dict[int, tuple[str, ObjectCategory]]] = {
    0: ("person", ObjectCategory.PERSON),
    1: ("bicycle", ObjectCategory.VEHICLE),
    2: ("car", ObjectCategory.VEHICLE),
    3: ("motorcycle", ObjectCategory.VEHICLE),
    5: ("bus", ObjectCategory.VEHICLE),
    7: ("truck", ObjectCategory.VEHICLE),
}

SUPPORTED_OBJECT_CLASSES: Final[frozenset[str]] = frozenset(
    name for name, _ in SUPPORTED_COCO_CLASSES.values()
)

MIN_LINE_LENGTH: Final[float] = 0.05


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

CAMERA_ID_RE: Final[re.Pattern[str]] = re.compile(r"^acam_[0-9a-f]{8}$")
LINE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^line_[0-9a-f]{8}$")
EVENT_ID_RE: Final[re.Pattern[str]] = re.compile(r"^evt_[0-9a-f]{32}$")
VMS_CAMERA_ID_RE: Final[re.Pattern[str]] = re.compile(r"^cam_[0-9a-f]{8}$")
WORKER_INSTANCE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^wi_[0-9a-f]{32}$")
WORKER_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^ws_[0-9a-f]{32}$")
REQUEST_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def new_camera_id(token: Any = None) -> str:
    return "acam_" + (token or secrets.token_hex)(4)


def new_line_id(token: Any = None) -> str:
    return "line_" + (token or secrets.token_hex)(4)


def new_event_id(token: Any = None) -> str:
    return "evt_" + (token or secrets.token_hex)(16)


def new_worker_instance_id(token: Any = None) -> str:
    return "wi_" + (token or secrets.token_hex)(16)


def new_worker_session_id(token: Any = None) -> str:
    return "ws_" + (token or secrets.token_hex)(16)


def new_request_id(token: Any = None) -> str:
    return "req_" + (token or secrets.token_hex)(16)


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Current wall-clock instant, timezone aware, truncated to milliseconds."""
    now = datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def to_iso_ms(value: datetime) -> str:
    """Serialise as ``YYYY-MM-DDTHH:MM:SS.mmmZ``.

    Exactly millisecond precision keeps lexical and chronological ordering in
    agreement, which the event cursor seek predicate relies on.
    """
    if value.tzinfo is None:
        raise ValueError("naive datetimes are never serialised")
    moment = value.astimezone(UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


def parse_iso_utc(value: str) -> datetime:
    """Parse a timezone-aware ISO-8601 timestamp into UTC.

    Accepts the trailing ``Z`` form this service emits and the explicit-offset
    forms Component 3 may return. A naive timestamp is rejected: guessing a
    zone would silently move an event in time.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must carry a timezone offset")
    return parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# Immutable persisted records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CameraRecord:
    id: str
    vms_camera_id: str
    name: str
    rtsp_url: str
    enabled: bool
    inference_fps: float
    confidence_threshold: float
    enabled_classes: frozenset[ObjectCategory]
    created_at: datetime
    updated_at: datetime

    def sorted_classes(self) -> list[str]:
        return sorted(category.value for category in self.enabled_classes)


@dataclass(frozen=True, slots=True)
class LineRecord:
    id: str
    camera_id: str
    name: str
    x1: float
    y1: float
    x2: float
    y2: float
    direction: LineDirection
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @property
    def a(self) -> tuple[float, float]:
        return (self.x1, self.y1)

    @property
    def b(self) -> tuple[float, float]:
        return (self.x2, self.y2)

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: str
    camera_id: str
    vms_camera_id: str
    camera_name: str
    line_id: str
    line_name: str
    worker_session_id: str
    track_id: int
    object_category: ObjectCategory
    object_class: str
    direction: CrossingDirection
    confidence: float
    crossed_at: datetime
    bbox_x1: float
    bbox_y1: float
    bbox_x2: float
    bbox_y2: float
    centroid_x: float
    centroid_y: float
    frame_width: int
    frame_height: int
    source_pts_ns: int | None
    snapshot_path: str
    crop_path: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Shared HTTP value objects
# ---------------------------------------------------------------------------

_NORMALISED = Annotated[float, Field(ge=0.0, le=1.0)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Point(_StrictModel):
    x: _NORMALISED
    y: _NORMALISED

    @field_validator("x", "y")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("coordinate must be a finite number")
        return float(value)


class BoundingBox(_StrictModel):
    x1: _NORMALISED
    y1: _NORMALISED
    x2: _NORMALISED
    y2: _NORMALISED


def _clean_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    text = value.strip()
    if not (1 <= len(text) <= 100):
        raise ValueError("name must be 1 to 100 characters after trimming")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError("name must not contain control characters")
    return text


def _clean_categories(value: Any) -> frozenset[ObjectCategory]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set,
                                                                frozenset)):
        raise ValueError("enabled_classes must be a list of categories")
    items = list(value)
    if not items:
        raise ValueError("enabled_classes must not be empty")
    if len(items) != len(set(str(item) for item in items)):
        raise ValueError("enabled_classes must not contain duplicates")
    parsed: set[ObjectCategory] = set()
    for item in items:
        try:
            parsed.add(ObjectCategory(item))
        except ValueError as exc:
            raise ValueError("enabled_classes must be 'person' and/or 'vehicle'") from exc
    return frozenset(parsed)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CameraCreateRequest(_StrictModel):
    vms_camera_id: str
    name: str
    rtsp_url: str
    enabled: bool = True
    inference_fps: float = 5.0
    confidence_threshold: float = 0.25
    enabled_classes: list[str] = Field(
        default_factory=lambda: [ObjectCategory.PERSON.value, ObjectCategory.VEHICLE.value]
    )

    @field_validator("vms_camera_id")
    @classmethod
    def _vms_id(cls, value: str) -> str:
        text = str(value).strip()
        if not VMS_CAMERA_ID_RE.fullmatch(text):
            raise ValueError("vms_camera_id must match cam_<8 lowercase hex>")
        return text

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("rtsp_url")
    @classmethod
    def _rtsp(cls, value: str) -> str:
        from app.security.rtsp_url import InvalidRtspUrl, validate_rtsp_url

        try:
            return validate_rtsp_url(value)
        except InvalidRtspUrl as exc:
            raise ValueError(str(exc)) from None

    @field_validator("inference_fps")
    @classmethod
    def _fps(cls, value: float) -> float:
        if not math.isfinite(value) or not (1.0 <= value <= 10.0):
            raise ValueError("inference_fps must be a finite number between 1.0 and 10.0")
        return float(value)

    @field_validator("confidence_threshold")
    @classmethod
    def _confidence(cls, value: float) -> float:
        if not math.isfinite(value) or not (0.10 <= value <= 0.95):
            raise ValueError(
                "confidence_threshold must be a finite number between 0.10 and 0.95"
            )
        return float(value)

    @field_validator("enabled_classes")
    @classmethod
    def _classes(cls, value: Any) -> list[str]:
        return sorted(item.value for item in _clean_categories(value))

    def categories(self) -> frozenset[ObjectCategory]:
        return frozenset(ObjectCategory(item) for item in self.enabled_classes)


class CameraPatchRequest(_StrictModel):
    """Partial camera update.

    Every field is optional, but an empty object and an explicit ``null`` are
    both rejected: silently accepting them would hide a genuine client bug.
    """

    vms_camera_id: str | None = None
    name: str | None = None
    rtsp_url: str | None = None
    enabled: bool | None = None
    inference_fps: float | None = None
    confidence_threshold: float | None = None
    enabled_classes: list[str] | None = None

    _vms_id = field_validator("vms_camera_id")(CameraCreateRequest._vms_id.__func__)  # type: ignore[attr-defined]
    _name = field_validator("name")(CameraCreateRequest._name.__func__)  # type: ignore[attr-defined]
    _rtsp = field_validator("rtsp_url")(CameraCreateRequest._rtsp.__func__)  # type: ignore[attr-defined]
    _fps = field_validator("inference_fps")(CameraCreateRequest._fps.__func__)  # type: ignore[attr-defined]
    _confidence = field_validator("confidence_threshold")(
        CameraCreateRequest._confidence.__func__  # type: ignore[attr-defined]
    )
    _classes = field_validator("enabled_classes")(
        CameraCreateRequest._classes.__func__  # type: ignore[attr-defined]
    )

    @model_validator(mode="after")
    def _not_empty_and_not_null(self) -> "CameraPatchRequest":
        provided = self.model_fields_set
        if not provided:
            raise ValueError("at least one field must be supplied")
        nulls = [name for name in provided if getattr(self, name) is None]
        if nulls:
            raise ValueError(
                "explicit null is not accepted for: " + ", ".join(sorted(nulls))
            )
        return self

    def categories(self) -> frozenset[ObjectCategory] | None:
        if self.enabled_classes is None:
            return None
        return frozenset(ObjectCategory(item) for item in self.enabled_classes)


class LinePutRequest(_StrictModel):
    name: str
    a: Point
    b: Point
    direction: LineDirection
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("direction", mode="before")
    @classmethod
    def _direction(cls, value: Any) -> Any:
        if isinstance(value, LineDirection):
            return value
        if not isinstance(value, str) or value not in {
            item.value for item in LineDirection
        }:
            raise ValueError("direction must be exactly A_TO_B, B_TO_A, or BOTH")
        return value

    @model_validator(mode="after")
    def _length(self) -> "LinePutRequest":
        length = math.hypot(self.b.x - self.a.x, self.b.y - self.a.y)
        if length < MIN_LINE_LENGTH:
            raise ValueError(
                f"line must be at least {MIN_LINE_LENGTH} normalised units long"
            )
        return self


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeErrorView(_ResponseModel):
    code: str
    message: str


class RuntimeView(_ResponseModel):
    state: RuntimeState
    stopping: bool
    worker_session_id: str | None
    last_frame_at: str | None
    last_inference_at: str | None
    last_event_at: str | None
    reconnect_attempt: int
    applied_config_revision: int
    last_error: RuntimeErrorView | None


class CameraResponse(_ResponseModel):
    id: str
    vms_camera_id: str
    name: str
    rtsp_url_masked: str
    rtsp_has_credentials: bool
    enabled: bool
    inference_fps: float
    confidence_threshold: float
    enabled_classes: list[str]
    line_configured: bool
    runtime: RuntimeView
    created_at: str
    updated_at: str


class LineResponse(_ResponseModel):
    id: str
    camera_id: str
    name: str
    a: Point
    b: Point
    direction: LineDirection
    enabled: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_record(cls, record: LineRecord) -> "LineResponse":
        return cls(
            id=record.id,
            camera_id=record.camera_id,
            name=record.name,
            a=Point(x=record.x1, y=record.y1),
            b=Point(x=record.x2, y=record.y2),
            direction=record.direction,
            enabled=record.enabled,
            created_at=to_iso_ms(record.created_at),
            updated_at=to_iso_ms(record.updated_at),
        )


class EventSummaryResponse(_ResponseModel):
    id: str
    camera_id: str
    vms_camera_id: str
    camera_name: str
    line_id: str
    line_name: str
    worker_session_id: str
    track_id: int
    object_category: ObjectCategory
    object_class: str
    direction: CrossingDirection
    confidence: float
    crossed_at: str
    bbox: BoundingBox
    centroid: Point
    frame_width: int
    frame_height: int
    frame_url: str
    crop_url: str

    @classmethod
    def from_record(cls, record: EventRecord) -> "EventSummaryResponse":
        return cls(
            id=record.id,
            camera_id=record.camera_id,
            vms_camera_id=record.vms_camera_id,
            camera_name=record.camera_name,
            line_id=record.line_id,
            line_name=record.line_name,
            worker_session_id=record.worker_session_id,
            track_id=record.track_id,
            object_category=record.object_category,
            object_class=record.object_class,
            direction=record.direction,
            confidence=record.confidence,
            crossed_at=to_iso_ms(record.crossed_at),
            bbox=BoundingBox(
                x1=record.bbox_x1,
                y1=record.bbox_y1,
                x2=record.bbox_x2,
                y2=record.bbox_y2,
            ),
            centroid=Point(x=record.centroid_x, y=record.centroid_y),
            frame_width=record.frame_width,
            frame_height=record.frame_height,
            frame_url=f"/api/events/{record.id}/frame",
            crop_url=f"/api/events/{record.id}/crop",
        )


class EventDetailResponse(EventSummaryResponse):
    """Identical field set: no server path, source URL, or PTS is exposed."""


class EventPageResponse(_ResponseModel):
    items: list[EventSummaryResponse]
    next_cursor: str | None


class RecordingItemResponse(_ResponseModel):
    id: str
    start_time: str
    end_time: str
    duration_seconds: float
    playback_url: str
    source: str | None


class RecordingLookupResponse(_ResponseModel):
    status: RecordingStatus
    event_id: str
    recording: RecordingItemResponse | None
    reason: str | None


class WorkerCounts(_ResponseModel):
    enabled: int
    running: int
    reconnecting: int
    error: int


class HealthResponse(_ResponseModel):
    status: Literal["ok", "degraded"]
    database: str
    event_storage: str
    detector: str
    workers: WorkerCounts
