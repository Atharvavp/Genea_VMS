"""Read-only Component 3 recording lookup.

This is the ONLY integration with the VMS. It performs one bounded GET against
the public recordings endpoint and returns a value. It cannot mutate a worker,
a camera, a line, a tracker, or any analytics state, and a failure here never
becomes a route-level 5xx.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.domain.models import EventRecord, RecordingStatus, parse_iso_utc, to_iso_ms

__all__ = [
    "VMSRecordingClient",
    "RecordingLookup",
    "RecordingMatch",
    "UnavailableReason",
    "NOT_FOUND_REASON",
    "build_client",
]

logger = logging.getLogger("analytics.vms_recordings")

NOT_FOUND_REASON: Final[str] = "no_containing_recording"

#: The complete, safe reason vocabulary. Nothing else ever reaches a client.
UNAVAILABLE_REASONS: Final[frozenset[str]] = frozenset(
    {
        "vms_timeout",
        "vms_unreachable",
        "vms_rejected_request",
        "vms_unavailable",
        "vms_invalid_response",
        "unsafe_playback_url",
    }
)

#: Provided and computed ends must agree to within this much.
END_TIME_TOLERANCE = timedelta(milliseconds=1)


class UnavailableReason:
    TIMEOUT = "vms_timeout"
    UNREACHABLE = "vms_unreachable"
    REJECTED = "vms_rejected_request"
    UNAVAILABLE = "vms_unavailable"
    INVALID = "vms_invalid_response"
    UNSAFE_URL = "unsafe_playback_url"


class _RecordingItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    start_time: str
    end_time: str
    duration_seconds: float
    playback_url: str
    source: str | None = None


class _RecordingList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    camera_id: str
    camera_name: str | None = None
    date: str
    items: list[_RecordingItem]


@dataclass(frozen=True, slots=True)
class RecordingMatch:
    id: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    playback_url: str
    source: str | None


@dataclass(frozen=True, slots=True)
class RecordingLookup:
    status: RecordingStatus
    recording: RecordingMatch | None
    reason: str | None


def _safe_playback_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if not parts.hostname:
        return False
    if parts.username or parts.password:
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return False
    return len(url) <= 2048


def build_client(timeout_scale: float = 1.0) -> httpx.AsyncClient:
    """One lifespan-owned client with an explicit, bounded timeout budget."""
    timeout = httpx.Timeout(
        connect=2.0 * timeout_scale,
        read=5.0 * timeout_scale,
        write=2.0 * timeout_scale,
        pool=2.0 * timeout_scale,
    )
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


class VMSRecordingClient:
    def __init__(self, *, base_url: str, client: httpx.AsyncClient):
        self._base_url = base_url.rstrip("/")
        self._client = client

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/api/recordings"

    async def lookup(self, event: EventRecord) -> RecordingLookup:
        """Find the recording timespan that contains ``event.crossed_at``."""
        crossed_at = event.crossed_at.astimezone(UTC)
        params = {
            "camera_id": event.vms_camera_id,
            "date": crossed_at.date().isoformat(),
        }
        try:
            response = await self._client.get(self.endpoint, params=params)
        except httpx.TimeoutException:
            return self._unavailable(event, UnavailableReason.TIMEOUT)
        except httpx.HTTPError:
            return self._unavailable(event, UnavailableReason.UNREACHABLE)

        if response.status_code != 200:
            reason = (
                UnavailableReason.REJECTED
                if 400 <= response.status_code < 500
                else UnavailableReason.UNAVAILABLE
            )
            return self._unavailable(event, reason, status=response.status_code)

        try:
            payload = _RecordingList.model_validate(response.json())
        except (ValidationError, ValueError):
            return self._unavailable(event, UnavailableReason.INVALID)

        if payload.camera_id != event.vms_camera_id:
            return self._unavailable(event, UnavailableReason.INVALID)

        try:
            candidates = self._parse_items(payload.items)
        except _UnsafeUrl:
            return self._unavailable(event, UnavailableReason.UNSAFE_URL)
        except _MalformedItem:
            return self._unavailable(event, UnavailableReason.INVALID)

        containing = [
            item
            for item in candidates
            if item.start_time <= crossed_at < item.end_time
        ]
        if not containing:
            logger.info(
                "vms_recording_not_found",
                extra={"event_id": event.id, "items": len(candidates)},
            )
            return RecordingLookup(
                status=RecordingStatus.NOT_FOUND,
                recording=None,
                reason=NOT_FOUND_REASON,
            )
        if len(containing) > 1:
            # Upstream spans should never overlap. Choose deterministically and
            # say so; never mutate the VMS to "fix" it.
            containing.sort(key=lambda item: (item.start_time, item.id))
            logger.warning(
                "vms_recording_overlap",
                extra={"event_id": event.id, "matches": len(containing)},
            )
        return RecordingLookup(
            status=RecordingStatus.AVAILABLE,
            recording=containing[-1],
            reason=None,
        )

    def _parse_items(self, items: list[_RecordingItem]) -> list[RecordingMatch]:
        parsed: list[RecordingMatch] = []
        for item in items:
            try:
                start = parse_iso_utc(item.start_time)
                provided_end = parse_iso_utc(item.end_time)
            except ValueError as exc:
                raise _MalformedItem from exc
            duration = float(item.duration_seconds)
            if not (duration > 0.0) or duration != duration or duration == float("inf"):
                raise _MalformedItem
            computed_end = start + timedelta(seconds=duration)
            if abs(computed_end - provided_end) > END_TIME_TOLERANCE:
                raise _MalformedItem
            if not _safe_playback_url(item.playback_url):
                raise _UnsafeUrl
            parsed.append(
                RecordingMatch(
                    id=item.id,
                    start_time=start,
                    end_time=computed_end,
                    duration_seconds=duration,
                    playback_url=item.playback_url,
                    source=item.source,
                )
            )
        return parsed

    def _unavailable(
        self, event: EventRecord, reason: str, *, status: int | None = None
    ) -> RecordingLookup:
        assert reason in UNAVAILABLE_REASONS
        logger.warning(
            "vms_recording_unavailable",
            extra={
                "event_id": event.id,
                "reason": reason,
                "upstream_status": status,
            },
        )
        return RecordingLookup(
            status=RecordingStatus.UNAVAILABLE, recording=None, reason=reason
        )


class _MalformedItem(Exception):
    pass


class _UnsafeUrl(Exception):
    pass


def serialise_match(match: RecordingMatch) -> dict[str, Any]:
    return {
        "id": match.id,
        "start_time": to_iso_ms(match.start_time),
        "end_time": to_iso_ms(match.end_time),
        "duration_seconds": match.duration_seconds,
        "playback_url": match.playback_url,
        "source": match.source,
    }
