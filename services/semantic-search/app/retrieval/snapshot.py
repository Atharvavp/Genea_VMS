"""The immutable in-memory search index and the atomic reference that holds it.

A snapshot is built off-lock from validated SQLite rows, frozen, and then
swapped in under one short lock. A search takes one reference and reads only
that: it performs no database I/O, takes no write lock, and cannot observe a
half-built index. Publishing a new snapshot never mutates the old one, so an
in-flight search keeps a consistent view even while indexing continues.

Only events whose CROP is indexed are included - crop is what makes an event
searchable. The frame vector is optional, and its absence is expressed as a
``-1`` row index rather than a missing entry, so every array stays aligned.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Iterable, Mapping, Sequence

import numpy as np

from app.domain.models import EventRecord, RepresentationKind, format_utc
from app.embeddings.manifest import EMBEDDING_DIM, MANIFEST
from app.persistence.repositories import IndexedVector

__all__ = ["SearchSnapshot", "SnapshotStore", "build_snapshot"]

logger = logging.getLogger("semantic.retrieval")

_MISSING: Final[int] = -1


def _readonly(array: np.ndarray) -> np.ndarray:
    array = np.ascontiguousarray(array)
    array.setflags(write=False)
    return array


@dataclass(frozen=True, slots=True)
class SearchSnapshot:
    """One complete, immutable view of the searchable corpus."""

    index_revision: int
    model_id: str
    built_at: datetime

    #: Event identity and metadata, all aligned on the same event index, which
    #: is also the event's rank in ascending id order (the deterministic
    #: tie-break the ranker relies on).
    event_ids: tuple[str, ...]
    camera_ids: np.ndarray
    camera_names: tuple[str, ...]
    vms_camera_ids: tuple[str, ...]
    object_categories: np.ndarray
    object_classes: np.ndarray
    directions: np.ndarray
    crossed_at_ms: np.ndarray
    crossed_at_text: tuple[str, ...]

    #: Vector storage. ``crop_rows[i]`` indexes ``crop_matrix``; a frame row of
    #: -1 means this event has no frame vector.
    crop_matrix: np.ndarray
    frame_matrix: np.ndarray
    crop_rows: np.ndarray
    frame_rows: np.ndarray

    @property
    def event_count(self) -> int:
        return len(self.event_ids)

    @property
    def is_empty(self) -> bool:
        return self.event_count == 0

    def index_of(self, event_id: str) -> int:
        try:
            return self.event_ids.index(event_id)
        except ValueError:
            return _MISSING


def _empty(revision: int, built_at: datetime) -> SearchSnapshot:
    return SearchSnapshot(
        index_revision=revision,
        model_id=MANIFEST.model_id,
        built_at=built_at,
        event_ids=(),
        camera_ids=_readonly(np.empty(0, dtype=object)),
        camera_names=(),
        vms_camera_ids=(),
        object_categories=_readonly(np.empty(0, dtype=object)),
        object_classes=_readonly(np.empty(0, dtype=object)),
        directions=_readonly(np.empty(0, dtype=object)),
        crossed_at_ms=_readonly(np.empty(0, dtype=np.int64)),
        crossed_at_text=(),
        crop_matrix=_readonly(np.empty((0, EMBEDDING_DIM), dtype=np.float32)),
        frame_matrix=_readonly(np.empty((0, EMBEDDING_DIM), dtype=np.float32)),
        crop_rows=_readonly(np.empty(0, dtype=np.int32)),
        frame_rows=_readonly(np.empty(0, dtype=np.int32)),
    )


def build_snapshot(
    *,
    events: Iterable[EventRecord],
    vectors: Sequence[IndexedVector],
    index_revision: int,
    built_at: datetime,
) -> SearchSnapshot:
    """Build one snapshot from validated rows. Deterministic and total.

    Vectors are placed in ``(event_id, kind)`` order so two builds of the same
    revision produce byte-identical matrices. Any vector whose event is unknown,
    or whose shape is wrong, is skipped rather than corrupting the matrix.
    """
    by_id: Mapping[str, EventRecord] = {event.event_id: event for event in events}

    crop_vectors: dict[str, np.ndarray] = {}
    frame_vectors: dict[str, np.ndarray] = {}
    for item in vectors:
        if item.event_id not in by_id:
            continue
        if item.vector.shape != (EMBEDDING_DIM,):
            logger.warning(
                "snapshot_vector_skipped",
                extra={"event_id": item.event_id, "kind": str(item.kind)},
            )
            continue
        target = (
            crop_vectors if item.kind is RepresentationKind.CROP else frame_vectors
        )
        target[item.event_id] = item.vector

    # Crop is what makes an event searchable.
    searchable = sorted(crop_vectors)
    if not searchable:
        return _empty(index_revision, built_at)

    crop_rows = np.full(len(searchable), _MISSING, dtype=np.int32)
    frame_rows = np.full(len(searchable), _MISSING, dtype=np.int32)
    crop_stack: list[np.ndarray] = []
    frame_stack: list[np.ndarray] = []

    for position, event_id in enumerate(searchable):
        crop_rows[position] = len(crop_stack)
        crop_stack.append(crop_vectors[event_id])
        frame = frame_vectors.get(event_id)
        if frame is not None:
            frame_rows[position] = len(frame_stack)
            frame_stack.append(frame)

    records = [by_id[event_id] for event_id in searchable]
    frame_matrix = (
        np.vstack(frame_stack).astype(np.float32, copy=False)
        if frame_stack
        else np.empty((0, EMBEDDING_DIM), dtype=np.float32)
    )
    return SearchSnapshot(
        index_revision=index_revision,
        model_id=MANIFEST.model_id,
        built_at=built_at,
        event_ids=tuple(searchable),
        camera_ids=_readonly(np.array([r.camera_id for r in records], dtype=object)),
        camera_names=tuple(r.camera_name for r in records),
        vms_camera_ids=tuple(r.vms_camera_id for r in records),
        object_categories=_readonly(
            np.array([str(r.object_category) for r in records], dtype=object)
        ),
        object_classes=_readonly(
            np.array([str(r.object_class) for r in records], dtype=object)
        ),
        directions=_readonly(np.array([str(r.direction) for r in records], dtype=object)),
        crossed_at_ms=_readonly(
            np.array(
                [int(r.crossed_at.timestamp() * 1000) for r in records], dtype=np.int64
            )
        ),
        crossed_at_text=tuple(format_utc(r.crossed_at) for r in records),
        crop_matrix=_readonly(np.vstack(crop_stack).astype(np.float32, copy=False)),
        frame_matrix=_readonly(frame_matrix),
        crop_rows=_readonly(crop_rows),
        frame_rows=_readonly(frame_rows),
    )


class SnapshotStore:
    """Holds the one active snapshot and exchanges it atomically."""

    def __init__(self, initial: SearchSnapshot | None = None):
        self._lock = threading.Lock()
        self._snapshot = initial if initial is not None else _empty(0, _epoch())

    def current(self) -> SearchSnapshot:
        """Take a reference. The returned object is never mutated in place."""
        with self._lock:
            return self._snapshot

    def publish(self, snapshot: SearchSnapshot) -> SearchSnapshot:
        """Swap in a fully built replacement. The lock is held for one rebind."""
        with self._lock:
            previous = self._snapshot
            self._snapshot = snapshot
        logger.info(
            "snapshot_published",
            extra={
                "index_revision": snapshot.index_revision,
                "events": snapshot.event_count,
                "crop_vectors": int(snapshot.crop_matrix.shape[0]),
                "frame_vectors": int(snapshot.frame_matrix.shape[0]),
                "previous_revision": previous.index_revision,
            },
        )
        return previous


def _epoch() -> datetime:
    from datetime import timezone

    return datetime(1970, 1, 1, tzinfo=timezone.utc)
