"""Synchronous event insert, get, filter, cursor seek, and dedupe lookup.

Event rows are denormalized on purpose: they must remain displayable after the
camera or line they came from has been deleted, so this module performs no
joins and the schema declares no foreign key from ``events``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Sequence

from app.domain.models import (
    CrossingDirection,
    EventRecord,
    ObjectCategory,
    to_iso_ms,
)
from app.persistence.camera_repository import _timestamp
from app.persistence.database import Database, DataIntegrityError

__all__ = ["EventRepository", "EventFilter", "EventPage", "MAX_EVENT_PAGE_SIZE"]

MAX_EVENT_PAGE_SIZE: Final[int] = 100

_COLUMNS: Final[str] = (
    "id, camera_id, vms_camera_id, camera_name, line_id, line_name, "
    "worker_session_id, track_id, object_category, object_class, direction, "
    "confidence, crossed_at, bbox_x1, bbox_y1, bbox_x2, bbox_y2, centroid_x, "
    "centroid_y, frame_width, frame_height, source_pts_ns, snapshot_path, "
    "crop_path, created_at"
)


def map_event(row: sqlite3.Row) -> EventRecord:
    try:
        category = ObjectCategory(str(row["object_category"]))
        direction = CrossingDirection(str(row["direction"]))
    except ValueError as exc:
        raise DataIntegrityError("stored event holds an unknown enum value") from exc
    source_pts = row["source_pts_ns"]
    return EventRecord(
        id=str(row["id"]),
        camera_id=str(row["camera_id"]),
        vms_camera_id=str(row["vms_camera_id"]),
        camera_name=str(row["camera_name"]),
        line_id=str(row["line_id"]),
        line_name=str(row["line_name"]),
        worker_session_id=str(row["worker_session_id"]),
        track_id=int(row["track_id"]),
        object_category=category,
        object_class=str(row["object_class"]),
        direction=direction,
        confidence=float(row["confidence"]),
        crossed_at=_timestamp(row["crossed_at"], "crossed_at"),
        bbox_x1=float(row["bbox_x1"]),
        bbox_y1=float(row["bbox_y1"]),
        bbox_x2=float(row["bbox_x2"]),
        bbox_y2=float(row["bbox_y2"]),
        centroid_x=float(row["centroid_x"]),
        centroid_y=float(row["centroid_y"]),
        frame_width=int(row["frame_width"]),
        frame_height=int(row["frame_height"]),
        source_pts_ns=int(source_pts) if source_pts is not None else None,
        snapshot_path=str(row["snapshot_path"]),
        crop_path=str(row["crop_path"]),
        created_at=_timestamp(row["created_at"], "created_at"),
    )


@dataclass(frozen=True, slots=True)
class EventFilter:
    camera_id: str | None = None
    object_category: ObjectCategory | None = None
    object_class: str | None = None
    direction: CrossingDirection | None = None
    start: datetime | None = None
    end: datetime | None = None

    def identity(self) -> str:
        """Stable text used to bind a page cursor to the filters that made it."""
        return "|".join(
            (
                self.camera_id or "",
                self.object_category.value if self.object_category else "",
                self.object_class or "",
                self.direction.value if self.direction else "",
                to_iso_ms(self.start) if self.start else "",
                to_iso_ms(self.end) if self.end else "",
            )
        )


@dataclass(frozen=True, slots=True)
class EventPage:
    items: list[EventRecord]
    has_more: bool


class EventRepository:
    def __init__(self, database: Database):
        self._db = database

    # -- writes -------------------------------------------------------------

    def insert(self, record: EventRecord) -> bool:
        """Insert one event. Returns ``False`` on a unique-key conflict.

        The conflict is not an error: it is the database proving that this
        exact crossing was already recorded for this session.
        """
        try:
            with self._db.write() as connection:
                connection.execute(
                    "INSERT INTO events (id, camera_id, vms_camera_id, camera_name, "
                    "line_id, line_name, worker_session_id, track_id, object_category, "
                    "object_class, direction, confidence, crossed_at, bbox_x1, bbox_y1, "
                    "bbox_x2, bbox_y2, centroid_x, centroid_y, frame_width, "
                    "frame_height, source_pts_ns, snapshot_path, crop_path, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?)",
                    (
                        record.id,
                        record.camera_id,
                        record.vms_camera_id,
                        record.camera_name,
                        record.line_id,
                        record.line_name,
                        record.worker_session_id,
                        record.track_id,
                        record.object_category.value,
                        record.object_class,
                        record.direction.value,
                        record.confidence,
                        to_iso_ms(record.crossed_at),
                        record.bbox_x1,
                        record.bbox_y1,
                        record.bbox_x2,
                        record.bbox_y2,
                        record.centroid_x,
                        record.centroid_y,
                        record.frame_width,
                        record.frame_height,
                        record.source_pts_ns,
                        record.snapshot_path,
                        record.crop_path,
                        to_iso_ms(record.created_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                return False
            raise
        return True

    # -- reads --------------------------------------------------------------

    def get(self, event_id: str) -> EventRecord | None:
        with self._db.read() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        return map_event(row) if row is not None else None

    def find_by_dedupe_key(
        self,
        camera_id: str,
        line_id: str,
        worker_session_id: str,
        track_id: int,
        direction: CrossingDirection,
    ) -> EventRecord | None:
        with self._db.read() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM events WHERE camera_id = ? AND line_id = ? "
                "AND worker_session_id = ? AND track_id = ? AND direction = ?",
                (camera_id, line_id, worker_session_id, track_id, direction.value),
            ).fetchone()
        return map_event(row) if row is not None else None

    def query(
        self,
        filters: EventFilter,
        *,
        limit: int,
        cursor_crossed_at: datetime | None = None,
        cursor_id: str | None = None,
    ) -> EventPage:
        """Newest-first page using the ``(crossed_at DESC, id DESC)`` seek."""
        if not (1 <= limit <= MAX_EVENT_PAGE_SIZE):
            raise ValueError(f"limit must be between 1 and {MAX_EVENT_PAGE_SIZE}")
        clauses: list[str] = []
        params: list[object] = []
        if filters.camera_id:
            clauses.append("camera_id = ?")
            params.append(filters.camera_id)
        if filters.object_category:
            clauses.append("object_category = ?")
            params.append(filters.object_category.value)
        if filters.object_class:
            clauses.append("object_class = ?")
            params.append(filters.object_class)
        if filters.direction:
            clauses.append("direction = ?")
            params.append(filters.direction.value)
        if filters.start is not None:
            clauses.append("crossed_at >= ?")
            params.append(to_iso_ms(filters.start))
        if filters.end is not None:
            clauses.append("crossed_at < ?")
            params.append(to_iso_ms(filters.end))
        if cursor_crossed_at is not None and cursor_id is not None:
            clauses.append("(crossed_at < ? OR (crossed_at = ? AND id < ?))")
            marker = to_iso_ms(cursor_crossed_at)
            params.extend([marker, marker, cursor_id])

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT {_COLUMNS} FROM events{where} "
            "ORDER BY crossed_at DESC, id DESC LIMIT ?"
        )
        params.append(limit + 1)
        with self._db.read() as connection:
            rows: Sequence[sqlite3.Row] = connection.execute(sql, params).fetchall()
        records = [map_event(row) for row in rows[:limit]]
        return EventPage(items=records, has_more=len(rows) > limit)

    def count(self) -> int:
        with self._db.read() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def all_event_ids(self) -> set[str]:
        """Used by startup reconciliation to identify orphan directories."""
        with self._db.read() as connection:
            return {str(row[0]) for row in connection.execute("SELECT id FROM events")}

    def iter_paths(self):
        """(event_id, snapshot_path, crop_path) for the read-only verifier."""
        with self._db.read() as connection:
            for row in connection.execute(
                "SELECT id, snapshot_path, crop_path FROM events ORDER BY id"
            ):
                yield str(row[0]), str(row[1]), str(row[2])
