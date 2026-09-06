"""Historical recordings: what exists, and where the browser can play it.

MediaMTX is authoritative for recording existence. There is no index table and
no filesystem scan: the playback server already answers "which finalised
recordings exist for camera X on day Y?", and duplicating that in SQLite would
create a second source of truth that could disagree with the files on disk.

What this service adds on top of MediaMTX is the part MediaMTX cannot do:

* it resolves a *camera id* to the MediaMTX path that camera owns, so a client
  never supplies a path and cannot ask for someone else's recordings;
* it turns a UTC calendar date into the time window to query;
* it builds public playback URLs from VMS settings rather than echoing the URL
  MediaMTX returns, which reflects the compose-internal address the request
  happened to carry.

No media passes through this process, and no filesystem path is ever accepted
from or returned to a client.

Failures here are *historical playback* failures. They are reported as such and
are never folded into a camera's recording state - a camera can be recording
perfectly well while the playback server is unreachable.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.domain.models import (
    CameraRecord,
    RecordingItem,
    RecordingListResponse,
    recording_id,
)
from app.persistence.camera_repository import CameraRepository
from app.services.mediamtx_client import MediaMTXPlaybackClient, RecordingUnavailable

logger = logging.getLogger(__name__)

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CameraNotFound(KeyError):
    """No camera with that id."""


class DisabledCameraHistory(Exception):
    """History was asked for on a camera that is currently disabled.

    Component 2 semantics remove a disabled camera's MediaMTX path, and the
    playback server refuses any path it does not have configured - verified. The
    files are still on disk; they become visible again when the camera is
    re-enabled.
    """


class InvalidDate(ValueError):
    """The supplied date is not a `YYYY-MM-DD` calendar day."""


def parse_utc_day(value: str) -> tuple[str, str]:
    """`YYYY-MM-DD` -> the RFC3339 bounds of that UTC day, `[start, end)`.

    Dates are UTC calendar days throughout: MediaMTX records and lists in UTC,
    and the frontend asks with `toISOString().slice(0, 10)`, so there is one
    interpretation of "today" on both sides.
    """
    if not isinstance(value, str) or not DATE_PATTERN.match(value.strip()):
        raise InvalidDate("date must be a calendar day in YYYY-MM-DD form.")
    try:
        day = datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise InvalidDate("date is not a real calendar day.") from exc
    return _rfc3339(day), _rfc3339(day + timedelta(days=1))


def _rfc3339(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


class RecordingManager:
    def __init__(
        self,
        settings: Settings,
        repository: CameraRepository,
        playback: MediaMTXPlaybackClient,
    ) -> None:
        self._settings = settings
        self._repo = repository
        self._playback = playback

    async def aclose(self) -> None:
        await self._playback.aclose()

    async def list_recordings(self, camera_id: str, date: str) -> RecordingListResponse:
        """Finalised recordings for one camera on one UTC day."""
        start, end = parse_utc_day(date)
        record = await self._require(camera_id)
        if not record.enabled:
            raise DisabledCameraHistory(camera_id)

        spans = await self._playback.list_timespans(record.mediamtx_path, start, end)
        logger.info(
            "recordings_listed camera_id=%s date=%s items=%d",
            record.id,
            date,
            len(spans),
        )
        return RecordingListResponse(
            camera_id=record.id,
            camera_name=record.name,
            date=date.strip(),
            items=[
                RecordingItem(
                    id=recording_id(record.id, span.start, span.duration_seconds),
                    start_time=span.start,
                    end_time=span.end,
                    duration_seconds=span.duration_seconds,
                    playback_url=self._settings.playback_url(
                        record.mediamtx_path, span.start, span.duration_seconds
                    ),
                )
                for span in spans
            ],
        )

    async def _require(self, camera_id: str) -> CameraRecord:
        record = await asyncio.to_thread(self._repo.get, camera_id)
        if record is None:
            raise CameraNotFound(camera_id)
        return record


__all__ = [
    "CameraNotFound",
    "DisabledCameraHistory",
    "InvalidDate",
    "RecordingManager",
    "RecordingUnavailable",
    "parse_utc_day",
]
