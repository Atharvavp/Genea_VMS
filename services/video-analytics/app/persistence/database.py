"""SQLite schema version 1, connection factory, and migration.

Connections are never shared between threads. Every repository method opens one
short-lived connection on the thread that will use it and closes it again.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Final, Iterator

__all__ = [
    "Database",
    "DatabaseError",
    "SchemaIncompatibleError",
    "DataIntegrityError",
    "SCHEMA_VERSION",
    "SCHEMA_STATEMENTS",
    "REQUIRED_TABLES",
    "REQUIRED_INDEXES",
]

logger = logging.getLogger("analytics.database")

SCHEMA_VERSION: Final[int] = 1

REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "analytics_cameras",
    "line_configs",
    "events",
)

REQUIRED_INDEXES: Final[tuple[str, ...]] = (
    "idx_events_crossed_at_id",
    "idx_events_camera_crossed_at_id",
    "idx_events_category_crossed_at_id",
    "idx_events_class_crossed_at_id",
)

_HEX8 = "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"

SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE analytics_cameras (
        id                   TEXT PRIMARY KEY
                             CHECK (id GLOB 'acam_{_HEX8}'),
        vms_camera_id        TEXT NOT NULL UNIQUE
                             CHECK (length(vms_camera_id) = 12
                                    AND substr(vms_camera_id, 1, 4) = 'cam_'
                                    AND substr(vms_camera_id, 5) NOT GLOB '*[^0-9a-f]*'),
        name                 TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 100),
        rtsp_url             TEXT NOT NULL CHECK (length(rtsp_url) BETWEEN 1 AND 2048),
        enabled              INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        inference_fps        REAL NOT NULL DEFAULT 5.0
                             CHECK (inference_fps >= 1.0 AND inference_fps <= 10.0),
        confidence_threshold REAL NOT NULL DEFAULT 0.25
                             CHECK (confidence_threshold >= 0.10
                                    AND confidence_threshold <= 0.95),
        enabled_classes_json TEXT NOT NULL DEFAULT '["person","vehicle"]'
                             CHECK (enabled_classes_json IN
                                    ('["person"]', '["vehicle"]', '["person","vehicle"]')),
        created_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE line_configs (
        id          TEXT PRIMARY KEY
                    CHECK (length(id) = 13 AND substr(id, 1, 5) = 'line_'
                           AND substr(id, 6) NOT GLOB '*[^0-9a-f]*'),
        camera_id   TEXT NOT NULL UNIQUE,
        name        TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 100),
        x1          REAL NOT NULL CHECK (x1 >= 0.0 AND x1 <= 1.0),
        y1          REAL NOT NULL CHECK (y1 >= 0.0 AND y1 <= 1.0),
        x2          REAL NOT NULL CHECK (x2 >= 0.0 AND x2 <= 1.0),
        y2          REAL NOT NULL CHECK (y2 >= 0.0 AND y2 <= 1.0),
        direction   TEXT NOT NULL CHECK (direction IN ('A_TO_B', 'B_TO_A', 'BOTH')),
        enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        CHECK (((x2 - x1) * (x2 - x1) + (y2 - y1) * (y2 - y1)) >= 0.0025),
        FOREIGN KEY (camera_id) REFERENCES analytics_cameras(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE events (
        id                 TEXT PRIMARY KEY
                           CHECK (length(id) = 36 AND substr(id, 1, 4) = 'evt_'
                                  AND substr(id, 5) NOT GLOB '*[^0-9a-f]*'),
        camera_id          TEXT NOT NULL
                           CHECK (length(camera_id) = 13
                                  AND substr(camera_id, 1, 5) = 'acam_'
                                  AND substr(camera_id, 6) NOT GLOB '*[^0-9a-f]*'),
        vms_camera_id      TEXT NOT NULL
                           CHECK (length(vms_camera_id) = 12
                                  AND substr(vms_camera_id, 1, 4) = 'cam_'
                                  AND substr(vms_camera_id, 5) NOT GLOB '*[^0-9a-f]*'),
        camera_name        TEXT NOT NULL,
        line_id            TEXT NOT NULL
                           CHECK (length(line_id) = 13 AND substr(line_id, 1, 5) = 'line_'
                                  AND substr(line_id, 6) NOT GLOB '*[^0-9a-f]*'),
        line_name          TEXT NOT NULL,
        worker_session_id  TEXT NOT NULL
                           CHECK (length(worker_session_id) = 35
                                  AND substr(worker_session_id, 1, 3) = 'ws_'
                                  AND substr(worker_session_id, 4) NOT GLOB '*[^0-9a-f]*'),
        track_id           INTEGER NOT NULL CHECK (track_id >= 0),
        object_category    TEXT NOT NULL CHECK (object_category IN ('person', 'vehicle')),
        object_class       TEXT NOT NULL
                           CHECK (object_class IN ('person', 'bicycle', 'car',
                                                   'motorcycle', 'bus', 'truck')),
        direction          TEXT NOT NULL CHECK (direction IN ('A_TO_B', 'B_TO_A')),
        confidence         REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
        crossed_at         TEXT NOT NULL,
        bbox_x1            REAL NOT NULL CHECK (bbox_x1 >= 0.0 AND bbox_x1 <= 1.0),
        bbox_y1            REAL NOT NULL CHECK (bbox_y1 >= 0.0 AND bbox_y1 <= 1.0),
        bbox_x2            REAL NOT NULL CHECK (bbox_x2 >= 0.0 AND bbox_x2 <= 1.0),
        bbox_y2            REAL NOT NULL CHECK (bbox_y2 >= 0.0 AND bbox_y2 <= 1.0),
        centroid_x         REAL NOT NULL CHECK (centroid_x >= 0.0 AND centroid_x <= 1.0),
        centroid_y         REAL NOT NULL CHECK (centroid_y >= 0.0 AND centroid_y <= 1.0),
        frame_width        INTEGER NOT NULL CHECK (frame_width > 1),
        frame_height       INTEGER NOT NULL CHECK (frame_height > 1),
        source_pts_ns      INTEGER,
        snapshot_path      TEXT NOT NULL,
        crop_path          TEXT NOT NULL,
        created_at         TEXT NOT NULL,
        CHECK (bbox_x1 < bbox_x2),
        CHECK (bbox_y1 < bbox_y2),
        UNIQUE (camera_id, line_id, worker_session_id, track_id, direction)
    )
    """,
    "CREATE INDEX idx_events_crossed_at_id ON events (crossed_at DESC, id DESC)",
    "CREATE INDEX idx_events_camera_crossed_at_id "
    "ON events (camera_id, crossed_at DESC, id DESC)",
    "CREATE INDEX idx_events_category_crossed_at_id "
    "ON events (object_category, crossed_at DESC, id DESC)",
    "CREATE INDEX idx_events_class_crossed_at_id "
    "ON events (object_class, crossed_at DESC, id DESC)",
)


class DatabaseError(RuntimeError):
    """Base class for analytics persistence failures."""


class SchemaIncompatibleError(DatabaseError):
    """The on-disk schema cannot be used by this build."""


class DataIntegrityError(DatabaseError):
    """A persisted row could not be mapped back to a domain value."""


class Database:
    """Connection factory and schema owner for the analytics SQLite file."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 10_000):
        self._path = Path(path)
        self._busy_timeout_ms = int(busy_timeout_ms)

    @property
    def path(self) -> Path:
        return self._path

    def ensure_parent(self) -> None:
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            parent.chmod(0o750)
        except PermissionError:  # pragma: no cover - pre-created bind mount
            logger.debug("database_parent_chmod_skipped")

    def connect(self) -> sqlite3.Connection:
        """Open one connection with the settings this application requires.

        ``isolation_level=None`` disables the driver's implicit transaction
        handling: every write path issues its own ``BEGIN IMMEDIATE``.
        """
        connection = sqlite3.connect(
            str(self._path),
            timeout=self._busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            self._assert_pragmas(connection)
        except Exception:
            connection.close()
            raise
        return connection

    def _assert_pragmas(self, connection: sqlite3.Connection) -> None:
        checks = {
            "foreign_keys": 1,
            "synchronous": 1,
            "busy_timeout": self._busy_timeout_ms,
        }
        for pragma, expected in checks.items():
            actual = connection.execute(f"PRAGMA {pragma}").fetchone()[0]
            if int(actual) != int(expected):
                raise DatabaseError(
                    f"PRAGMA {pragma} is {actual}, expected {expected}"
                )
        journal = str(
            connection.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
        if journal != "wal":
            raise DatabaseError(f"PRAGMA journal_mode is {journal}, expected wal")

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One logical mutation inside ``BEGIN IMMEDIATE`` / ``COMMIT``."""
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    # -- schema -------------------------------------------------------------

    def initialize(self) -> int:
        """Apply or validate schema version 1.

        Returns the version in force. A future version is fatal: this build
        performs no destructive downgrade.
        """
        self.ensure_parent()
        connection = self.connect()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise SchemaIncompatibleError(
                    f"database schema version {version} is newer than the "
                    f"supported version {SCHEMA_VERSION}"
                )
            if version == SCHEMA_VERSION:
                self._validate_schema(connection)
                return version
            if version != 0:
                raise SchemaIncompatibleError(
                    f"database schema version {version} cannot be upgraded"
                )
            self._apply_schema(connection)
            self._validate_schema(connection)
            logger.info("schema_created", extra={"schema_version": SCHEMA_VERSION})
            return SCHEMA_VERSION
        finally:
            connection.close()

    def _apply_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                )
            }
            if any(name in existing for name in REQUIRED_TABLES):
                raise SchemaIncompatibleError(
                    "database already contains analytics tables but reports "
                    "user_version 0; refusing to guess its state"
                )
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        missing_tables = [name for name in REQUIRED_TABLES if name not in names]
        missing_indexes = [name for name in REQUIRED_INDEXES if name not in names]
        if missing_tables or missing_indexes:
            raise SchemaIncompatibleError(
                "schema version 1 is incomplete; missing "
                f"tables={missing_tables} indexes={missing_indexes}"
            )

    def check_readable(self) -> None:
        """Cheap liveness probe used by ``/health``."""
        with self.read() as connection:
            connection.execute("SELECT 1 FROM analytics_cameras LIMIT 1").fetchone()
