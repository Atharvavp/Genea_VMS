"""The only code in Component 5 that talks to Component 4.

Three operations exist and no more: list events, fetch one event's crop or
frame, look up one event's recording. Every outbound URL is built from the
configured base plus a locally validated event id - a caller, an event payload,
or an upstream response can never influence the destination, so there is no
SSRF surface and no way to reach the VMS, MediaMTX, or a recording file.

Failure classification is the contract the indexer depends on:

* ``UpstreamGone`` / ``UpstreamEventNotFound`` are permanent for that artifact;
* ``UpstreamTimeout`` / ``UpstreamUnavailable`` / ``UpstreamRejected`` (408, 429,
  5xx, transport) are retryable and never invalidate a stored vector;
* ``UpstreamContractError`` pauses the current discovery pass rather than
  advancing a cursor over data this build cannot interpret.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from typing import Any, Final, Literal
from urllib.parse import urlsplit

import httpx

from app.domain.models import (
    Direction,
    EventRecord,
    ObjectCategory,
    ObjectClass,
    RepresentationKind,
    format_utc,
    is_event_id,
    parse_utc,
    utc_now,
)

__all__ = [
    "Component4Client", "EventPage", "RecordingLookup", "RecordingInfo",
    "UpstreamError", "UpstreamTimeout", "UpstreamUnavailable", "UpstreamRejected",
    "UpstreamContractError", "UpstreamGone", "UpstreamEventNotFound",
    "InvalidCursor", "build_client", "PAGE_SIZE", "SAFE_RECORDING_REASONS",
]

logger = logging.getLogger("semantic.component4")

#: Component 4 caps `limit` at 100; Component 5 always requests a full page.
PAGE_SIZE: Final[int] = 100
_MAX_IMAGE_BYTES_DEFAULT: Final[int] = 16 * 1024 * 1024
_MAX_PLAYBACK_URL: Final[int] = 2048
_JPEG_MAGIC: Final[bytes] = b"\xff\xd8\xff"

#: Reasons Component 5 may return for a recording lookup it could not complete.
SAFE_RECORDING_REASONS: Final[frozenset[str]] = frozenset(
    {
        "component4_timeout",
        "component4_unreachable",
        "component4_rejected_request",
        "component4_unavailable",
        "component4_invalid_response",
        "component4_event_not_found",
        "unsafe_playback_url",
    }
)


class UpstreamError(RuntimeError):
    """Base class. ``code`` is a stable, safe, loggable identifier."""

    code = "upstream_error"
    retryable = True


class UpstreamTimeout(UpstreamError):
    code = "component4_timeout"


class UpstreamUnavailable(UpstreamError):
    code = "component4_unreachable"


class UpstreamRejected(UpstreamError):
    """408/429/5xx: Component 4 answered, but not usefully. Retryable."""

    code = "component4_rejected_request"


class UpstreamContractError(UpstreamError):
    """The response does not match the contract this build was written against."""

    code = "upstream_contract_error"
    retryable = False


class UpstreamGone(UpstreamError):
    """410: the artifact was committed but is no longer available. Permanent."""

    code = "event_artifact_gone"
    retryable = False


class UpstreamEventNotFound(UpstreamError):
    """404 for a specific event. Permanent for that event's representation."""

    code = "upstream_event_not_found"
    retryable = False


class InvalidCursor(UpstreamError):
    """400 invalid_cursor: clear only this pass's cursor and restart it."""

    code = "invalid_cursor"
    retryable = False


@dataclass(frozen=True, slots=True)
class EventPage:
    events: tuple[EventRecord, ...]
    next_cursor: str | None
    malformed: int = 0
    raw_count: int = 0


@dataclass(frozen=True, slots=True)
class RecordingInfo:
    id: str
    start_time: str
    end_time: str
    duration_seconds: float
    playback_url: str
    source: str


@dataclass(frozen=True, slots=True)
class RecordingLookup:
    status: Literal["AVAILABLE", "NOT_FOUND", "UNAVAILABLE"]
    recording: RecordingInfo | None = None
    reason: str | None = None


def build_client(*, image_read_timeout: float = 15.0) -> httpx.AsyncClient:
    """One shared client. Redirects are never followed: a redirect could move
    the request off the configured base."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=2.0, read=10.0, write=2.0, pool=2.0),
        follow_redirects=False,
        headers={"User-Agent": "genea-semantic-search/0.1"},
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    )


def _safe_playback_url(value: object) -> str | None:
    """Accept only an http(s) URL with a host, no credentials, no controls."""
    if not isinstance(value, str) or not value or len(value) > _MAX_PLAYBACK_URL:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return value


class Component4Client:
    """A read-only view of Component 4 over its public HTTP API."""

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.AsyncClient,
        max_image_bytes: int = _MAX_IMAGE_BYTES_DEFAULT,
        image_read_timeout: float = 15.0,
    ):
        self._base = base_url.rstrip("/")
        self._client = client
        self._max_image_bytes = int(max_image_bytes)
        self._image_timeout = httpx.Timeout(
            connect=2.0, read=image_read_timeout, write=2.0, pool=2.0
        )

    @property
    def base_url(self) -> str:
        return self._base

    # ---- listing ---------------------------------------------------------

    async def list_events(
        self, *, cursor: str | None = None, from_: datetime | None = None
    ) -> EventPage:
        """One page of events, newest first. Cursors are opaque and unmodified."""
        params: dict[str, str] = {"limit": str(PAGE_SIZE)}
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                raise UpstreamContractError("cursor must be a non-empty string")
            params["cursor"] = cursor
        if from_ is not None:
            params["from"] = format_utc(from_)

        response = await self._request("GET", "/api/events", params=params)
        if response.status_code == 400:
            code = self._error_code(response)
            if code == "invalid_cursor":
                raise InvalidCursor("Component 4 rejected the stored cursor")
            raise UpstreamContractError(f"Component 4 rejected the query: {code}")
        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamContractError("event list is not JSON") from exc
        return self._parse_page(payload)

    def _parse_page(self, payload: Any) -> EventPage:
        if not isinstance(payload, dict):
            raise UpstreamContractError("event list envelope is not an object")
        items = payload.get("items")
        next_cursor = payload.get("next_cursor")
        if not isinstance(items, list):
            raise UpstreamContractError("event list 'items' is not an array")
        if next_cursor is not None and (
            not isinstance(next_cursor, str) or not next_cursor
        ):
            raise UpstreamContractError("event list 'next_cursor' is not a cursor")

        now = utc_now()
        events: list[EventRecord] = []
        malformed = 0
        for item in items:
            try:
                events.append(self._parse_event(item, now))
            except (ValueError, TypeError, KeyError) as exc:
                # One malformed item must not discard its valid peers.
                malformed += 1
                logger.warning(
                    "upstream_event_rejected",
                    extra={"reason": type(exc).__name__},
                )
        return EventPage(
            events=tuple(events),
            next_cursor=next_cursor,
            malformed=malformed,
            raw_count=len(items),
        )

    @staticmethod
    def _parse_event(item: Any, now: datetime) -> EventRecord:
        if not isinstance(item, dict):
            raise TypeError("event item is not an object")
        event_id = item["id"]
        if not is_event_id(event_id):
            raise ValueError("event id is not a Component 4 event id")
        for key, expected in (
            ("crop_url", f"/api/events/{event_id}/crop"),
            ("frame_url", f"/api/events/{event_id}/frame"),
        ):
            # The declared paths are checked, but never used to build a request:
            # every outbound URL is composed from the configured base instead.
            if item.get(key) != expected:
                raise ValueError(f"{key} is not the expected Component 4 path")
        camera_name = item["camera_name"]
        if not isinstance(camera_name, str) or not camera_name.strip():
            raise ValueError("camera_name is empty")
        return EventRecord(
            event_id=event_id,
            camera_id=item["camera_id"],
            vms_camera_id=item["vms_camera_id"],
            camera_name=camera_name[:200],
            crossed_at=parse_utc(item["crossed_at"]),
            object_category=ObjectCategory(item["object_category"]),
            object_class=ObjectClass(item["object_class"]),
            direction=Direction(item["direction"]),
            discovered_at=now,
            source_last_seen_at=now,
        )

    # ---- artifacts -------------------------------------------------------

    async def fetch_artifact(
        self, event_id: str, kind: RepresentationKind | str
    ) -> bytes:
        """Fetch one committed JPEG. Bounded, content-checked, and decodable."""
        if not is_event_id(event_id):
            raise UpstreamContractError("refusing to fetch a non-event id")
        kind_value = str(kind)
        if kind_value not in {"crop", "frame"}:
            raise UpstreamContractError("unknown artifact kind")

        path = f"/api/events/{event_id}/{kind_value}"
        response = await self._request("GET", path, timeout=self._image_timeout, stream=True)
        try:
            if response.status_code == 404:
                raise UpstreamEventNotFound("Component 4 does not know this event")
            if response.status_code == 410:
                raise UpstreamGone("the artifact is no longer available")
            self._raise_for_status(response)

            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit():
                if int(declared) > self._max_image_bytes:
                    raise UpstreamContractError("artifact exceeds the byte cap")

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._max_image_bytes:
                    raise UpstreamContractError("artifact exceeds the byte cap")
                chunks.append(chunk)
        finally:
            await response.aclose()

        payload = b"".join(chunks)
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if content_type != "image/jpeg":
            raise UpstreamContractError("artifact is not declared as image/jpeg")
        if not payload.startswith(_JPEG_MAGIC):
            raise UpstreamContractError("artifact is not a JPEG")
        _verify_single_frame_jpeg(payload)
        return payload

    # ---- recording -------------------------------------------------------

    async def fetch_recording(self, event_id: str) -> RecordingLookup:
        """Delegate the recording question to Component 4 and validate the shape.

        Component 5 never derives, rewrites, proxies, or stores a playback URL:
        Component 3 owns recordings and Component 4 owns their resolution.
        """
        if not is_event_id(event_id):
            raise UpstreamContractError("refusing to look up a non-event id")
        response = await self._request("GET", f"/api/events/{event_id}/recording")
        if response.status_code == 404:
            raise UpstreamEventNotFound("Component 4 does not know this event")
        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamContractError("recording response is not JSON") from exc
        if not isinstance(payload, dict):
            raise UpstreamContractError("recording response is not an object")

        status = payload.get("status")
        if status not in {"AVAILABLE", "NOT_FOUND", "UNAVAILABLE"}:
            raise UpstreamContractError("recording status is not a known value")
        reason = payload.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 64):
            raise UpstreamContractError("recording reason is not a safe token")

        if status != "AVAILABLE":
            return RecordingLookup(status=status, recording=None, reason=reason)

        recording = payload.get("recording")
        if not isinstance(recording, dict):
            raise UpstreamContractError("AVAILABLE recording has no recording object")
        playback_url = _safe_playback_url(recording.get("playback_url"))
        if playback_url is None:
            # Component 4 said AVAILABLE, but the URL is not one this service is
            # willing to hand a browser. Downgrade rather than pass it through.
            return RecordingLookup(
                status="UNAVAILABLE", recording=None, reason="unsafe_playback_url"
            )
        try:
            info = RecordingInfo(
                id=str(recording["id"])[:128],
                start_time=format_utc(parse_utc(recording["start_time"])),
                end_time=format_utc(parse_utc(recording["end_time"])),
                duration_seconds=float(recording["duration_seconds"]),
                playback_url=playback_url,
                source=str(recording.get("source", "unknown"))[:32],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamContractError("recording object is malformed") from exc
        return RecordingLookup(status="AVAILABLE", recording=info, reason=None)

    # ---- transport -------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        url = f"{self._base}{path}"
        try:
            request = self._client.build_request(
                method, url, params=params, timeout=timeout
            )
            return await self._client.send(request, stream=stream)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout("Component 4 did not respond in time") from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable("Component 4 is not reachable") from exc

    @staticmethod
    def _error_code(response: httpx.Response) -> str:
        try:
            body = response.json()
            code = body.get("error", {}).get("code")
        except (ValueError, AttributeError):
            return "unknown"
        return code if isinstance(code, str) and len(code) <= 64 else "unknown"

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        if status in (408, 429) or status >= 500:
            raise UpstreamRejected(f"Component 4 returned {status}")
        raise UpstreamContractError(f"Component 4 returned {status}")


def _verify_single_frame_jpeg(payload: bytes) -> None:
    """Decode-verify without keeping the decoded image."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(BytesIO(payload)) as image:
            if image.format != "JPEG":
                raise UpstreamContractError("artifact is not a JPEG")
            if getattr(image, "n_frames", 1) != 1:
                raise UpstreamContractError("artifact is not a single frame")
            image.verify()
    except UpstreamContractError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UpstreamContractError("artifact could not be decoded") from exc
