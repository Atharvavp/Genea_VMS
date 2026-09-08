"""Thin async wrapper around the MediaMTX Control API (v1.20.1).

Verified against `bluenviron/mediamtx:1.20.1`:

* ``POST /v3/config/paths/replace/{name}`` upserts a path, so one call covers
  both "create" and "update" and no camera change ever restarts the server.
* ``DELETE /v3/config/paths/delete/{name}`` returns 404 when the path is gone.
* ``GET /v3/paths/list`` reports runtime state; ``available`` is the honest
  "can MediaMTX currently obtain this source?" signal. (``online`` stays true
  for a configured-but-unreachable static source, so it is not usable here.)
* ``GET /v3/paths/static-sources/get/{name}`` does **not** exist in 1.20.1, so
  there is no per-source ``lastError`` to read.
* ``GET /v3/config/paths/*`` echoes the source URL **including its password**.
  Those responses are therefore never returned to callers: only path names are
  extracted from them, and the recording-config comparison returns a bare bool.

Recording and playback, verified against the same pinned version:

* a path replacement must carry the **whole** recording block every time. A
  Component 2-shaped payload (source/sourceOnDemand/rtspTransport/record alone)
  silently resets ``recordPath`` to MediaMTX's default, at which point the
  playback server can no longer see anything recorded under the custom path.
* ``recordPath: <root>/%path/%Y-%m-%d/%H-%M-%S-%f`` creates
  ``<root>/<path>/<YYYY-MM-DD>/<HH-MM-SS-ffffff>.mp4``.
* the playback server's ``/list`` returns **timespans**, not files: consecutive
  segments are merged into one entry, so an entry covers everything recorded
  between two interruptions of the source.
* ``/list`` reports "nothing to show" in two different ways. A path that has
  recorded before but has nothing in the requested window answers ``404 no
  recording segments found``; a path that has *never* recorded has no directory
  on disk at all and answers ``400 lstat <root>/<path>: no such file or
  directory``. Both mean an empty history, not a failure.
* ``400 path '...' is not configured`` is the genuinely unavailable case - what
  a disabled camera looks like, because disabling removes its path.
* durations come back normalised (``5m`` reads back as ``5m0s``, ``24h`` as
  ``1d``), so config comparison has to compare seconds, not strings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.security.rtsp_url import sanitize_text

logger = logging.getLogger(__name__)

ITEMS_PER_PAGE = 500


class MediaMTXError(Exception):
    """The Control API answered, but not with what was asked for."""


class MediaMTXUnavailable(MediaMTXError):
    """The Control API could not be reached at all."""


class PathNotFound(MediaMTXError):
    """No such path."""


class PathRejected(MediaMTXError):
    """MediaMTX refused the path configuration (e.g. a source it cannot parse)."""


class RecordingUnavailable(MediaMTXError):
    """History cannot be served: the playback server is down, or the path is gone.

    A historical-playback failure only. It says nothing about whether the camera
    is currently recording, and must never be folded into camera health or
    recording state.
    """


@dataclass(frozen=True)
class MediaMTXInfo:
    version: str
    started: str


@dataclass(frozen=True)
class RecordingPathConfig:
    """The recording half of a managed path's configuration.

    Sent in full on every path replacement, whether or not `record` is true -
    see the module docstring for why a partial payload loses history.
    """

    record_path: str
    segment_duration: str
    delete_after: str
    record_format: str = "fmp4"
    part_duration: str = "1s"
    max_part_size: str = "50M"

    def as_payload(self, record: bool) -> dict[str, Any]:
        return {
            "record": record,
            "recordPath": self.record_path,
            "recordFormat": self.record_format,
            "recordPartDuration": self.part_duration,
            "recordMaxPartSize": self.max_part_size,
            "recordSegmentDuration": self.segment_duration,
            "recordDeleteAfter": self.delete_after,
        }


@dataclass(frozen=True)
class RecordingTimespan:
    """One continuous recorded span, as the playback server reports it."""

    start: str
    duration_seconds: float

    @property
    def end(self) -> str:
        """End of the span, derived from its start and duration."""
        started = datetime.fromisoformat(self.start.replace("Z", "+00:00"))
        ended = started + timedelta(seconds=self.duration_seconds)
        return (
            ended.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )


@dataclass(frozen=True)
class PathRuntime:
    """Runtime state of one MediaMTX path. Contains no credentials."""

    name: str
    available: bool
    ready: bool
    source_type: str | None = None
    tracks: tuple[str, ...] = field(default_factory=tuple)
    readers: int = 0

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "PathRuntime":
        source = payload.get("source") or {}
        return cls(
            name=str(payload.get("name", "")),
            available=bool(payload.get("available")),
            ready=bool(payload.get("ready")),
            source_type=source.get("type") if isinstance(source, dict) else None,
            tracks=tuple(str(track) for track in payload.get("tracks") or ()),
            readers=len(payload.get("readers") or ()),
        )


class MediaMTXClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- plumbing -------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        secrets: tuple[str, ...] = (),
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            response = await self._client.request(
                method, url, json=json, params=params, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            # The exception text can quote the request URL but never the body,
            # so no camera credential can appear here. Sanitised anyway.
            raise MediaMTXUnavailable(
                sanitize_text(f"MediaMTX Control API is unreachable: {exc}", *secrets)
            ) from exc

        if response.status_code >= 400:
            detail = _error_detail(response)
            message = sanitize_text(
                f"MediaMTX returned {response.status_code}: {detail}", *secrets
            )
            if response.status_code == 404:
                raise PathNotFound(message)
            if response.status_code == 400:
                raise PathRejected(message)
            raise MediaMTXError(message)
        return response

    async def _paged_items(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 0
        while True:
            response = await self._request(
                "GET", path, params={"page": page, "itemsPerPage": ITEMS_PER_PAGE}
            )
            payload = response.json()
            items.extend(payload.get("items") or [])
            page += 1
            if page >= int(payload.get("pageCount") or 0):
                return items

    # --- reads ----------------------------------------------------------

    async def get_info(self) -> MediaMTXInfo:
        payload = (await self._request("GET", "/v3/info")).json()
        return MediaMTXInfo(
            version=str(payload.get("version", "")),
            started=str(payload.get("started", "")),
        )

    async def list_paths(self) -> dict[str, PathRuntime]:
        """Runtime state of every path, keyed by name."""
        items = await self._paged_items("/v3/paths/list")
        return {
            str(item.get("name")): PathRuntime.from_api(item)
            for item in items
            if item.get("name")
        }

    async def get_path(self, name: str) -> PathRuntime:
        response = await self._request("GET", f"/v3/paths/get/{name}")
        return PathRuntime.from_api(response.json())

    async def list_configured_path_names(self) -> list[str]:
        """Names of configured paths.

        Only names are returned: the raw payload contains each path's `source`
        with its password in clear, and must not travel any further.
        """
        items = await self._paged_items("/v3/config/paths/list")
        return [str(item["name"]) for item in items if item.get("name")]

    # --- writes ---------------------------------------------------------

    async def ensure_path(
        self,
        name: str,
        rtsp_url: str,
        *,
        record: bool,
        recording: RecordingPathConfig,
    ) -> None:
        """Make `name` a pull-always RTSP source for `rtsp_url`.

        Uses the `replace` endpoint, which upserts (verified on 1.20.1), so the
        result is the same whether the path existed or not.

        The recording block is always sent in full, including when `record` is
        false. Omitting it would reset `recordPath` to MediaMTX's default and
        make everything already recorded under the configured root invisible to
        the playback server - verified, and locked down by a test.
        """
        payload = {
            "source": rtsp_url,
            # Pull always: source health must not depend on a browser watching.
            "sourceOnDemand": False,
            # UDP RTP does not survive Docker port mapping reliably.
            "rtspTransport": "tcp",
            **recording.as_payload(record),
        }
        await self._request(
            "POST",
            f"/v3/config/paths/replace/{name}",
            json=payload,
            secrets=(rtsp_url,),
        )
        logger.info("mediamtx_path_ensured path=%s record=%s", name, record)

    async def path_config_matches(
        self,
        name: str,
        rtsp_url: str,
        *,
        record: bool,
        recording: RecordingPathConfig,
    ) -> bool:
        """Is `name` already configured exactly as the VMS wants it?

        Used to skip a needless replacement on backend restart, which would
        briefly interrupt live delivery and the recorder for no gain.

        Returns a bare bool by design: the config response it reads contains the
        source URL with its password, so nothing derived from it may escape this
        method. A missing path, or any failure to read one, is simply "no".
        """
        try:
            payload = (
                await self._request("GET", f"/v3/config/paths/get/{name}")
            ).json()
        except PathNotFound:
            return False

        if payload.get("source") != rtsp_url:
            return False
        if bool(payload.get("record")) is not record:
            return False
        if payload.get("sourceOnDemand") is not False:
            return False
        if payload.get("rtspTransport") != "tcp":
            return False
        if payload.get("recordPath") != recording.record_path:
            return False
        if payload.get("recordFormat") != recording.record_format:
            return False
        # MediaMTX normalises durations on read-back, so compare seconds.
        for key, wanted in (
            ("recordSegmentDuration", recording.segment_duration),
            ("recordDeleteAfter", recording.delete_after),
            ("recordPartDuration", recording.part_duration),
        ):
            if not _same_duration(payload.get(key), wanted):
                return False
        return True

    async def delete_path(self, name: str) -> bool:
        """Remove a path. Returns False if it was already gone."""
        try:
            await self._request("DELETE", f"/v3/config/paths/delete/{name}")
        except PathNotFound:
            return False
        logger.info("mediamtx_path_deleted path=%s", name)
        return True


class MediaMTXPlaybackClient:
    """The MediaMTX playback server: what history exists, and where to play it.

    Deliberately separate from the Control API client. Its failures are
    historical-playback failures and are mapped to `RecordingUnavailable`, never
    to the path-runtime state that camera health and recording state come from.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def list_timespans(
        self, path: str, start: str, end: str
    ) -> list[RecordingTimespan]:
        """Recorded timespans for `path` inside `[start, end)`.

        An empty history is an empty list, not an error. MediaMTX says so in two
        ways, both of which are normal: `404 no recording segments found` for a
        path that has recorded before but not in this window, and a `400` about
        a missing directory for a path that has never recorded at all - a camera
        whose recording was only just switched on, or which has never been
        online. Only a path that is not configured is genuinely unavailable.
        """
        try:
            response = await self._client.get(
                f"{self.base_url}/list",
                params={"path": path, "start": start, "end": end},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise RecordingUnavailable(
                sanitize_text(f"MediaMTX playback server is unreachable: {exc}")
            ) from exc

        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            detail = _error_detail(response)
            if response.status_code == 400 and _means_nothing_recorded_yet(detail):
                return []
            raise RecordingUnavailable(
                sanitize_text(
                    "MediaMTX playback server returned "
                    f"{response.status_code}: {detail}"
                )
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RecordingUnavailable(
                "MediaMTX playback server returned a response that is not JSON."
            ) from exc
        if not isinstance(payload, list):
            raise RecordingUnavailable(
                "MediaMTX playback server returned an unexpected response shape."
            )

        spans: list[RecordingTimespan] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            start_value = item.get("start")
            duration = item.get("duration")
            if not start_value or duration is None:
                continue
            try:
                span = RecordingTimespan(
                    start=str(start_value), duration_seconds=float(duration)
                )
                span.end  # reject a start value that cannot be parsed
            except (TypeError, ValueError):
                continue
            spans.append(span)
        # MediaMTX's own `url` is ignored on purpose: it reflects whatever Host
        # header this request carried, which is a compose-internal address.
        return sorted(spans, key=lambda span: span.start)


def _means_nothing_recorded_yet(detail: str) -> bool:
    """Is this 400 "there is no recording directory" rather than a real failure?

    MediaMTX only creates a path's recording directory when it first writes a
    segment, and `/list` then fails to stat it. A camera with recording just
    switched on, or one that has never come online, is in exactly that state,
    and it must read as an empty history rather than an outage.

    Deliberately narrow: `path '...' is not configured` is a different 400 and
    stays unavailable, because that is what a disabled camera looks like.
    """
    lowered = detail.lower()
    return "no such file or directory" in lowered and "not configured" not in lowered


def _same_duration(actual: Any, wanted: str) -> bool:
    """Compare two MediaMTX durations by value, tolerating normalisation."""
    if not isinstance(actual, str):
        return False
    if actual == wanted:
        return True
    left, right = _duration_seconds(actual), _duration_seconds(wanted)
    return left is not None and right is not None and left == right


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|us|ns|[dhms])")
_DURATION_SECONDS = {
    "ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0,
    "h": 3600.0, "d": 86400.0,
}


def _duration_seconds(value: str) -> float | None:
    """Seconds in a Go-style duration, including composite forms like `1h0m0s`."""
    parts = _DURATION_PART.findall(value.strip())
    if not parts:
        return None
    if "".join(f"{number}{unit}" for number, unit in parts) != value.strip():
        return None
    return sum(float(number) * _DURATION_SECONDS[unit] for number, unit in parts)


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    return str(payload)[:200]
