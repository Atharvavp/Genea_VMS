"""SQLite connection handling and schema initialisation.

A fresh connection is opened per operation and closed immediately, so a
connection is never shared between threads. All repository calls are executed
on a worker thread (see ``CameraManager``), which keeps the event loop free.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id                       TEXT PRIMARY KEY,
    name                     TEXT NOT NULL,
    stream_path              TEXT NOT NULL,
    source_kind              TEXT NOT NULL,
    source_path              TEXT NOT NULL,
    source_original_filename TEXT NOT NULL,
    source_stored_filename   TEXT,
    source_metadata_json     TEXT NOT NULL,
    codec                    TEXT NOT NULL,
    resolution_json          TEXT NOT NULL,
    fps_json                 TEXT NOT NULL,
    bitrate_json             TEXT NOT NULL,
    loop                     INTEGER NOT NULL,
    auto_start               INTEGER NOT NULL,
    status                   TEXT NOT NULL,
    last_error               TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_cameras_stream_path ON cameras (stream_path);
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


def initialize_database(db_path: Path | str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as connection:
        connection.executescript(SCHEMA)
