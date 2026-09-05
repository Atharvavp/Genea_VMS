"""CRUD over the `cameras` table.

Synchronous by design: every call is dispatched to a worker thread by
``CameraManager`` (``asyncio.to_thread``), so a connection is opened, used and
closed on the same thread and the event loop never blocks on disk I/O.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.domain.models import CameraRecord
from app.persistence.database import connect, initialize_database


class DuplicatePath(Exception):
    """A camera already owns this MediaMTX path."""


def _row_to_record(row: sqlite3.Row) -> CameraRecord:
    return CameraRecord(
        id=row["id"],
        name=row["name"],
        rtsp_url=row["rtsp_url"],
        mediamtx_path=row["mediamtx_path"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class CameraRepository:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        initialize_database(self.db_path)

    # --- reads ----------------------------------------------------------

    def list(self) -> list[CameraRecord]:
        with connect(self.db_path) as connection:
            # rowid breaks any remaining timestamp tie with true insert order.
            rows = connection.execute(
                "SELECT * FROM cameras ORDER BY created_at, rowid"
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get(self, camera_id: str) -> CameraRecord | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM cameras WHERE id = ?", (camera_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def count(self) -> tuple[int, int]:
        """(total, enabled)."""
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(enabled), 0) AS enabled FROM cameras"
            ).fetchone()
        return int(row["total"]), int(row["enabled"])

    # --- writes ---------------------------------------------------------

    def insert(self, record: CameraRecord) -> CameraRecord:
        try:
            with connect(self.db_path) as connection:
                connection.execute(
                    "INSERT INTO cameras "
                    "(id, name, rtsp_url, mediamtx_path, enabled, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.id,
                        record.name,
                        record.rtsp_url,
                        record.mediamtx_path,
                        int(record.enabled),
                        record.created_at,
                        record.updated_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicatePath(record.mediamtx_path) from exc
        return record

    def update(self, record: CameraRecord) -> CameraRecord:
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                "UPDATE cameras SET name = ?, rtsp_url = ?, enabled = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    record.name,
                    record.rtsp_url,
                    int(record.enabled),
                    record.updated_at,
                    record.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(record.id)
        return record

    def delete(self, camera_id: str) -> bool:
        with connect(self.db_path) as connection:
            cursor = connection.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
            return cursor.rowcount > 0
