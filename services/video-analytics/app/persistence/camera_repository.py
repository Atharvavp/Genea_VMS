"""Synchronous camera CRUD and row mapping.

Every method owns one short-lived connection. Async callers dispatch these
through ``asyncio.to_thread``; worker threads call them directly.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Final, Sequence

from app.domain.models import (
    CameraRecord,
    ObjectCategory,
    parse_iso_utc,
    to_iso_ms,
)
from app.persistence.database import Database, DataIntegrityError

__all__ = ["CameraRepository", "DuplicateVmsCameraId"]

_COLUMNS: Final[str] = (
    "id, vms_camera_id, name, rtsp_url, enabled, inference_fps, "
    "confidence_threshold, enabled_classes_json, created_at, updated_at"
)

_ALLOWED_CLASS_JSON: Final[frozenset[str]] = frozenset(
    {'["person"]', '["vehicle"]', '["person","vehicle"]'}
)


class DuplicateVmsCameraId(ValueError):
    """A second analytics camera would consume the same VMS camera."""


def encode_classes(categories: frozenset[ObjectCategory]) -> str:
    """Serialise categories in the one canonical, sorted form the CHECK allows."""
    values = sorted(category.value for category in categories)
    if not values:
        raise DataIntegrityError("enabled_classes must not be empty")
    encoded = json.dumps(values, separators=(",", ":"))
    if encoded not in _ALLOWED_CLASS_JSON:
        raise DataIntegrityError(f"unsupported enabled_classes value: {values}")
    return encoded


def decode_classes(value: str) -> frozenset[ObjectCategory]:
    """Strictly decode stored categories; never substitute a default."""
    if value not in _ALLOWED_CLASS_JSON:
        raise DataIntegrityError("stored enabled_classes_json is not a supported value")
    try:
        items = json.loads(value)
    except json.JSONDecodeError as exc:
        raise DataIntegrityError("stored enabled_classes_json is not valid JSON") from exc
    if not isinstance(items, list) or not items:
        raise DataIntegrityError("stored enabled_classes_json is not a non-empty list")
    try:
        return frozenset(ObjectCategory(item) for item in items)
    except ValueError as exc:
        raise DataIntegrityError("stored enabled_classes_json holds an unknown category") from exc


def _bool(value: object, column: str) -> bool:
    if value in (0, 1):
        return bool(value)
    raise DataIntegrityError(f"column {column} holds a non-boolean value")


def _timestamp(value: object, column: str) -> datetime:
    try:
        return parse_iso_utc(str(value))
    except Exception as exc:  # noqa: BLE001
        raise DataIntegrityError(f"column {column} holds an unparsable timestamp") from exc


def map_camera(row: sqlite3.Row) -> CameraRecord:
    return CameraRecord(
        id=str(row["id"]),
        vms_camera_id=str(row["vms_camera_id"]),
        name=str(row["name"]),
        rtsp_url=str(row["rtsp_url"]),
        enabled=_bool(row["enabled"], "enabled"),
        inference_fps=float(row["inference_fps"]),
        confidence_threshold=float(row["confidence_threshold"]),
        enabled_classes=decode_classes(str(row["enabled_classes_json"])),
        created_at=_timestamp(row["created_at"], "created_at"),
        updated_at=_timestamp(row["updated_at"], "updated_at"),
    )


class CameraRepository:
    def __init__(self, database: Database):
        self._db = database

    # -- reads --------------------------------------------------------------

    def list(self) -> list[CameraRecord]:
        """Deterministic order: case-folded name, then id."""
        with self._db.read() as connection:
            rows: Sequence[sqlite3.Row] = connection.execute(
                f"SELECT {_COLUMNS} FROM analytics_cameras"
            ).fetchall()
        records = [map_camera(row) for row in rows]
        records.sort(key=lambda record: (record.name.casefold(), record.id))
        return records

    def get(self, camera_id: str) -> CameraRecord | None:
        with self._db.read() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM analytics_cameras WHERE id = ?",
                (camera_id,),
            ).fetchone()
        return map_camera(row) if row is not None else None

    def get_by_vms_camera_id(self, vms_camera_id: str) -> CameraRecord | None:
        with self._db.read() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM analytics_cameras WHERE vms_camera_id = ?",
                (vms_camera_id,),
            ).fetchone()
        return map_camera(row) if row is not None else None

    # -- writes -------------------------------------------------------------

    def insert(self, record: CameraRecord) -> CameraRecord:
        try:
            with self._db.write() as connection:
                connection.execute(
                    "INSERT INTO analytics_cameras (id, vms_camera_id, name, rtsp_url, "
                    "enabled, inference_fps, confidence_threshold, enabled_classes_json, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.id,
                        record.vms_camera_id,
                        record.name,
                        record.rtsp_url,
                        int(record.enabled),
                        record.inference_fps,
                        record.confidence_threshold,
                        encode_classes(record.enabled_classes),
                        to_iso_ms(record.created_at),
                        to_iso_ms(record.updated_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "vms_camera_id" in str(exc):
                raise DuplicateVmsCameraId(record.vms_camera_id) from None
            raise
        return record

    def update(self, record: CameraRecord) -> CameraRecord:
        try:
            with self._db.write() as connection:
                cursor = connection.execute(
                    "UPDATE analytics_cameras SET vms_camera_id = ?, name = ?, "
                    "rtsp_url = ?, enabled = ?, inference_fps = ?, "
                    "confidence_threshold = ?, enabled_classes_json = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        record.vms_camera_id,
                        record.name,
                        record.rtsp_url,
                        int(record.enabled),
                        record.inference_fps,
                        record.confidence_threshold,
                        encode_classes(record.enabled_classes),
                        to_iso_ms(record.updated_at),
                        record.id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DataIntegrityError(
                        f"camera update affected {cursor.rowcount} rows, expected 1"
                    )
        except sqlite3.IntegrityError as exc:
            if "vms_camera_id" in str(exc):
                raise DuplicateVmsCameraId(record.vms_camera_id) from None
            raise
        return record

    def delete(self, camera_id: str) -> None:
        """Delete exactly one camera row. Event rows are never touched here."""
        with self._db.write() as connection:
            cursor = connection.execute(
                "DELETE FROM analytics_cameras WHERE id = ?", (camera_id,)
            )
            if cursor.rowcount != 1:
                raise DataIntegrityError(
                    f"camera delete affected {cursor.rowcount} rows, expected 1"
                )

    def exists(self, camera_id: str) -> bool:
        with self._db.read() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM analytics_cameras WHERE id = ?", (camera_id,)
                ).fetchone()
                is not None
            )
