"""Deterministic detector doubles for the unit and integration tiers."""

from __future__ import annotations

import threading
from typing import Callable, Sequence

from app.analytics.types import Detection, DetectionBatch
from app.domain.models import ObjectCategory


class FakeDetector:
    """Returns scripted detections and records how it was called."""

    def __init__(
        self,
        script: Sequence[Sequence[Detection]] | None = None,
        *,
        ready: bool = True,
        busy: bool = False,
        on_call: Callable[[int], None] | None = None,
    ):
        self._script = [tuple(item) for item in (script or ())]
        self._ready = ready
        self.busy = busy
        self.calls = 0
        self.arguments: list[tuple[float, frozenset[ObjectCategory]]] = []
        self._on_call = on_call
        self._lock = threading.Lock()
        self.max_concurrent = 0
        self._concurrent = 0

    @property
    def ready(self) -> bool:
        return self._ready

    def load(self) -> None:
        self._ready = True

    def release(self) -> None:
        self._ready = False

    def try_detect(self, rgb, confidence, enabled_categories):
        if self.busy:
            return None
        with self._lock:
            self._concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self._concurrent)
        try:
            index = self.calls
            self.calls += 1
            self.arguments.append((confidence, enabled_categories))
            if self._on_call is not None:
                self._on_call(index)
            detections = (
                self._script[index] if index < len(self._script) else tuple()
            )
            return DetectionBatch(detections=tuple(detections), inference_seconds=0.001)
        finally:
            with self._lock:
                self._concurrent -= 1


def person(x: float, y: float, size: float = 20.0, confidence: float = 0.9) -> Detection:
    return Detection(
        x1=x,
        y1=y,
        x2=x + size,
        y2=y + size,
        confidence=confidence,
        coco_class_id=0,
        object_class="person",
        object_category=ObjectCategory.PERSON,
    )


def truck(x: float, y: float, size: float = 30.0, confidence: float = 0.92) -> Detection:
    return Detection(
        x1=x,
        y1=y,
        x2=x + size,
        y2=y + size,
        confidence=confidence,
        coco_class_id=7,
        object_class="truck",
        object_category=ObjectCategory.VEHICLE,
    )


class BlobDetector:
    """Finds the fixture's bright moving square in a genuinely decoded frame.

    This is a deterministic stand-in for YOLO: the synthetic test pattern is not
    a person or a vehicle, so the real model would (correctly) find nothing in
    it. Everything else in the path - GStreamer, the tracker, the crossing
    engine, and event persistence - is exercised for real.
    """

    def __init__(self, *, threshold: int = 200, min_pixels: int = 200):
        self._threshold = threshold
        self._min_pixels = min_pixels
        self._lock = threading.Lock()
        self.calls = 0
        self.max_concurrent = 0
        self._concurrent = 0
        self.busy = False

    @property
    def ready(self) -> bool:
        return True

    def load(self) -> None:
        return None

    def release(self) -> None:
        return None

    def try_detect(self, rgb, confidence, enabled_categories):
        import numpy as np

        if not self._lock.acquire(blocking=False):
            return None
        try:
            with threading.Lock():
                pass
            self._concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self._concurrent)
            self.calls += 1
            mask = rgb[:, :, 0] > self._threshold
            rows, cols = np.nonzero(mask)
            detections: tuple[Detection, ...] = ()
            if rows.size >= self._min_pixels:
                detections = (
                    Detection(
                        x1=float(cols.min()),
                        y1=float(rows.min()),
                        x2=float(cols.max()) + 1.0,
                        y2=float(rows.max()) + 1.0,
                        confidence=0.95,
                        coco_class_id=0,
                        object_class="person",
                        object_category=ObjectCategory.PERSON,
                    ),
                )
            if detections and ObjectCategory.PERSON not in enabled_categories:
                detections = ()
            return DetectionBatch(detections=detections, inference_seconds=0.0)
        finally:
            self._concurrent -= 1
            self._lock.release()
