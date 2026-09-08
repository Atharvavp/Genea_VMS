"""Repositories: the only code that reads or writes Component 5's SQLite store.

Every write is one short transaction. Two rules are enforced here rather than
left to callers:

* discovery is idempotent. An event is keyed by its Component 4 id and a
  representation by ``(event_id, kind, model_id)``, so rediscovering an event -
  through overlap polling, full reconciliation, or a restarted backfill - can
  never create a second vector row or duplicate a result.
* ``index_revision`` moves in the same transaction that adds or removes an
  indexed representation, so a snapshot built from a revision is exactly the set
  of vectors that revision describes.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Iterable, Sequence

import numpy as np

from app.domain.models import (
    Direction,
    EventRecord,
    EventState,
    ObjectCategory,
    ObjectClass,
    RepresentationKind,
    RepresentationState,
    UpstreamState,
    format_utc,
    parse_utc,
    utc_now,
)
from app.embeddings.manifest import (
    EMBEDDING_BYTES,
    EMBEDDING_DIM,
    EMBEDDING_DTYPE,
    MANIFEST,
    NORM_TOLERANCE,
)
from app.persistence.database import Database

__all__ = [
    "EventRepository", "RepresentationRepository", "SyncRepository",
    "SyncState", "IndexedVector", "ClaimedRepresentation", "VectorError",
    "vector_to_blob", "blob_to_vector", "vector_digest",
]

logger = logging.getLogger("semantic.persistence")

#: Transient representation retry ladder, then a permanent 300 s floor.
RETRY_LADDER: Final[tuple[int, ...]] = (2, 4, 8, 16, 30, 60, 120, 300)


class VectorError(ValueError):
    """A vector violates the frozen persistence contract."""


def vector_to_blob(vector: np.ndarray) -> bytes:
    """Serialize one embedding as contiguous little-endian float32 bytes."""
    if vector.ndim != 1 or vector.shape[0] != EMBEDDING_DIM:
        raise VectorError(f"expected shape ({EMBEDDING_DIM},), got {vector.shape}")
    if not np.isfinite(vector).all():
        raise VectorError("vector contains non-finite values")
    little = np.ascontiguousarray(vector, dtype="<f4")
    blob = little.tobytes()
    if len(blob) != EMBEDDING_BYTES:
        raise VectorError(f"serialized {len(blob)} bytes, expected {EMBEDDING_BYTES}")
    return blob


def blob_to_vector(blob: bytes) -> np.ndarray:
    """Deserialize and fully validate one stored embedding."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise VectorError("embedding is not stored as bytes")
    raw = bytes(blob)
    if len(raw) != EMBEDDING_BYTES:
        raise VectorError(f"embedding has {len(raw)} bytes, expected {EMBEDDING_BYTES}")
    vector = np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=True)
    if not np.isfinite(vector).all():
        raise VectorError("embedding contains non-finite values")
    norm = float(np.linalg.norm(vector))
    if abs(norm - 1.0) > NORM_TOLERANCE:
        raise VectorError(f"embedding norm {norm} is outside 1 +/- {NORM_TOLERANCE}")
    return vector


def vector_digest(blob: bytes) -> str:
    """SHA-256 over the exact persisted bytes."""
    return hashlib.sha256(blob).hexdigest()


def _row_to_event(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        event_id=row["event_id"],
        camera_id=row["camera_id"],
        vms_camera_id=row["vms_camera_id"],
        camera_name=row["camera_name"],
        crossed_at=parse_utc(row["crossed_at"]),
        object_category=ObjectCategory(row["object_category"]),
        object_class=ObjectClass(row["object_class"]),
        direction=Direction(row["direction"]),
        discovered_at=parse_utc(row["discovered_at"]),
        source_last_seen_at=parse_utc(row["source_last_seen_at"]),
    )


@dataclass(frozen=True, slots=True)
class IndexedVector:
    event_id: str
    kind: RepresentationKind
    vector: np.ndarray


@dataclass(frozen=True, slots=True)
class ClaimedRepresentation:
    event_id: str
    kind: RepresentationKind
    attempt_count: int


class EventRepository:
    """Mirrored Component 4 event metadata."""

    def __init__(self, database: Database):
        self._db = database

    def upsert_page(self, events: Sequence[EventRecord]) -> tuple[int, int]:
        """Persist one discovered page and its pending representation rows.

        Metadata and both representation rows commit together, so a crash can
        never leave an event that no indexer will ever pick up. Returns
        ``(inserted, refreshed)``.
        """
        if not events:
            return (0, 0)
        now = format_utc(utc_now())
        inserted = 0
        with self._db.transaction() as conn:
            for event in events:
                existing = conn.execute(
                    "SELECT 1 FROM events WHERE event_id = ?", (event.event_id,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO events (event_id, camera_id, vms_camera_id,"
                    " camera_name, crossed_at, object_category, object_class,"
                    " direction, discovered_at, source_last_seen_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(event_id) DO UPDATE SET"
                    "   camera_name = excluded.camera_name,"
                    "   camera_id = excluded.camera_id,"
                    "   vms_camera_id = excluded.vms_camera_id,"
                    "   crossed_at = excluded.crossed_at,"
                    "   object_category = excluded.object_category,"
                    "   object_class = excluded.object_class,"
                    "   direction = excluded.direction,"
                    "   source_last_seen_at = excluded.source_last_seen_at",
                    (
                        event.event_id,
                        event.camera_id,
                        event.vms_camera_id,
                        event.camera_name,
                        format_utc(event.crossed_at),
                        str(event.object_category),
                        str(event.object_class),
                        str(event.direction),
                        format_utc(event.discovered_at),
                        format_utc(event.source_last_seen_at),
                    ),
                )
                if existing is None:
                    inserted += 1
                for kind in (RepresentationKind.CROP, RepresentationKind.FRAME):
                    # The PK includes the model id, so rediscovery never creates
                    # a second vector row and never resets indexed work.
                    conn.execute(
                        "INSERT INTO representations (event_id, kind, model_id,"
                        " state, attempt_count, updated_at)"
                        " VALUES (?, ?, ?, 'pending', 0, ?)"
                        " ON CONFLICT(event_id, kind, model_id) DO NOTHING",
                        (event.event_id, str(kind), MANIFEST.model_id, now),
                    )
        return (inserted, len(events) - inserted)

    def get(self, event_id: str) -> EventRecord | None:
        row = self._db.connect().execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return _row_to_event(row) if row else None

    def exists(self, event_id: str) -> bool:
        return (
            self._db.connect()
            .execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,))
            .fetchone()
            is not None
        )

    def count(self) -> int:
        return int(
            self._db.connect().execute("SELECT COUNT(*) FROM events").fetchone()[0]
        )

    def max_crossed_at(self) -> datetime | None:
        row = self._db.connect().execute(
            "SELECT MAX(crossed_at) FROM events"
        ).fetchone()
        return parse_utc(row[0]) if row and row[0] else None

    def all_for_snapshot(self) -> list[EventRecord]:
        """Every known event, in deterministic id order, for snapshot building."""
        rows = self._db.connect().execute(
            "SELECT * FROM events ORDER BY event_id ASC"
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def representation_states(self, event_id: str) -> dict[str, str]:
        rows = self._db.connect().execute(
            "SELECT kind, state FROM representations"
            " WHERE event_id = ? AND model_id = ?",
            (event_id, MANIFEST.model_id),
        ).fetchall()
        return {row["kind"]: row["state"] for row in rows}

    @staticmethod
    def event_state(states: dict[str, str]) -> EventState:
        """Computed overall state - crop decides searchability (PLAN 6.4)."""
        crop = states.get("crop")
        frame = states.get("frame")
        if crop == RepresentationState.INDEXED:
            if frame == RepresentationState.INDEXED:
                return EventState.COMPLETE
            return EventState.PARTIAL
        if crop == RepresentationState.PERMANENT_ERROR:
            return EventState.FAILED
        return EventState.UNINDEXED


class RepresentationRepository:
    """Vector rows, their state machine, and the index revision."""

    def __init__(self, database: Database):
        self._db = database

    # ---- revision --------------------------------------------------------

    def index_revision(self) -> int:
        row = self._db.connect().execute(
            "SELECT index_revision FROM system_state WHERE singleton = 1"
        ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _bump_revision(conn: sqlite3.Connection, now: str) -> None:
        conn.execute(
            "UPDATE system_state SET index_revision = index_revision + 1,"
            " updated_at = ? WHERE singleton = 1",
            (now,),
        )

    # ---- work selection --------------------------------------------------

    def claim_batch(
        self, *, limit: int, newest_first: bool, now: datetime | None = None
    ) -> list[ClaimedRepresentation]:
        """Claim work for at most ``limit`` events, in one short transaction.

        Selection is by event so crop and frame for the same event travel
        together; the caller embeds crop first. Rows move to ``indexing``, which
        is a durable state: an interrupted process converts them back to
        ``retryable`` at startup rather than losing them.
        """
        moment = now or utc_now()
        stamp = format_utc(moment)
        order = "DESC" if newest_first else "ASC"
        with self._db.transaction() as conn:
            event_rows = conn.execute(
                "SELECT DISTINCT r.event_id, e.crossed_at FROM representations r"
                " JOIN events e ON e.event_id = r.event_id"
                " WHERE r.model_id = ?"
                "   AND (r.state = 'pending'"
                "        OR (r.state = 'retryable'"
                "            AND (r.next_retry_at IS NULL OR r.next_retry_at <= ?)))"
                f" ORDER BY e.crossed_at {order}, r.event_id {order}"
                " LIMIT ?",
                (MANIFEST.model_id, stamp, limit),
            ).fetchall()
            if not event_rows:
                return []
            event_ids = [row["event_id"] for row in event_rows]
            placeholders = ",".join("?" for _ in event_ids)
            claimable = conn.execute(
                f"SELECT event_id, kind, attempt_count FROM representations"
                f" WHERE model_id = ? AND event_id IN ({placeholders})"
                "   AND (state = 'pending'"
                "        OR (state = 'retryable'"
                "            AND (next_retry_at IS NULL OR next_retry_at <= ?)))"
                " ORDER BY event_id ASC, kind ASC",
                (MANIFEST.model_id, *event_ids, stamp),
            ).fetchall()
            claimed = [
                ClaimedRepresentation(
                    event_id=row["event_id"],
                    kind=RepresentationKind(row["kind"]),
                    attempt_count=int(row["attempt_count"]),
                )
                for row in claimable
            ]
            for item in claimed:
                conn.execute(
                    "UPDATE representations SET state = 'indexing', updated_at = ?"
                    " WHERE event_id = ? AND kind = ? AND model_id = ?",
                    (stamp, item.event_id, str(item.kind), MANIFEST.model_id),
                )
        return claimed

    def recover_interrupted(self) -> int:
        """Convert rows left ``indexing`` by an interrupted process."""
        now = format_utc(utc_now())
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE representations SET state = 'retryable',"
                " last_error_code = 'interrupted_indexing', last_error_at = ?,"
                " next_retry_at = NULL, updated_at = ?"
                " WHERE state = 'indexing'",
                (now, now),
            )
            return cursor.rowcount or 0

    # ---- outcomes --------------------------------------------------------

    def mark_indexed(
        self, event_id: str, kind: RepresentationKind, vector: np.ndarray
    ) -> None:
        """Commit one vector and bump the revision in the same transaction."""
        blob = vector_to_blob(vector)
        norm = float(np.linalg.norm(np.frombuffer(blob, dtype="<f4")))
        if abs(norm - 1.0) > NORM_TOLERANCE:
            raise VectorError(f"refusing to store a vector with norm {norm}")
        digest = vector_digest(blob)
        now = format_utc(utc_now())
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE representations SET state = 'indexed', embedding = ?,"
                " dimension = ?, dtype = ?, l2_norm = ?, embedding_sha256 = ?,"
                " next_retry_at = NULL, last_error_code = NULL,"
                " indexed_at = ?, updated_at = ?"
                " WHERE event_id = ? AND kind = ? AND model_id = ?",
                (
                    blob, EMBEDDING_DIM, EMBEDDING_DTYPE, norm, digest,
                    now, now, event_id, str(kind), MANIFEST.model_id,
                ),
            )
            if cursor.rowcount:
                self._bump_revision(conn, now)

    def mark_retryable(
        self, event_id: str, kind: RepresentationKind, error_code: str, *,
        attempt: int, max_retry_seconds: int = 300,
    ) -> datetime:
        """Schedule the next attempt on the frozen ladder."""
        index = min(attempt, len(RETRY_LADDER) - 1)
        delay = min(RETRY_LADDER[index], max_retry_seconds)
        retry_at = utc_now() + timedelta(seconds=delay)
        now = format_utc(utc_now())
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE representations SET state = 'retryable',"
                " attempt_count = attempt_count + 1, next_retry_at = ?,"
                " last_error_code = ?, last_error_at = ?, updated_at = ?"
                " WHERE event_id = ? AND kind = ? AND model_id = ?",
                (
                    format_utc(retry_at), error_code[:64], now, now,
                    event_id, str(kind), MANIFEST.model_id,
                ),
            )
        return retry_at

    def mark_permanent_error(
        self, event_id: str, kind: RepresentationKind, error_code: str
    ) -> None:
        now = format_utc(utc_now())
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "SELECT state FROM representations"
                " WHERE event_id = ? AND kind = ? AND model_id = ?",
                (event_id, str(kind), MANIFEST.model_id),
            ).fetchone()
            conn.execute(
                "UPDATE representations SET state = 'permanent_error',"
                " attempt_count = attempt_count + 1, embedding = NULL,"
                " dimension = NULL, dtype = NULL, l2_norm = NULL,"
                " embedding_sha256 = NULL, indexed_at = NULL,"
                " next_retry_at = NULL, last_error_code = ?, last_error_at = ?,"
                " updated_at = ? WHERE event_id = ? AND kind = ? AND model_id = ?",
                (error_code[:64], now, now, event_id, str(kind), MANIFEST.model_id),
            )
            if cursor is not None and cursor["state"] == RepresentationState.INDEXED:
                self._bump_revision(conn, now)

    def reset_to_pending(
        self, items: Iterable[tuple[str, RepresentationKind]], error_code: str
    ) -> int:
        """Invalidate stored vectors that failed validation, and reschedule."""
        now = format_utc(utc_now())
        changed = 0
        with self._db.transaction() as conn:
            for event_id, kind in items:
                cursor = conn.execute(
                    "UPDATE representations SET state = 'pending', embedding = NULL,"
                    " dimension = NULL, dtype = NULL, l2_norm = NULL,"
                    " embedding_sha256 = NULL, indexed_at = NULL,"
                    " next_retry_at = NULL, last_error_code = ?, last_error_at = ?,"
                    " updated_at = ? WHERE event_id = ? AND kind = ? AND model_id = ?",
                    (error_code[:64], now, now, event_id, str(kind), MANIFEST.model_id),
                )
                changed += cursor.rowcount or 0
            if changed:
                self._bump_revision(conn, now)
        return changed

    def release_claim(self, event_id: str, kind: RepresentationKind) -> None:
        """Return an unprocessed claim to ``pending`` without counting a failure."""
        now = format_utc(utc_now())
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE representations SET state = 'pending', updated_at = ?"
                " WHERE event_id = ? AND kind = ? AND model_id = ? AND state = 'indexing'",
                (now, event_id, str(kind), MANIFEST.model_id),
            )

    # ---- reads -----------------------------------------------------------

    def load_indexed_vectors(self) -> tuple[list[IndexedVector], list[tuple[str, RepresentationKind]]]:
        """Load and validate every indexed vector.

        Returns the accepted vectors in deterministic ``(event_id, kind)`` order
        plus the rows that failed validation, which the caller resets. A corrupt
        row is excluded from search; it never fails the whole load.
        """
        rows = self._db.connect().execute(
            "SELECT event_id, kind, embedding, dimension, dtype, embedding_sha256"
            " FROM representations"
            " WHERE state = 'indexed' AND model_id = ?"
            " ORDER BY event_id ASC, kind ASC",
            (MANIFEST.model_id,),
        ).fetchall()
        accepted: list[IndexedVector] = []
        invalid: list[tuple[str, RepresentationKind]] = []
        for row in rows:
            kind = RepresentationKind(row["kind"])
            try:
                if row["dimension"] != EMBEDDING_DIM:
                    raise VectorError("stored dimension differs from the manifest")
                if row["dtype"] != EMBEDDING_DTYPE:
                    raise VectorError("stored dtype differs from the manifest")
                blob = row["embedding"]
                if blob is None:
                    raise VectorError("indexed row has no embedding")
                raw = bytes(blob)
                if vector_digest(raw) != row["embedding_sha256"]:
                    raise VectorError("stored checksum does not match the bytes")
                accepted.append(IndexedVector(row["event_id"], kind, blob_to_vector(raw)))
            except VectorError as exc:
                logger.warning(
                    "vector_rejected",
                    extra={
                        "event_id": row["event_id"],
                        "kind": str(kind),
                        "reason": type(exc).__name__,
                    },
                )
                invalid.append((row["event_id"], kind))
        return accepted, invalid

    def counts(self) -> dict[str, int]:
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT state, kind, COUNT(*) AS total FROM representations"
            " WHERE model_id = ? GROUP BY state, kind",
            (MANIFEST.model_id,),
        ).fetchall()
        totals: dict[str, int] = {
            "pending": 0, "indexing": 0, "indexed": 0,
            "retryable": 0, "permanent_error": 0,
            "crop_indexed": 0, "frame_indexed": 0,
        }
        for row in rows:
            totals[row["state"]] = totals.get(row["state"], 0) + int(row["total"])
            if row["state"] == "indexed":
                totals[f"{row['kind']}_indexed"] += int(row["total"])
        return totals

    def event_counts(self) -> dict[str, int]:
        """Known/searchable/complete/partial/failed events, computed in SQL."""
        conn = self._db.connect()
        row = conn.execute(
            "SELECT"
            " (SELECT COUNT(*) FROM events) AS known,"
            " (SELECT COUNT(*) FROM representations WHERE model_id = :m"
            "   AND kind = 'crop' AND state = 'indexed') AS searchable,"
            " (SELECT COUNT(*) FROM events e WHERE"
            "   (SELECT COUNT(*) FROM representations r WHERE r.event_id = e.event_id"
            "     AND r.model_id = :m AND r.state = 'indexed') = 2) AS complete,"
            " (SELECT COUNT(*) FROM representations WHERE model_id = :m"
            "   AND kind = 'crop' AND state = 'permanent_error') AS failed",
            {"m": MANIFEST.model_id},
        ).fetchone()
        known, searchable, complete, failed = (int(row[k]) for k in range(4))
        return {
            "known": known,
            "searchable": searchable,
            "complete": complete,
            "partial": searchable - complete,
            "failed": failed,
        }

    def oldest_pending_at(self) -> datetime | None:
        row = self._db.connect().execute(
            "SELECT MIN(e.crossed_at) FROM representations r"
            " JOIN events e ON e.event_id = r.event_id"
            " WHERE r.model_id = ? AND r.state IN ('pending', 'retryable')",
            (MANIFEST.model_id,),
        ).fetchone()
        return parse_utc(row[0]) if row and row[0] else None

    def pending_work_exists(self, now: datetime | None = None) -> bool:
        stamp = format_utc(now or utc_now())
        row = self._db.connect().execute(
            "SELECT 1 FROM representations WHERE model_id = ?"
            " AND (state = 'pending' OR (state = 'retryable'"
            "      AND (next_retry_at IS NULL OR next_retry_at <= ?))) LIMIT 1",
            (MANIFEST.model_id, stamp),
        ).fetchone()
        return row is not None


@dataclass(frozen=True, slots=True)
class SyncState:
    backfill_complete: bool
    backfill_cursor: str | None
    full_reconcile_cursor: str | None
    high_water_crossed_at: datetime | None
    last_poll_attempt_at: datetime | None
    last_successful_poll_at: datetime | None
    last_full_reconcile_at: datetime | None
    upstream_state: UpstreamState
    consecutive_failures: int
    next_poll_at: datetime | None
    last_error_code: str | None


class SyncRepository:
    """Discovery cursors, high water, and safe upstream state."""

    _FIELDS: Final[tuple[str, ...]] = (
        "backfill_complete", "backfill_cursor", "full_reconcile_cursor",
        "high_water_crossed_at", "last_poll_attempt_at", "last_successful_poll_at",
        "last_full_reconcile_at", "upstream_state", "consecutive_failures",
        "next_poll_at", "last_error_code",
    )

    def __init__(self, database: Database):
        self._db = database

    def read(self) -> SyncState:
        row = self._db.connect().execute(
            "SELECT * FROM sync_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return SyncState(
                False, None, None, None, None, None, None,
                UpstreamState.UNKNOWN, 0, None, None,
            )

        def when(key: str) -> datetime | None:
            return parse_utc(row[key]) if row[key] else None

        return SyncState(
            backfill_complete=bool(row["backfill_complete"]),
            backfill_cursor=row["backfill_cursor"],
            full_reconcile_cursor=row["full_reconcile_cursor"],
            high_water_crossed_at=when("high_water_crossed_at"),
            last_poll_attempt_at=when("last_poll_attempt_at"),
            last_successful_poll_at=when("last_successful_poll_at"),
            last_full_reconcile_at=when("last_full_reconcile_at"),
            upstream_state=UpstreamState(row["upstream_state"]),
            consecutive_failures=int(row["consecutive_failures"]),
            next_poll_at=when("next_poll_at"),
            last_error_code=row["last_error_code"],
        )

    def update(self, **fields: Any) -> None:
        """Patch named sync fields in one short transaction."""
        unknown = set(fields) - set(self._FIELDS)
        if unknown:
            raise KeyError(f"unknown sync_state fields: {sorted(unknown)}")
        if not fields:
            return
        values: list[Any] = []
        assignments: list[str] = []
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            if isinstance(value, datetime):
                values.append(format_utc(value))
            elif isinstance(value, bool):
                values.append(1 if value else 0)
            elif isinstance(value, UpstreamState):
                values.append(str(value))
            else:
                values.append(value)
        values.append(format_utc(utc_now()))
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE sync_state SET {', '.join(assignments)}, updated_at = ?"
                " WHERE singleton = 1",
                values,
            )

    def advance_high_water(self, candidate: datetime) -> None:
        """Move the high-water mark forward only; never backwards."""
        current = self.read().high_water_crossed_at
        if current is None or candidate > current:
            self.update(high_water_crossed_at=candidate)
