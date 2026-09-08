"""The periodic full pass that makes the local mirror gap-safe.

Overlap polling closes small gaps. This closes the rest: an event committed with
a clock skew larger than the overlap window, an event missed while the upstream
was unreachable, a camera renamed since discovery, and a stored vector that has
since become unreadable.

Two rules keep it safe:

* it NEVER deletes a local event because one traversal did not return it. A
  Component 4 filter change, a partial outage, or a paging quirk must not be
  able to erase local history.
* it repairs the snapshot only by rebuilding it from validated rows, so a
  reconciliation pass can never publish a partial index.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final

from app.config import Settings
from app.domain.models import utc_now
from app.integrations.component4 import (
    Component4Client,
    InvalidCursor,
    UpstreamContractError,
    UpstreamError,
)
from app.persistence.repositories import (
    EventRepository,
    RepresentationRepository,
    SyncRepository,
)
from app.retrieval.snapshot import SnapshotStore
from app.services.indexer import SnapshotRebuilder

__all__ = ["ReconciliationService", "ReconcileReport"]

logger = logging.getLogger("semantic.reconciliation")

_MAX_PAGES: Final[int] = 10_000


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    pages: int = 0
    seen: int = 0
    discovered: int = 0
    refreshed: int = 0
    malformed: int = 0
    invalid_vectors: int = 0
    snapshot_rebuilt: bool = False
    completed: bool = False
    error_code: str | None = None


class ReconciliationService:
    def __init__(
        self,
        *,
        client: Component4Client,
        events: EventRepository,
        representations: RepresentationRepository,
        sync: SyncRepository,
        store: SnapshotStore,
        rebuilder: SnapshotRebuilder,
        settings: Settings,
    ):
        self._client = client
        self._events = events
        self._representations = representations
        self._sync = sync
        self._store = store
        self._rebuilder = rebuilder
        self._settings = settings

    async def run(self, stop: asyncio.Event) -> None:
        interval = self._settings.semantic_full_reconcile_seconds
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except (TimeoutError, asyncio.TimeoutError):
                pass
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bug must not kill the loop
                logger.exception("reconciliation_error")

    async def reconcile(self) -> ReconcileReport:
        """One complete pass: rediscover, refresh, revalidate, republish."""
        state = self._sync.read()
        cursor = state.full_reconcile_cursor
        pages = seen = discovered = refreshed = malformed = 0
        error_code: str | None = None
        completed = False

        while pages < _MAX_PAGES:
            try:
                page = await self._client.list_events(cursor=cursor)
            except InvalidCursor:
                logger.warning("reconcile_cursor_reset")
                self._sync.update(full_reconcile_cursor=None)
                error_code = "invalid_cursor"
                break
            except (UpstreamContractError, UpstreamError) as exc:
                # The pass pauses; the cursor stays so it resumes where it was.
                logger.warning("reconcile_paused", extra={"error_code": exc.code})
                error_code = exc.code
                break

            inserted, updated = self._events.upsert_page(page.events)
            discovered += inserted
            refreshed += updated
            seen += len(page.events)
            malformed += page.malformed
            pages += 1
            cursor = page.next_cursor
            self._sync.update(full_reconcile_cursor=cursor)
            if cursor is None:
                completed = True
                break
            await asyncio.sleep(0)

        invalid_vectors = 0
        rebuilt = False
        if completed:
            self._sync.update(last_full_reconcile_at=utc_now())
        # Revalidate stored vectors and repair the snapshot if it has drifted
        # from the durable revision (for example a crash between the commit and
        # the snapshot swap).
        _accepted, invalid = await asyncio.to_thread(
            self._representations.load_indexed_vectors
        )
        invalid_vectors = len(invalid)
        durable_revision = self._representations.index_revision()
        if invalid_vectors or self._store.current().index_revision != durable_revision:
            await self._rebuilder.rebuild_async()
            rebuilt = True

        logger.info(
            "reconcile_finished",
            extra={
                "pages": pages, "seen": seen, "discovered": discovered,
                "refreshed": refreshed, "malformed": malformed,
                "invalid_vectors": invalid_vectors, "rebuilt": rebuilt,
                "completed": completed, "error_code": error_code,
            },
        )
        return ReconcileReport(
            pages=pages, seen=seen, discovered=discovered, refreshed=refreshed,
            malformed=malformed, invalid_vectors=invalid_vectors,
            snapshot_rebuilt=rebuilt, completed=completed, error_code=error_code,
        )
