"""Deterministic builders for events, vectors, and Component 4 payloads."""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from app.domain.models import (
    Direction,
    EventRecord,
    ObjectCategory,
    ObjectClass,
    format_utc,
    utc_now,
)

BASE_TIME = datetime(2026, 9, 7, 9, 0, 0, tzinfo=timezone.utc)

_CLASS_CATEGORY = {
    ObjectClass.PERSON: ObjectCategory.PERSON,
    ObjectClass.BICYCLE: ObjectCategory.VEHICLE,
    ObjectClass.CAR: ObjectCategory.VEHICLE,
    ObjectClass.MOTORCYCLE: ObjectCategory.VEHICLE,
    ObjectClass.BUS: ObjectCategory.VEHICLE,
    ObjectClass.TRUCK: ObjectCategory.VEHICLE,
}


def event_id(seed: int | str) -> str:
    return "evt_" + hashlib.sha256(f"event-{seed}".encode()).hexdigest()[:32]


def camera_id(seed: int | str) -> str:
    return "acam_" + hashlib.sha256(f"camera-{seed}".encode()).hexdigest()[:8]


def vms_camera_id(seed: int | str) -> str:
    return "cam_" + hashlib.sha256(f"vms-{seed}".encode()).hexdigest()[:8]


def make_event(
    seed: int | str = 1,
    *,
    crossed_at: datetime | None = None,
    camera: int | str = 1,
    camera_name: str = "Loading Bay",
    object_class: ObjectClass = ObjectClass.CAR,
    direction: Direction = Direction.A_TO_B,
    discovered_at: datetime | None = None,
) -> EventRecord:
    moment = crossed_at or (BASE_TIME + timedelta(seconds=int(str(seed).__hash__() % 1000)))
    now = discovered_at or utc_now()
    return EventRecord(
        event_id=event_id(seed),
        camera_id=camera_id(camera),
        vms_camera_id=vms_camera_id(camera),
        camera_name=camera_name,
        crossed_at=moment,
        object_category=_CLASS_CATEGORY[object_class],
        object_class=object_class,
        direction=direction,
        discovered_at=now,
        source_last_seen_at=now,
    )


def make_events(count: int, *, start: datetime = BASE_TIME, step_seconds: int = 60) -> list[EventRecord]:
    classes = list(ObjectClass)
    return [
        make_event(
            index,
            crossed_at=start + timedelta(seconds=index * step_seconds),
            camera=index % 4,
            camera_name=f"Camera {index % 4}",
            object_class=classes[index % len(classes)],
            direction=Direction.A_TO_B if index % 2 == 0 else Direction.B_TO_A,
        )
        for index in range(count)
    ]


def unit_vector(seed: int, dimension: int = 768) -> np.ndarray:
    rng = np.random.default_rng(seed)
    raw = rng.standard_normal(dimension).astype(np.float32)
    return np.ascontiguousarray(raw / np.linalg.norm(raw), dtype=np.float32)


def event_payload(record: EventRecord, **overrides: Any) -> dict[str, Any]:
    """A Component 4 `GET /api/events` item for this record."""
    payload: dict[str, Any] = {
        "id": record.event_id,
        "camera_id": record.camera_id,
        "vms_camera_id": record.vms_camera_id,
        "camera_name": record.camera_name,
        "line_id": "line_0123abcd",
        "line_name": "Entry line",
        "worker_session_id": "ws_" + "0" * 32,
        "track_id": 7,
        "object_category": str(record.object_category),
        "object_class": str(record.object_class),
        "direction": str(record.direction),
        "confidence": 0.91,
        "crossed_at": format_utc(record.crossed_at),
        "bbox": {"x1": 0.1, "y1": 0.2, "x2": 0.3, "y2": 0.6},
        "centroid": {"x": 0.2, "y": 0.4},
        "frame_width": 1920,
        "frame_height": 1080,
        "frame_url": f"/api/events/{record.event_id}/frame",
        "crop_url": f"/api/events/{record.event_id}/crop",
    }
    payload.update(overrides)
    return payload


def jpeg_bytes(width: int = 64, height: int = 48, colour: tuple[int, int, int] = (30, 90, 160)) -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def png_bytes(width: int = 64, height: int = 48) -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 40, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeEmbeddingRuntime:
    """A deterministic stand-in for SigLIP used by the integration tier.

    It is a real embedding function, not a mock: an image's vector is derived
    from its mean colour and a text's vector from the colour word it contains,
    both projected through the same fixed basis. Cross-modal search therefore
    genuinely works, so the integration tier can assert retrieval behaviour
    without loading an 800 MB checkpoint. The real model is exercised by the
    `real_model` and `real_component4` tiers.
    """

    DIMENSION = 768
    COLOURS = {
        "red": (220, 30, 30),
        "green": (30, 200, 60),
        "blue": (30, 60, 220),
        "yellow": (230, 220, 40),
        "grey": (128, 128, 128),
    }

    def __init__(self, *, dimension: int = DIMENSION, fail_for: set[str] | None = None):
        self.dimension = dimension
        self.ready = True
        self.calls: list[tuple[str, int]] = []
        self.fail_for = fail_for or set()
        self._unavailable: str | None = None
        rng = np.random.default_rng(20260907)
        self._basis = rng.standard_normal((3, dimension)).astype(np.float32)

    # -- identity ------------------------------------------------------
    @property
    def model_id(self) -> str:
        from app.embeddings.manifest import MANIFEST

        return MANIFEST.model_id

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable

    def mark_unavailable(self, reason: str) -> None:
        self._unavailable = reason
        self.ready = False

    def clear_unavailable(self) -> None:
        self._unavailable = None
        self.ready = True

    def load(self) -> None:
        # A runtime marked unavailable fails to load, exactly as the real one
        # does when its manifest or checkpoint cannot be verified.
        if self._unavailable:
            raise RuntimeError(self._unavailable)
        self.ready = True

    def release(self) -> None:
        self.ready = False

    # -- inference -----------------------------------------------------
    def _project(self, rgb) -> np.ndarray:
        colour = np.asarray(rgb, dtype=np.float32) / 255.0
        vector = colour @ self._basis
        norm = float(np.linalg.norm(vector))
        return np.ascontiguousarray(vector / norm, dtype=np.float32)

    def embed_images(self, images) -> np.ndarray:
        self.calls.append(("images", len(images)))
        if self._unavailable:
            raise RuntimeError(self._unavailable)
        if not images:
            return np.zeros((0, self.dimension), dtype=np.float32)
        rows = []
        for image in images:
            small = image.convert("RGB").resize((8, 8))
            mean = np.asarray(small, dtype=np.float32).reshape(-1, 3).mean(axis=0)
            if tuple(int(v) for v in mean) in self.fail_for:
                raise ValueError("fake embedding failure")
            rows.append(self._project(mean))
        return np.vstack(rows).astype(np.float32)

    def embed_texts(self, texts) -> np.ndarray:
        self.calls.append(("texts", len(texts)))
        if self._unavailable:
            raise RuntimeError(self._unavailable)
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        rows = []
        for text in texts:
            lowered = text.lower()
            rgb = next(
                (value for name, value in self.COLOURS.items() if name in lowered),
                (128, 128, 128),
            )
            rows.append(self._project(rgb))
        return np.vstack(rows).astype(np.float32)
