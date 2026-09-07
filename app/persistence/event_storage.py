"""Atomic event artifact + metadata persistence.

Ordering is the whole point of this module:

    encode -> hidden temp dir -> write+fsync files -> fsync temp dir
           -> atomic rename -> fsync day parent -> INSERT row

A committed database row therefore never intentionally points at a partially
written image. The brief window it does admit - a final directory that exists
before its row is committed - is closed by :meth:`reconcile_startup`.
"""

from __future__ import annotations

import io
import logging
import math
import os
import secrets
import shutil
import stat as stat_module
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Final

from app.analytics.types import CommitOutcome, CommitResult, EventCandidate
from app.domain.models import (
    CAMERA_ID_RE,
    EVENT_ID_RE,
    EventRecord,
    new_event_id,
    utc_now,
)
from app.persistence.event_repository import EventRepository
from app.security.event_paths import (
    DAY_RE,
    TEMP_DIR_RE,
    UnsafeEventPath,
    event_directory_relative,
    event_image_relative,
    resolve_within,
    temp_directory_name,
)

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "EventStorage",
    "EventStorageError",
    "ReconcileReport",
    "MIN_CROP_PIXELS",
]

logger = logging.getLogger("analytics.event_storage")

MIN_CROP_PIXELS: Final[int] = 2


class EventStorageError(RuntimeError):
    """A durable artifact could not be written; the event was not recorded."""


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    temp_directories_removed: int
    orphan_directories_removed: int
    rows_with_missing_artifacts: int
    unknown_paths: int

    @property
    def clean(self) -> bool:
        return (
            self.temp_directories_removed == 0
            and self.orphan_directories_removed == 0
            and self.rows_with_missing_artifacts == 0
            and self.unknown_paths == 0
        )


def clip_crop_box(
    x1: float, y1: float, x2: float, y2: float, width: int, height: int
) -> tuple[int, int, int, int] | None:
    """Integer crop bounds, or ``None`` when the crop would be degenerate.

    ``right``/``bottom`` are exclusive. A crop narrower or shorter than
    :data:`MIN_CROP_PIXELS` is not an event.
    """
    for value in (x1, y1, x2, y2):
        if not math.isfinite(value):
            return None
    left = min(max(int(math.floor(x1)), 0), width - 1)
    top = min(max(int(math.floor(y1)), 0), height - 1)
    right = min(max(int(math.ceil(x2)), left + 1), width)
    bottom = min(max(int(math.ceil(y2)), top + 1), height)
    if (right - left) < MIN_CROP_PIXELS or (bottom - top) < MIN_CROP_PIXELS:
        return None
    return left, top, right, bottom


def _fsync_directory(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class EventStorage:
    """Owns everything under the event root and the event insert transaction."""

    def __init__(
        self,
        *,
        data_root: Path,
        events_root: Path,
        repository: EventRepository,
        jpeg_quality: int = 90,
        id_factory: Callable[[], str] = new_event_id,
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(8),
        clock: Callable[[], object] = utc_now,
    ):
        self._data_root = Path(data_root)
        self._events_root = Path(events_root)
        if not self._events_root.is_relative_to(self._data_root):
            raise ValueError("events root must live beneath the data root")
        self._events_root_name = self._events_root.relative_to(
            self._data_root
        ).as_posix()
        if "/" in self._events_root_name:
            raise ValueError("events root must be a single segment below the data root")
        self._repository = repository
        self._jpeg_quality = int(jpeg_quality)
        self._new_id = id_factory
        self._new_nonce = nonce_factory
        self._clock = clock

    # -- filesystem preparation ---------------------------------------------

    def ensure_roots(self) -> None:
        self._data_root.mkdir(parents=True, exist_ok=True)
        self._events_root.mkdir(parents=True, exist_ok=True)
        for path in (self._data_root, self._events_root):
            try:
                path.chmod(0o750)
            except PermissionError:  # pragma: no cover - pre-created bind mount
                logger.debug("event_root_chmod_skipped", extra={"path_kind": "root"})
        probe = self._data_root / ".writable-probe"
        try:
            probe.write_bytes(b"")
        finally:
            probe.unlink(missing_ok=True)

    # -- encoding -----------------------------------------------------------

    def _encode_jpeg(self, rgb: "NDArray[np.uint8]") -> bytes:
        from PIL import Image

        image = Image.fromarray(rgb)
        buffer = io.BytesIO()
        image.save(
            buffer,
            format="JPEG",
            quality=self._jpeg_quality,
            optimize=True,
            exif=b"",
        )
        payload = buffer.getvalue()
        if not payload:
            raise EventStorageError("JPEG encoder produced an empty byte sequence")
        return payload

    # -- the commit ---------------------------------------------------------

    def commit_event(self, candidate: EventCandidate) -> CommitResult:
        """Persist one crossing atomically.

        Returns ``INSERTED`` when this call created the row, ``ALREADY_EXISTS``
        when the unique key proves the crossing is already recorded, and
        ``INVALID_CROP`` when the bounding box cannot produce a usable crop
        (a dropped detection, not a storage failure).
        """
        existing = self._repository.find_by_dedupe_key(
            candidate.camera_id,
            candidate.line_id,
            candidate.worker_session_id,
            candidate.track_id,
            candidate.direction,
        )
        if existing is not None:
            return CommitResult(
                outcome=CommitOutcome.ALREADY_EXISTS,
                event_id=existing.id,
                crossed_at=existing.crossed_at,
            )

        box = clip_crop_box(
            *candidate.bbox_pixels, candidate.frame_width, candidate.frame_height
        )
        if box is None:
            return CommitResult(
                outcome=CommitOutcome.INVALID_CROP, event_id=None, crossed_at=None
            )
        left, top, right, bottom = box

        rgb = candidate.rgb
        if rgb is None:
            raise EventStorageError("event candidate carries no frame")
        frame_bytes = self._encode_jpeg(rgb)
        crop_bytes = self._encode_jpeg(rgb[top:bottom, left:right].copy())
        # Both payloads must be non-empty before anything touches the disk.
        if not frame_bytes or not crop_bytes:
            raise EventStorageError("an encoded event image was empty")

        event_id = self._new_id()
        if not EVENT_ID_RE.fullmatch(event_id):
            raise EventStorageError("generated event id is malformed")
        day = candidate.crossed_at.astimezone(UTC).date()

        relative_dir = event_directory_relative(
            self._events_root_name, candidate.camera_id, day, event_id
        )
        final_dir = resolve_within(self._data_root, relative_dir)
        day_dir = final_dir.parent
        if final_dir.exists():
            raise EventStorageError("generated event directory already exists")

        temp_name = temp_directory_name(event_id, self._new_nonce())
        temp_dir = day_dir / temp_name

        day_dir.mkdir(parents=True, exist_ok=True)
        for parent in (day_dir, day_dir.parent):
            try:
                parent.chmod(0o750)
            except PermissionError:  # pragma: no cover
                pass

        renamed = False
        try:
            temp_dir.mkdir(mode=0o700)
            self._write_file(temp_dir / "frame.jpg", frame_bytes)
            self._write_file(temp_dir / "crop.jpg", crop_bytes)
            _fsync_directory(temp_dir)
            os.rename(temp_dir, final_dir)
            renamed = True
            _fsync_directory(day_dir)
        except EventStorageError:
            self._cleanup(temp_dir if not renamed else final_dir, day_dir)
            raise
        except OSError as exc:
            self._cleanup(temp_dir if not renamed else final_dir, day_dir)
            raise EventStorageError(
                f"event artifacts could not be finalised: {type(exc).__name__}"
            ) from exc

        record = EventRecord(
            id=event_id,
            camera_id=candidate.camera_id,
            vms_camera_id=candidate.vms_camera_id,
            camera_name=candidate.camera_name,
            line_id=candidate.line_id,
            line_name=candidate.line_name,
            worker_session_id=candidate.worker_session_id,
            track_id=candidate.track_id,
            object_category=candidate.object_category,
            object_class=candidate.object_class,
            direction=candidate.direction,
            confidence=float(candidate.confidence),
            crossed_at=candidate.crossed_at,
            bbox_x1=left / candidate.frame_width,
            bbox_y1=top / candidate.frame_height,
            bbox_x2=right / candidate.frame_width,
            bbox_y2=bottom / candidate.frame_height,
            centroid_x=candidate.centroid_normalized[0],
            centroid_y=candidate.centroid_normalized[1],
            frame_width=candidate.frame_width,
            frame_height=candidate.frame_height,
            source_pts_ns=candidate.source_pts_ns,
            snapshot_path=event_image_relative(
                self._events_root_name, candidate.camera_id, day, event_id, "frame"
            ),
            crop_path=event_image_relative(
                self._events_root_name, candidate.camera_id, day, event_id, "crop"
            ),
            created_at=self._clock(),  # type: ignore[arg-type]
        )

        try:
            inserted = self._repository.insert(record)
        except Exception as exc:  # noqa: BLE001
            self._cleanup(final_dir, day_dir)
            raise EventStorageError(
                f"event row could not be committed: {type(exc).__name__}"
            ) from exc

        if not inserted:
            # Another writer won the same unique key between the lookup and the
            # insert. Remove this loser's directory and report the existing row.
            self._cleanup(final_dir, day_dir)
            winner = self._repository.find_by_dedupe_key(
                candidate.camera_id,
                candidate.line_id,
                candidate.worker_session_id,
                candidate.track_id,
                candidate.direction,
            )
            if winner is None:  # pragma: no cover - defensive
                raise EventStorageError(
                    "unique conflict reported but no existing event was found"
                )
            return CommitResult(
                outcome=CommitOutcome.ALREADY_EXISTS,
                event_id=winner.id,
                crossed_at=winner.crossed_at,
            )

        return CommitResult(
            outcome=CommitOutcome.INSERTED,
            event_id=record.id,
            crossed_at=record.crossed_at,
        )

    def _write_file(self, path: Path, payload: bytes) -> None:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            written = os.write(fd, payload)
            if written != len(payload):
                raise EventStorageError("short write while persisting an event image")
            os.fsync(fd)
        finally:
            os.close(fd)

    def _cleanup(self, directory: Path, parent: Path) -> None:
        """Remove one generated directory; never touch anything else."""
        try:
            if directory.exists():
                shutil.rmtree(directory)
                if parent.exists():
                    _fsync_directory(parent)
        except OSError:
            logger.warning(
                "event_artifact_orphan_left",
                extra={"reason": "cleanup_failed"},
            )

    # -- startup reconciliation ---------------------------------------------

    def reconcile_startup(self) -> ReconcileReport:
        """Remove abandoned temp/orphan directories before any worker starts.

        A committed row is never deleted because its image is missing: the row
        is the durable record, and the image endpoint reports 410 instead.
        """
        temp_removed = 0
        orphans_removed = 0
        unknown = 0
        self.ensure_roots()
        known_ids = self._repository.all_event_ids()

        for camera_dir in sorted(self._events_root.iterdir()):
            if not self._is_plain_directory(camera_dir):
                unknown += 1
                continue
            if not CAMERA_ID_RE.fullmatch(camera_dir.name):
                unknown += 1
                logger.warning("event_store_unknown_path", extra={"depth": "camera"})
                continue
            for day_dir in sorted(camera_dir.iterdir()):
                if not self._is_plain_directory(day_dir):
                    unknown += 1
                    continue
                if not DAY_RE.fullmatch(day_dir.name):
                    unknown += 1
                    logger.warning("event_store_unknown_path", extra={"depth": "day"})
                    continue
                for entry in sorted(day_dir.iterdir()):
                    if not self._is_plain_directory(entry):
                        unknown += 1
                        continue
                    name = entry.name
                    if TEMP_DIR_RE.fullmatch(name):
                        shutil.rmtree(entry, ignore_errors=True)
                        temp_removed += 1
                        continue
                    if not EVENT_ID_RE.fullmatch(name):
                        unknown += 1
                        logger.warning(
                            "event_store_unknown_path", extra={"depth": "event"}
                        )
                        continue
                    if name not in known_ids:
                        shutil.rmtree(entry, ignore_errors=True)
                        orphans_removed += 1

        missing = self.count_rows_with_missing_artifacts()
        report = ReconcileReport(
            temp_directories_removed=temp_removed,
            orphan_directories_removed=orphans_removed,
            rows_with_missing_artifacts=missing,
            unknown_paths=unknown,
        )
        logger.info(
            "event_store_reconciled",
            extra={
                "temp_removed": temp_removed,
                "orphans_removed": orphans_removed,
                "rows_missing_artifacts": missing,
                "unknown_paths": unknown,
            },
        )
        return report

    def _is_plain_directory(self, path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError:
            return False
        return stat_module.S_ISDIR(info.st_mode)

    def count_rows_with_missing_artifacts(self) -> int:
        missing = 0
        for event_id, snapshot_path, crop_path in self._repository.iter_paths():
            for relative in (snapshot_path, crop_path):
                if self.resolve_image(relative) is None:
                    missing += 1
                    logger.warning(
                        "event_artifact_missing", extra={"event_id": event_id}
                    )
                    break
        return missing

    def resolve_image(self, relative_path: str) -> Path | None:
        """Resolve a stored relative image path, or ``None`` when unusable."""
        try:
            resolved = resolve_within(self._data_root, relative_path)
        except UnsafeEventPath:
            logger.warning("event_artifact_invalid", extra={"reason": "unsafe_path"})
            return None
        try:
            info = resolved.lstat()
        except OSError:
            return None
        if not stat_module.S_ISREG(info.st_mode) or info.st_size == 0:
            return None
        return resolved
