"""Synchronous singleton-line get/upsert/delete.

``line_configs.camera_id`` is UNIQUE, so a camera has at most one line and the
database - not application convention - enforces it.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from app.domain.models import LineDirection, LineRecord, to_iso_ms
from app.persistence.camera_repository import _timestamp
from app.persistence.database import Database, DataIntegrityError

__all__ = ["LineRepository"]

_COLUMNS: Final[str] = (
    "id, camera_id, name, x1, y1, x2, y2, direction, enabled, created_at, updated_at"
)


def map_line(row: sqlite3.Row) -> LineRecord:
    raw_direction = str(row["direction"])
    try:
        direction = LineDirection(raw_direction)
    except ValueError as exc:
        raise DataIntegrityError(
            "stored line direction is not a supported value"
        ) from exc
    enabled = row["enabled"]
    if enabled not in (0, 1):
        raise DataIntegrityError("column enabled holds a non-boolean value")
    return LineRecord(
        id=str(row["id"]),
        camera_id=str(row["camera_id"]),
        name=str(row["name"]),
        x1=float(row["x1"]),
        y1=float(row["y1"]),
        x2=float(row["x2"]),
        y2=float(row["y2"]),
        direction=direction,
        enabled=bool(enabled),
        created_at=_timestamp(row["created_at"], "created_at"),
        updated_at=_timestamp(row["updated_at"], "updated_at"),
    )


class LineRepository:
    def __init__(self, database: Database):
        self._db = database

    def get(self, camera_id: str) -> LineRecord | None:
        with self._db.read() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM line_configs WHERE camera_id = ?",
                (camera_id,),
            ).fetchone()
        return map_line(row) if row is not None else None

    def list_all(self) -> dict[str, LineRecord]:
        with self._db.read() as connection:
            rows = connection.execute(f"SELECT {_COLUMNS} FROM line_configs").fetchall()
        return {str(row["camera_id"]): map_line(row) for row in rows}

    def upsert(self, record: LineRecord) -> LineRecord:
        """Insert or replace the camera's single line, preserving its identity.

        The caller supplies the id and ``created_at`` to preserve; this method
        never invents a second row for the same camera.
        """
        with self._db.write() as connection:
            existing = connection.execute(
                "SELECT id, created_at FROM line_configs WHERE camera_id = ?",
                (record.camera_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO line_configs (id, camera_id, name, x1, y1, x2, y2, "
                    "direction, enabled, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.id,
                        record.camera_id,
                        record.name,
                        record.x1,
                        record.y1,
                        record.x2,
                        record.y2,
                        record.direction.value,
                        int(record.enabled),
                        to_iso_ms(record.created_at),
                        to_iso_ms(record.updated_at),
                    ),
                )
                return record
            cursor = connection.execute(
                "UPDATE line_configs SET name = ?, x1 = ?, y1 = ?, x2 = ?, y2 = ?, "
                "direction = ?, enabled = ?, updated_at = ? WHERE camera_id = ?",
                (
                    record.name,
                    record.x1,
                    record.y1,
                    record.x2,
                    record.y2,
                    record.direction.value,
                    int(record.enabled),
                    to_iso_ms(record.updated_at),
                    record.camera_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DataIntegrityError(
                    f"line update affected {cursor.rowcount} rows, expected 1"
                )
            preserved_id = str(existing["id"])
            preserved_created = _timestamp(existing["created_at"], "created_at")
        return LineRecord(
            id=preserved_id,
            camera_id=record.camera_id,
            name=record.name,
            x1=record.x1,
            y1=record.y1,
            x2=record.x2,
            y2=record.y2,
            direction=record.direction,
            enabled=record.enabled,
            created_at=preserved_created,
            updated_at=record.updated_at,
        )

    def delete(self, camera_id: str) -> bool:
        """Idempotent: returns whether a row was removed."""
        with self._db.write() as connection:
            cursor = connection.execute(
                "DELETE FROM line_configs WHERE camera_id = ?", (camera_id,)
            )
            return cursor.rowcount > 0
