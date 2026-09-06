"""SQLite connection handling and schema initialisation.

A fresh connection is opened per operation and closed immediately, so a
connection is never shared between threads. Repository calls run on a worker
thread (see ``CameraManager``), which keeps the event loop free.

The schema holds desired state only. Camera health is derived from MediaMTX on
every poll and is deliberately not persisted: a stored health value would be
stale the moment the process stops, and would then have to be un-trusted at
startup anyway.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    rtsp_url      TEXT NOT NULL,
    mediamtx_path TEXT NOT NULL,
    enabled       INTEGER NOT NULL,
    recording_enabled INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_cameras_mediamtx_path ON cameras (mediamtx_path);
CREATE INDEX IF NOT EXISTS ix_cameras_enabled ON cameras (enabled);
"""


@contextmanager
def connect(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection with sane concurrency defaults."""
    connection = sqlite3.connect(str(db_path), timeout=15.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=10000")
        with connection:  # commits on success, rolls back on exception
            yield connection
    finally:
        connection.close()


def upgrade_schema(connection: sqlite3.Connection) -> None:
    """Bring a Component 2 database up to the current schema, in place.

    There is no migration framework here, and adding one for a single column
    would be heavier than the change it manages. This is the whole upgrade: it
    inspects what exists and adds only what is missing, so it is safe to run on
    every start and on a database that is already current.

    Existing rows default to `recording_enabled = 0`, so upgrading never starts
    recording a camera that was not asked to be recorded.
    """
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(cameras)")
    }
    if not columns:  # fresh database: SCHEMA already created it correctly
        return
    if "recording_enabled" not in columns:
        connection.execute(
            "ALTER TABLE cameras "
            "ADD COLUMN recording_enabled INTEGER NOT NULL DEFAULT 0"
        )
        logger.info("schema_upgraded added=recording_enabled")


def initialize_database(db_path: Path | str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as connection:
        connection.executescript(SCHEMA)
        upgrade_schema(connection)
