"""ByteTrack adapter over externally supplied detections.

Ultralytics' ``YOLO.track`` is deliberately NOT used: it attaches a tracker to
mutable ``model.predictor`` state shared by every caller of one model, and it
attempts a runtime dependency install. This adapter instantiates
``trackers.ByteTrackTracker`` directly, one instance per worker session, so two
cameras can never share tracker state.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any, Final, Iterable, Sequence

from app.analytics.types import Detection, TrackedObject
from app.domain.models import ObjectCategory

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "ByteTrackAdapter",
    "TrackerInvariantError",
    "DEFAULT_LOST_TRACK_BUFFER",
    "MINIMUM_CONSECUTIVE_FRAMES",
    "MINIMUM_IOU_THRESHOLD",
    "HIGH_CONFIDENCE_FLOOR",
]

logger = logging.getLogger("analytics.tracker")

DEFAULT_LOST_TRACK_BUFFER: Final[int] = 30
MINIMUM_CONSECUTIVE_FRAMES: Final[int] = 2
MINIMUM_IOU_THRESHOLD: Final[float] = 0.1
HIGH_CONFIDENCE_FLOOR: Final[float] = 0.6


class TrackerInvariantError(RuntimeError):
    """The tracker returned something this application cannot trust."""


class ByteTrackAdapter:
    """One ByteTrack instance, owned exclusively by one worker session."""

    def __init__(
        self,
        *,
        inference_fps: float,
        confidence_threshold: float,
        lost_track_buffer: int = DEFAULT_LOST_TRACK_BUFFER,
        tracker_factory: Any = None,
    ):
        self._inference_fps = float(inference_fps)
        self._confidence_threshold = float(confidence_threshold)
        self._lost_track_buffer = int(lost_track_buffer)
        self._factory = tracker_factory
        self._tracker = self._build()
        self._issued_ids: set[int] = set()
        self._last_returned_ids: set[int] = set()

    # -- construction -------------------------------------------------------

    @property
    def constructor_kwargs(self) -> dict[str, Any]:
        """Every value passed explicitly; no library default is left implicit."""
        return {
            "lost_track_buffer": self._lost_track_buffer,
            "frame_rate": self._inference_fps,
            "track_activation_threshold": self._confidence_threshold,
            "minimum_consecutive_frames": MINIMUM_CONSECUTIVE_FRAMES,
            "minimum_iou_threshold": MINIMUM_IOU_THRESHOLD,
            "high_conf_det_threshold": max(
                HIGH_CONFIDENCE_FLOOR, self._confidence_threshold
            ),
        }

    def _build(self) -> Any:
        factory = self._factory
        if factory is None:
            from trackers import ByteTrackTracker

            factory = ByteTrackTracker
        try:
            return factory(**self.constructor_kwargs)
        except TypeError as exc:
            raise TrackerInvariantError(
                f"ByteTrack constructor rejected the frozen parameter set: {exc}"
            ) from exc

    # -- update -------------------------------------------------------------

    def update(
        self,
        detections: Sequence[Detection],
        *,
        frame: "NDArray[np.uint8]",
        timestamp: float,
    ) -> list[TrackedObject]:
        """Feed one analytics opportunity and return the confirmed tracks.

        ``timestamp`` is monotonic, so a dropped or busy frame expresses real
        elapsed time to the tracker rather than a fictitious constant cadence.
        """
        sv_detections = self._to_supervision(detections)
        result = self._call_update(sv_detections, frame=frame, timestamp=timestamp)
        return self._from_supervision(result)

    def _call_update(self, detections: Any, *, frame: Any, timestamp: float) -> Any:
        update = self._tracker.update
        try:
            parameters = inspect.signature(update).parameters
        except (TypeError, ValueError):  # pragma: no cover - C callables
            parameters = {}
        kwargs: dict[str, Any] = {}
        if "timestamp" in parameters:
            kwargs["timestamp"] = timestamp
        # `frame` is in the signature but ByteTrack does not use it, and passing
        # it makes trackers 2.6.0 emit a UserWarning on every single update.
        del frame
        try:
            return update(detections, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise TrackerInvariantError(
                f"tracker update failed: {type(exc).__name__}: {exc}"
            ) from exc

    def _to_supervision(self, detections: Sequence[Detection]) -> Any:
        import numpy as np
        import supervision as sv

        count = len(detections)
        xyxy = np.empty((count, 4), dtype=np.float32)
        confidence = np.empty((count,), dtype=np.float32)
        class_id = np.empty((count,), dtype=int)
        object_class = np.empty((count,), dtype=object)
        object_category = np.empty((count,), dtype=object)
        for index, detection in enumerate(detections):
            xyxy[index] = (detection.x1, detection.y1, detection.x2, detection.y2)
            confidence[index] = detection.confidence
            class_id[index] = detection.coco_class_id
            object_class[index] = detection.object_class
            object_category[index] = detection.object_category.value
        return sv.Detections(
            xyxy=xyxy,
            confidence=confidence,
            class_id=class_id,
            data={
                "object_class": object_class,
                "object_category": object_category,
            },
        )

    def _from_supervision(self, result: Any) -> list[TrackedObject]:
        if result is None:
            return []
        tracker_ids = getattr(result, "tracker_id", None)
        if tracker_ids is None:
            self._last_returned_ids = set()
            return []
        xyxy = result.xyxy
        confidence = getattr(result, "confidence", None)
        class_id = getattr(result, "class_id", None)
        data = getattr(result, "data", {}) or {}
        classes = data.get("object_class")
        categories = data.get("object_category")

        tracked: list[TrackedObject] = []
        seen: set[int] = set()
        for index in range(len(tracker_ids)):
            raw_id = tracker_ids[index]
            if raw_id is None:
                continue
            track_id = int(raw_id)
            if track_id < 0:
                continue
            if track_id in seen:
                raise TrackerInvariantError(
                    "tracker returned the same track id twice in one update"
                )
            seen.add(track_id)

            object_class = self._resolve_class(classes, class_id, index)
            category = self._resolve_category(categories, index, object_class)
            score = (
                float(confidence[index])
                if confidence is not None and index < len(confidence)
                else 0.0
            )
            x1, y1, x2, y2 = (float(value) for value in xyxy[index])
            if x2 <= x1 or y2 <= y1:
                continue
            tracked.append(
                TrackedObject(
                    track_id=track_id,
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    confidence=min(max(score, 0.0), 1.0),
                    object_class=object_class,
                    object_category=category,
                )
            )
        self._issued_ids |= seen
        self._last_returned_ids = seen
        return tracked

    @staticmethod
    def _resolve_class(classes: Any, class_id: Any, index: int) -> str:
        from app.domain.models import SUPPORTED_COCO_CLASSES

        if classes is not None and index < len(classes) and classes[index]:
            return str(classes[index])
        if class_id is not None and index < len(class_id):
            mapping = SUPPORTED_COCO_CLASSES.get(int(class_id[index]))
            if mapping is not None:
                return mapping[0]
        raise TrackerInvariantError("tracker result lost its object class")

    @staticmethod
    def _resolve_category(
        categories: Any, index: int, object_class: str
    ) -> ObjectCategory:
        from app.domain.models import SUPPORTED_COCO_CLASSES

        if categories is not None and index < len(categories) and categories[index]:
            try:
                return ObjectCategory(str(categories[index]))
            except ValueError as exc:
                raise TrackerInvariantError(
                    "tracker returned an unknown object category"
                ) from exc
        for coco_class, category in SUPPORTED_COCO_CLASSES.values():
            if coco_class == object_class:
                return category
        raise TrackerInvariantError("tracker result lost its object category")

    # -- lifecycle ----------------------------------------------------------

    def active_track_ids(self) -> set[int]:
        """Ids the tracker still considers tracked or lost, best effort.

        The adapter falls back to the ids returned by the last update when the
        library exposes no internal view, which is safe: pruning a history that
        the tracker later revives simply restarts that track's side anchor.
        """
        ids: set[int] = set(self._last_returned_ids)
        for attribute in ("tracked_tracks", "lost_tracks", "trackers", "tracks"):
            container = getattr(self._tracker, attribute, None)
            if not isinstance(container, Iterable):
                continue
            for item in container:
                for id_attribute in ("tracker_id", "external_track_id", "track_id", "id"):
                    value = getattr(item, id_attribute, None)
                    if isinstance(value, (int,)) and value >= 0:
                        ids.add(int(value))
                        break
        return ids

    def reset(self) -> None:
        """Drop all continuity. The owner allocates a new session id."""
        reset = getattr(self._tracker, "reset", None)
        if callable(reset):
            reset()
        else:  # pragma: no cover - library without reset
            self._tracker = self._build()
        self._issued_ids.clear()
        self._last_returned_ids.clear()

    @property
    def issued_track_ids(self) -> frozenset[int]:
        return frozenset(self._issued_ids)
