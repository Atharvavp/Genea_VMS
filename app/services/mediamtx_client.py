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
  extracted from them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class MediaMTXInfo:
    version: str
    started: str


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

    async def ensure_path(self, name: str, rtsp_url: str) -> None:
        """Make `name` a pull-always RTSP source for `rtsp_url`.

        Uses the `replace` endpoint, which upserts (verified on 1.20.1), so the
        result is the same whether the path existed or not.
        """
        payload = {
            "source": rtsp_url,
            # Pull always: source health must not depend on a browser watching.
            "sourceOnDemand": False,
            # UDP RTP does not survive Docker port mapping reliably.
            "rtspTransport": "tcp",
            # Component 3 boundary: recording is explicitly off for now.
            "record": False,
        }
        await self._request(
            "POST",
            f"/v3/config/paths/replace/{name}",
            json=payload,
            secrets=(rtsp_url,),
        )
        logger.info("mediamtx_path_ensured path=%s", name)

    async def delete_path(self, name: str) -> bool:
        """Remove a path. Returns False if it was already gone."""
        try:
            await self._request("DELETE", f"/v3/config/paths/delete/{name}")
        except PathNotFound:
            return False
        logger.info("mediamtx_path_deleted path=%s", name)
        return True


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    return str(payload)[:200]
