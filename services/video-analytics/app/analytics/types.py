"""Hot-path value types shared by the pipeline, detector, tracker, and worker.

All of these are immutable. Arrays that cross from GStreamer memory into
application code are owned, C-contiguous NumPy copies: no ``Gst.Buffer`` map
survives the ``pull-sample`` processing that produced it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from app.domain.models import (
    CrossingDirection,
    LineDirection,
    LineRecord,
    ObjectCategory,
    RuntimeState,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "AttemptOutcome",
    "FatalReason",
    "PipelineError",
    "FramePacket",
    "Detection",
    "DetectionBatch",
    "TrackedObject",
    "WorkerConfig",
    "WorkerRuntime",
    "EventCandidate",
    "LatestFrame",
    "CommitOutcome",
    "CommitResult",
]


class AttemptOutcome(str, Enum):
    """Why one GStreamer pipeline attempt ended. All are recoverable."""

    SOURCE_ERROR = "source_error"
    SOURCE_EOS = "source_eos"
    SOURCE_STALL = "source_stall"
    SOURCE_START_FAILED = "source_start_failed"
    STOPPED = "stopped"


class FatalReason(str, Enum):
    """Failures that end the worker thread until configuration changes."""

    UNSUPPORTED_CODEC = "unsupported_codec"
    MODEL_UNAVAILABLE = "model_unavailable"
    DETECTOR_FAILED = "detector_failed"
    TRACKER_INVARIANT = "tracker_invariant"
    TRACKER_STATE_OVERFLOW = "tracker_state_overflow"
    EVENT_PERSISTENCE_FAILED = "event_persistence_failed"
    INTERNAL_INVARIANT = "internal_invariant"
    WORKER_START_FAILED = "worker_start_failed"
    WORKER_STOP_TIMEOUT = "worker_stop_timeout"


@dataclass(frozen=True, slots=True)
class PipelineError:
    """A sanitized pipeline failure. ``fatal`` short-circuits the retry loop."""

    outcome: AttemptOutcome | None
    code: str
    message: str
    fatal_reason: FatalReason | None = None

    @property
    def is_fatal(self) -> bool:
        return self.fatal_reason is not None


@dataclass(frozen=True, slots=True)
class FramePacket:
    """One decoded RGB frame owned entirely by the application."""

    rgb: "NDArray[np.uint8]"
    received_at_utc: datetime
    received_monotonic: float
    source_pts_ns: int | None
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 1 or self.height <= 1:
            raise ValueError("frame dimensions must both exceed one pixel")


@dataclass(frozen=True, slots=True)
class Detection:
    """One accepted detection in pixel space, top-left origin."""

    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    coco_class_id: int
    object_class: str
    object_category: ObjectCategory

    def __post_init__(self) -> None:
        for value in (self.x1, self.y1, self.x2, self.y2, self.confidence):
            if not math.isfinite(value):
                raise ValueError("detection values must be finite")
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError("detection box must have positive area")


@dataclass(frozen=True, slots=True)
class DetectionBatch:
    """Result of one admitted inference. May legitimately be empty."""

    detections: tuple[Detection, ...]
    inference_seconds: float

    def __len__(self) -> int:
        return len(self.detections)


@dataclass(frozen=True, slots=True)
class TrackedObject:
    """A confirmed track returned by the ByteTrack adapter."""

    track_id: int
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    object_class: str
    object_category: ObjectCategory

    def normalized_centroid(self, width: int, height: int) -> tuple[float, float]:
        cx = ((self.x1 + self.x2) / 2.0) / float(width)
        cy = ((self.y1 + self.y2) / 2.0) / float(height)
        return (min(max(cx, 0.0), 1.0), min(max(cy, 0.0), 1.0))


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Immutable snapshot of everything one worker iteration may read."""

    camera_id: str
    vms_camera_id: str
    camera_name: str
    rtsp_url: str
    inference_fps: float
    confidence_threshold: float
    enabled_categories: frozenset[ObjectCategory]
    line: LineRecord | None
    config_revision: int
    requested_session_id: str

    @property
    def active_line(self) -> LineRecord | None:
        """The line only counts when it exists and is enabled."""
        if self.line is not None and self.line.enabled:
            return self.line
        return None

    @property
    def line_direction(self) -> LineDirection | None:
        line = self.active_line
        return line.direction if line else None


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """Bounded, immutable snapshot published to the manager registry."""

    camera_id: str
    worker_instance_id: str | None
    worker_session_id: str | None
    state: RuntimeState
    stopping: bool
    last_frame_at: datetime | None
    last_inference_at: datetime | None
    last_event_at: datetime | None
    reconnect_attempt: int
    applied_config_revision: int
    last_error_code: str | None
    last_error_message: str | None
    updated_at: datetime
    frames_received: int = 0
    inferences_run: int = 0
    inference_busy_drops: int = 0
    events_recorded: int = 0

    def replace(self, **changes: Any) -> "WorkerRuntime":
        from dataclasses import replace as _replace

        return _replace(self, **changes)


@dataclass(frozen=True, slots=True)
class EventCandidate:
    """Everything needed to persist one crossing, plus the frame it came from.

    ``rgb`` is borrowed for the duration of the synchronous
    :meth:`EventStorage.commit_event` call only; the worker iteration that built
    the candidate still owns it.
    """

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
    bbox_pixels: tuple[float, float, float, float]
    centroid_normalized: tuple[float, float]
    frame_width: int
    frame_height: int
    source_pts_ns: int | None
    rgb: "NDArray[np.uint8]" = field(repr=False, default=None)  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class LatestFrame:
    """Copy of a worker's most recent decoded frame, safe to encode."""

    sequence: int
    rgb: "NDArray[np.uint8]"
    received_at_utc: datetime
    width: int
    height: int
    age_seconds: float


class CommitOutcome(str, Enum):
    INSERTED = "INSERTED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    INVALID_CROP = "INVALID_CROP"


@dataclass(frozen=True, slots=True)
class CommitResult:
    outcome: CommitOutcome
    event_id: str | None
    crossed_at: datetime | None
