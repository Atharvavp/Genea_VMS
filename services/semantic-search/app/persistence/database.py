"""SQLite ownership: connection pragmas, schema, integrity, and process lock.

Component 5 owns exactly one database, on its own volume. Two invariants matter:

* one process owns a volume. ``ProcessLock`` takes a non-blocking ``flock`` on
  ``/data/semantic.lock``; a second Uvicorn worker or a concurrent reindex fails
  loudly rather than indexing the same store (PLAN section 10.1).
* an incompatible store is never opened for search. Schema version and the
  recorded model identity are checked at startup and a mismatch requires an
  explicit offline reindex, so vectors from two spaces cannot mix.
"""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterator

from app.domain.models import format_utc, utc_now
from app.embeddings.manifest import MANIFEST

__all__ = [
    "Database", "ProcessLock", "ProcessLockError", "SchemaError",
    "ModelIdentityMismatch", "DatabaseCorrupt", "SCHEMA_VERSION",
    "quarantine_database",
]

logger = logging.getLogger("semantic.persistence")

SCHEMA_VERSION: Final[int] = 1
_SCHEMA_PATH: Final[Path] = Path(__file__).parent / "schema.sql"
_BUSY_TIMEOUT_MS: Final[int] = 10_000


class SchemaError(RuntimeError):
    """The store's schema version is not the one this build understands."""


class ModelIdentityMismatch(RuntimeError):
    """Stored vectors belong to a different model/preprocessor/dtype/dimension."""

    def __init__(self, stored: dict[str, Any], expected: dict[str, Any]):
        super().__init__(
            "stored vectors were produced by a different embedding contract"
        )
        self.stored = stored
        self.expected = expected


class DatabaseCorrupt(RuntimeError):
    """``PRAGMA quick_check`` failed; the file must be quarantined."""


class ProcessLockError(RuntimeError):
    """Another process already owns this data directory."""


class ProcessLock:
    """Exclusive, non-blocking ownership of one Component 5 data directory."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise ProcessLockError(
                "another process already owns this data directory"
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class SystemState:
    schema_version: int
    model_id: str
    model_revision: str
    model_sha256: str
    preprocessor_id: str
    embedding_dim: int
    embedding_dtype: str
    index_revision: int


class Database:
    """One SQLite file, one write lock, short transactions.

    Connections are per-thread (``sqlite3`` objects are not shareable), and every
    write goes through ``transaction()`` which serializes on a single in-process
    lock. Reads run concurrently under WAL.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def ensure_parent(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ---- connections -----------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Return this thread's connection, configured on first use."""
        connection: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if connection is not None:
            return connection
        connection = sqlite3.connect(
            str(self._path), timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA trusted_schema=OFF")
        cursor.close()
        self._local.conn = connection
        return connection

    def close(self) -> None:
        connection: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if connection is not None:
            connection.close()
            self._local.conn = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One short, serialized write transaction."""
        connection = self.connect()
        with self._write_lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    # ---- integrity and schema -------------------------------------------

    def quick_check(self) -> None:
        connection = self.connect()
        row = connection.execute("PRAGMA quick_check").fetchone()
        result = row[0] if row else "unknown"
        if str(result).lower() != "ok":
            raise DatabaseCorrupt(f"quick_check reported: {result}")

    def check_readable(self) -> None:
        self.connect().execute("SELECT 1").fetchone()

    def initialize(self) -> SystemState:
        """Create or validate the store. Returns the recorded system state.

        A fresh store is stamped with the current manifest. An existing store is
        validated against it: a different schema version or embedding contract
        raises rather than silently mixing vector spaces.
        """
        connection = self.connect()
        connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        now = format_utc(utc_now())
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT schema_version, model_id, model_revision, model_sha256,"
                " preprocessor_id, embedding_dim, embedding_dtype, index_revision"
                " FROM system_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO system_state (singleton, schema_version, model_id,"
                    " model_revision, model_sha256, preprocessor_id, embedding_dim,"
                    " embedding_dtype, index_revision, created_at, updated_at)"
                    " VALUES (1, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        SCHEMA_VERSION,
                        MANIFEST.model_id,
                        MANIFEST.revision,
                        MANIFEST.weights_sha256,
                        MANIFEST.preprocessor_id,
                        MANIFEST.dimension,
                        MANIFEST.dtype,
                        now,
                        now,
                    ),
                )
                state = SystemState(
                    SCHEMA_VERSION,
                    MANIFEST.model_id,
                    MANIFEST.revision,
                    MANIFEST.weights_sha256,
                    MANIFEST.preprocessor_id,
                    MANIFEST.dimension,
                    MANIFEST.dtype,
                    0,
                )
            else:
                state = SystemState(*row)
            conn.execute(
                "INSERT INTO sync_state (singleton, backfill_complete,"
                " upstream_state, consecutive_failures, updated_at)"
                " VALUES (1, 0, 'unknown', 0, ?)"
                " ON CONFLICT(singleton) DO NOTHING",
                (now,),
            )

        if state.schema_version != SCHEMA_VERSION:
            raise SchemaError(
                f"store schema version {state.schema_version} != {SCHEMA_VERSION}"
            )
        expected = {
            "model_id": MANIFEST.model_id,
            "model_revision": MANIFEST.revision,
            "model_sha256": MANIFEST.weights_sha256,
            "preprocessor_id": MANIFEST.preprocessor_id,
            "embedding_dim": MANIFEST.dimension,
            "embedding_dtype": MANIFEST.dtype,
        }
        stored = {
            "model_id": state.model_id,
            "model_revision": state.model_revision,
            "model_sha256": state.model_sha256,
            "preprocessor_id": state.preprocessor_id,
            "embedding_dim": state.embedding_dim,
            "embedding_dtype": state.embedding_dtype,
        }
        if stored != expected:
            raise ModelIdentityMismatch(stored, expected)
        return state


def quarantine_database(db_path: Path, quarantine_root: Path) -> Path:
    """Move the DB and its sidecars aside, atomically, keeping every byte.

    A quarantined copy is never deleted automatically: it is the only remaining
    evidence of whatever corrupted the store.
    """
    stamp = utc_now().strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"
    destination = quarantine_root / stamp
    destination.mkdir(parents=True, exist_ok=False)
    moved: list[str] = []
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(db_path) + suffix)
        if source.exists():
            shutil.move(str(source), str(destination / source.name))
            moved.append(source.name)
    logger.warning(
        "database_quarantined",
        extra={"quarantine": str(destination), "files": ",".join(moved) or "-"},
    )
    return destination
