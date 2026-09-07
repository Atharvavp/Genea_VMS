"""Event read, filter, cursor, image resolution, and recording delegation.

This service is read-only with respect to analytics runtime state. It holds no
reference to the camera manager and therefore cannot change a worker, a
tracker, a line, or a camera even by accident.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from app.domain.models import (
    CrossingDirection,
    EventRecord,
    ObjectCategory,
    RecordingStatus,
    SUPPORTED_OBJECT_CLASSES,
    parse_iso_utc,
    to_iso_ms,
)
from app.persistence.event_repository import (
    MAX_EVENT_PAGE_SIZE,
    EventFilter,
    EventRepository,
)
from app.persistence.event_storage import EventStorage
from app.services.vms_recordings import RecordingLookup, VMSRecordingClient

__all__ = [
    "EventService",
    "EventQuery",
    "InvalidCursor",
    "InvalidTimeRange",
    "EventNotFound",
    "ArtifactUnavailable",
    "MAX_RANGE_DAYS",
]

logger = logging.getLogger("analytics.events")

MAX_RANGE_DAYS: Final[int] = 31
DEFAULT_PAGE_SIZE: Final[int] = 50


class InvalidCursor(ValueError):
    pass


class InvalidTimeRange(ValueError):
    pass


class EventNotFound(LookupError):
    pass


class ArtifactUnavailable(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class EventQuery:
    filters: EventFilter
    limit: int
    cursor: str | None


def _b64encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class EventService:
    def __init__(
        self,
        *,
        repository: EventRepository,
        storage: EventStorage,
        recordings: VMSRecordingClient,
        cursor_signing_key: str,
    ):
        self._repository = repository
        self._storage = storage
        self._recordings = recordings
        self._key = cursor_signing_key.encode("utf-8")

    # -- filters and cursors ------------------------------------------------

    def build_query(
        self,
        *,
        camera_id: str | None,
        object_category: str | None,
        object_class: str | None,
        direction: str | None,
        start: str | None,
        end: str | None,
        limit: int | None,
        cursor: str | None,
    ) -> EventQuery:
        parsed_start = self._parse_bound(start, "from")
        parsed_end = self._parse_bound(end, "to")
        if parsed_start is not None and parsed_end is not None:
            if parsed_start >= parsed_end:
                raise InvalidTimeRange("'from' must be strictly before 'to'")
            if parsed_end - parsed_start > timedelta(days=MAX_RANGE_DAYS):
                raise InvalidTimeRange(
                    f"the requested range exceeds {MAX_RANGE_DAYS} days"
                )
        if object_class is not None and object_class not in SUPPORTED_OBJECT_CLASSES:
            raise ValueError("object_class is not a supported class")

        filters = EventFilter(
            camera_id=camera_id,
            object_category=ObjectCategory(object_category) if object_category else None,
            object_class=object_class,
            direction=CrossingDirection(direction) if direction else None,
            start=parsed_start,
            end=parsed_end,
        )
        size = DEFAULT_PAGE_SIZE if limit is None else int(limit)
        if not (1 <= size <= MAX_EVENT_PAGE_SIZE):
            raise ValueError(f"limit must be between 1 and {MAX_EVENT_PAGE_SIZE}")
        return EventQuery(filters=filters, limit=size, cursor=cursor)

    @staticmethod
    def _parse_bound(value: str | None, field: str) -> datetime | None:
        if value is None or value == "":
            return None
        try:
            return parse_iso_utc(value)
        except ValueError as exc:
            raise InvalidTimeRange(f"'{field}' is not a UTC timestamp") from exc

    def _filter_hash(self, filters: EventFilter) -> str:
        digest = hashlib.sha256(filters.identity().encode("utf-8")).hexdigest()
        return digest[:16]

    def encode_cursor(self, filters: EventFilter, last: EventRecord) -> str:
        payload = json.dumps(
            {
                "crossed_at": to_iso_ms(last.crossed_at),
                "id": last.id,
                "filter_hash": self._filter_hash(filters),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self._key, payload, hashlib.sha256).digest()
        return f"{_b64encode(payload)}.{_b64encode(signature)}"

    def decode_cursor(
        self, cursor: str, filters: EventFilter
    ) -> tuple[datetime, str]:
        try:
            body, signature = cursor.split(".", 1)
            payload = _b64decode(body)
            expected = hmac.new(self._key, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _b64decode(signature)):
                raise InvalidCursor("cursor signature does not verify")
            data = json.loads(payload)
            marker = parse_iso_utc(str(data["crossed_at"]))
            event_id = str(data["id"])
            filter_hash = str(data["filter_hash"])
        except InvalidCursor:
            raise
        except Exception as exc:  # noqa: BLE001
            raise InvalidCursor("cursor is malformed") from exc
        if filter_hash != self._filter_hash(filters):
            raise InvalidCursor("cursor does not belong to these filters")
        return marker, event_id

    # -- reads --------------------------------------------------------------

    def list_events(self, query: EventQuery) -> tuple[list[EventRecord], str | None]:
        marker: datetime | None = None
        marker_id: str | None = None
        if query.cursor is not None:
            # An explicitly supplied but empty cursor is a client bug, not
            # "start from the beginning"; only an absent cursor means that.
            marker, marker_id = self.decode_cursor(query.cursor, query.filters)
        page = self._repository.query(
            query.filters,
            limit=query.limit,
            cursor_crossed_at=marker,
            cursor_id=marker_id,
        )
        next_cursor = (
            self.encode_cursor(query.filters, page.items[-1])
            if page.has_more and page.items
            else None
        )
        return page.items, next_cursor

    def get_event(self, event_id: str) -> EventRecord:
        record = self._repository.get(event_id)
        if record is None:
            raise EventNotFound(event_id)
        return record

    def resolve_image(self, event_id: str, kind: str) -> tuple[EventRecord, Path]:
        record = self.get_event(event_id)
        relative = record.snapshot_path if kind == "frame" else record.crop_path
        resolved = self._storage.resolve_image(relative)
        if resolved is None:
            logger.warning(
                "event_artifact_gone", extra={"event_id": event_id, "kind": kind}
            )
            raise ArtifactUnavailable(event_id)
        return record, resolved

    # -- recording lookup ---------------------------------------------------

    async def lookup_recording(self, event_id: str) -> tuple[EventRecord, RecordingLookup]:
        """Delegate to the VMS client. Never changes analytics state."""
        record = self.get_event(event_id)
        lookup = await self._recordings.lookup(record)
        logger.info(
            "recording_lookup",
            extra={
                "event_id": record.id,
                "status": lookup.status.value,
                "reason": lookup.reason,
            },
        )
        assert lookup.status in RecordingStatus
        return record, lookup
