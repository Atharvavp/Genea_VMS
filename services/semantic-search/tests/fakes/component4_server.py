"""A private in-process fake of Component 4's public HTTP API.

It reproduces the behaviours Component 5 actually depends on - opaque cursor
pagination over (crossed_at DESC, id DESC), duplicate timestamps, insertions
during a traversal, artifact 200/404/410/oversize/garbage, the three recording
states, transient failure, outage, and invalid-cursor-after-restart - so the
integration tier can prove backfill, polling, restart, retry, reconciliation,
proxying and search without ever touching a real Component 2-4 process.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.domain.models import EventRecord, format_utc, parse_utc
from tests.fakes.factories import event_payload, jpeg_bytes


def _error(status: int, code: str, message: str = "fake") -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "details": {},
                "request_id": "req_" + "0" * 32,
            }
        },
        status_code=status,
    )


@dataclass
class FakeComponent4:
    """Mutable upstream state a test can steer between requests."""

    events: list[EventRecord] = field(default_factory=list)
    page_size_cap: int = 100
    #: When set, every request fails this way: "timeout", "unreachable", "500".
    outage: str | None = None
    #: Event ids whose artifacts answer 410 / 404 / garbage / oversize.
    gone_artifacts: set[str] = field(default_factory=set)
    missing_artifacts: set[str] = field(default_factory=set)
    garbage_artifacts: set[str] = field(default_factory=set)
    oversized_artifacts: set[str] = field(default_factory=set)
    #: Event ids that fail transiently the first N times.
    transient_artifacts: dict[str, int] = field(default_factory=dict)
    #: Extra raw items injected into the next page (used for malformed items).
    inject_items: list[dict[str, Any]] = field(default_factory=list)
    #: Cursor "signing key". Changing it invalidates every issued cursor.
    cursor_epoch: str = "epoch-1"
    recording_status: str = "AVAILABLE"
    recording_reason: str | None = None
    recording_playback_url: str = "http://localhost:9996/get?path=x&start=y"
    malformed_envelope: bool = False
    request_log: list[str] = field(default_factory=list)

    # ---- helpers ---------------------------------------------------------

    def add(self, records: Iterable[EventRecord]) -> None:
        known = {record.event_id for record in self.events}
        for record in records:
            if record.event_id not in known:
                self.events.append(record)

    def _ordered(self) -> list[EventRecord]:
        return sorted(
            self.events, key=lambda r: (r.crossed_at, r.event_id), reverse=True
        )

    def _encode(self, record: EventRecord) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(
                {
                    "crossed_at": format_utc(record.crossed_at),
                    "id": record.event_id,
                    "epoch": self.cursor_epoch,
                }
            ).encode()
        ).decode()

    def _decode(self, cursor: str) -> tuple[Any, str]:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if payload["epoch"] != self.cursor_epoch:
                raise ValueError("stale cursor")
            return parse_utc(payload["crossed_at"]), payload["id"]
        except Exception as exc:  # noqa: BLE001 - any malformed cursor is a 400
            raise ValueError("invalid cursor") from exc

    # ---- routes ----------------------------------------------------------

    async def list_events(self, request: Request) -> Response:
        self.request_log.append(f"list:{request.url.query}")
        if (failure := self._outage_response()) is not None:
            return failure
        if self.malformed_envelope:
            return JSONResponse({"data": [], "cursor": None})

        limit = int(request.query_params.get("limit", "50"))
        if limit < 1 or limit > self.page_size_cap:
            return _error(422, "validation_error")
        cursor = request.query_params.get("cursor")
        from_raw = request.query_params.get("from")
        to_raw = request.query_params.get("to")

        rows = self._ordered()
        if from_raw:
            start = parse_utc(from_raw)
            rows = [row for row in rows if row.crossed_at >= start]
        if to_raw:
            end = parse_utc(to_raw)
            rows = [row for row in rows if row.crossed_at < end]
        if cursor is not None:
            if cursor == "":
                return _error(400, "invalid_cursor")
            try:
                after_time, after_id = self._decode(cursor)
            except ValueError:
                return _error(400, "invalid_cursor")
            rows = [
                row
                for row in rows
                if (row.crossed_at, row.event_id) < (after_time, after_id)
            ]

        page = rows[:limit]
        items: list[dict[str, Any]] = [event_payload(record) for record in page]
        if self.inject_items:
            items.extend(self.inject_items)
            self.inject_items = []
        next_cursor = self._encode(page[-1]) if len(rows) > limit and page else None
        return JSONResponse({"items": items, "next_cursor": next_cursor})

    async def get_event(self, request: Request) -> Response:
        if (failure := self._outage_response()) is not None:
            return failure
        event_id = request.path_params["event_id"]
        for record in self.events:
            if record.event_id == event_id:
                return JSONResponse(event_payload(record))
        return _error(404, "event_not_found")

    async def get_artifact(self, request: Request) -> Response:
        event_id = request.path_params["event_id"]
        kind = request.url.path.rsplit("/", 1)[-1]
        self.request_log.append(f"{kind}:{event_id}")
        if (failure := self._outage_response()) is not None:
            return failure
        remaining = self.transient_artifacts.get(event_id, 0)
        if remaining > 0:
            self.transient_artifacts[event_id] = remaining - 1
            return _error(503, "service_degraded")
        if event_id in self.missing_artifacts:
            return _error(404, "event_not_found")
        if event_id in self.gone_artifacts:
            return _error(410, "event_artifact_gone")
        if event_id not in {record.event_id for record in self.events}:
            return _error(404, "event_not_found")
        if event_id in self.garbage_artifacts:
            return Response(b"not really a jpeg", media_type="image/jpeg")
        if event_id in self.oversized_artifacts:
            return Response(b"\xff\xd8\xff" + b"\x00" * (20 * 1024 * 1024),
                            media_type="image/jpeg")
        seed = int(event_id[4:8], 16)
        payload = jpeg_bytes(
            64 if kind == "crop" else 160,
            48 if kind == "crop" else 120,
            ((seed * 7) % 256, (seed * 13) % 256, (seed * 29) % 256),
        )
        return Response(
            payload,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def get_recording(self, request: Request) -> Response:
        event_id = request.path_params["event_id"]
        if (failure := self._outage_response()) is not None:
            return failure
        if event_id not in {record.event_id for record in self.events}:
            return _error(404, "event_not_found")
        if self.recording_status == "AVAILABLE":
            return JSONResponse(
                {
                    "status": "AVAILABLE",
                    "event_id": event_id,
                    "recording": {
                        "id": "rec_0123abcd",
                        "start_time": "2026-09-07T09:15:00.000Z",
                        "end_time": "2026-09-07T09:16:00.000Z",
                        "duration_seconds": 60.0,
                        "playback_url": self.recording_playback_url,
                        "source": "mediamtx",
                    },
                    "reason": None,
                }
            )
        return JSONResponse(
            {
                "status": self.recording_status,
                "event_id": event_id,
                "recording": None,
                "reason": self.recording_reason,
            }
        )

    def _outage_response(self) -> Response | None:
        if self.outage is None:
            return None
        if self.outage == "500":
            return _error(500, "internal_error")
        if self.outage == "429":
            return _error(429, "too_many_requests")
        if self.outage == "timeout":
            raise httpx.ReadTimeout("fake upstream timeout")
        raise httpx.ConnectError("fake upstream is down")

    # ---- wiring ----------------------------------------------------------

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/api/events", self.list_events),
                Route("/api/events/{event_id}", self.get_event),
                Route("/api/events/{event_id}/crop", self.get_artifact),
                Route("/api/events/{event_id}/frame", self.get_artifact),
                Route("/api/events/{event_id}/recording", self.get_recording),
            ]
        )

    def client(self) -> httpx.AsyncClient:
        """An httpx client bound to this fake with the production timeouts."""
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app()),
            timeout=httpx.Timeout(connect=2.0, read=10.0, write=2.0, pool=2.0),
            follow_redirects=False,
        )
