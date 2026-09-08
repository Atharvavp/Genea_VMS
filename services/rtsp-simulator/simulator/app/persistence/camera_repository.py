"""CRUD for the ``cameras`` table."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from app.api.errors import DuplicateStreamPath
from app.domain.models import (
    CameraRecord,
    CameraStatus,
    SourceMetadata,
    VideoConfig,
)
from app.persistence.database import connect, initialize_database

_COLUMNS = (
    "id, name, stream_path, source_kind, source_path, source_original_filename, "
    "source_stored_filename, source_metadata_json, codec, resolution_json, fps_json, "
    "bitrate_json, loop, auto_start, status, last_error, created_at, updated_at"
)


class CameraRepository:
    """Synchronous repository; callers dispatch it to a worker thread."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        initialize_database(self.db_path)

    # --- reads ----------------------------------------------------------

    def list_cameras(self) -> list[CameraRecord]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM cameras ORDER BY created_at ASC"
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get(self, camera_id: str) -> CameraRecord | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM cameras WHERE id = ?", (camera_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_stream_path(self, stream_path: str) -> CameraRecord | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM cameras WHERE stream_path = ?", (stream_path,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def stream_path_exists(self, stream_path: str, exclude_id: str | None = None) -> bool:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT id FROM cameras WHERE stream_path = ? AND id != ?",
                (stream_path, exclude_id or ""),
            ).fetchone()
        return row is not None

    # --- writes ---------------------------------------------------------

    def insert(self, record: CameraRecord) -> CameraRecord:
        values = _record_to_values(record)
        placeholders = ", ".join("?" for _ in values)
        try:
            with connect(self.db_path) as connection:
                connection.execute(
                    f"INSERT INTO cameras ({_COLUMNS}) VALUES ({placeholders})", values
                )
        except sqlite3.IntegrityError as exc:
            raise _integrity_error(record, exc) from exc
        return record

    def update(self, record: CameraRecord) -> CameraRecord:
        values = _record_to_values(record)
        assignments = ", ".join(
            f"{column.strip()} = ?" for column in _COLUMNS.split(",") if column.strip() != "id"
        )
        # `id` is the first column in _COLUMNS; move it to the end for the WHERE clause.
        try:
            with connect(self.db_path) as connection:
                connection.execute(
                    f"UPDATE cameras SET {assignments} WHERE id = ?",
                    (*values[1:], record.id),
                )
        except sqlite3.IntegrityError as exc:
            raise _integrity_error(record, exc) from exc
        return record

    def update_status(
        self, camera_id: str, status: CameraStatus, last_error: str | None, updated_at: datetime
    ) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                "UPDATE cameras SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status.value, last_error, updated_at.isoformat(), camera_id),
            )

    def delete(self, camera_id: str) -> bool:
        with connect(self.db_path) as connection:
            cursor = connection.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
        return cursor.rowcount > 0


def _integrity_error(record: CameraRecord, exc: sqlite3.IntegrityError) -> Exception:
    if "stream_path" in str(exc):
        return DuplicateStreamPath(record.stream_path)
    return exc


def _record_to_values(record: CameraRecord) -> tuple:
    return (
        record.id,
        record.name,
        record.stream_path,
        record.source_kind.value,
        record.source_path,
        record.source_original_filename,
        record.source_stored_filename,
        record.source_metadata.model_dump_json(),
        record.video.codec.value,
        record.video.resolution.model_dump_json(),
        record.video.fps.model_dump_json(),
        record.video.bitrate.model_dump_json(),
        int(record.loop),
        int(record.auto_start),
        record.status.value,
        record.last_error,
        record.created_at.isoformat(),
        record.updated_at.isoformat(),
    )


def _row_to_record(row: sqlite3.Row) -> CameraRecord:
    video = VideoConfig.model_validate(
        {
            "codec": row["codec"],
            "resolution": json.loads(row["resolution_json"]),
            "fps": json.loads(row["fps_json"]),
            "bitrate": json.loads(row["bitrate_json"]),
        }
    )
    return CameraRecord(
        id=row["id"],
        name=row["name"],
        stream_path=row["stream_path"],
        source_kind=row["source_kind"],
        source_path=row["source_path"],
        source_original_filename=row["source_original_filename"],
        source_stored_filename=row["source_stored_filename"],
        source_metadata=SourceMetadata.model_validate_json(row["source_metadata_json"]),
        video=video,
        loop=bool(row["loop"]),
        auto_start=bool(row["auto_start"]),
        status=CameraStatus(row["status"]),
        last_error=row["last_error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
