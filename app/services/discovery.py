"""Historical backfill, ten-second overlap polling, and upstream state.

Discovery mirrors Component 4's event metadata into the local store. It is
deliberately separate from embedding: thousands of event summaries can be
discovered quickly while indexing catches up, and nothing is ever queued in
memory - at most one page of 100 items exists at a time, and the durable
representation rows written alongside it are the work queue.

Every traversal is idempotent. Events are keyed by their Component 4 id, so a
restarted backfill, an overlapping poll, and a full reconciliation can all
rediscover the same event without creating a duplicate result or resetting
committed work.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Final

from app.config import Settings
from app.domain.models import UpstreamState, utc_now
from app.integrations.component4 import (
    Component4Client,
    InvalidCursor,
    UpstreamContractError,
    UpstreamError,
)
from app.logging_config import RateLimiter
from app.persistence.repositories import EventRepository, SyncRepository

__all__ = ["DiscoveryService", "PassOutcome", "DISCOVERY_BACKOFF"]

logger = logging.getLogger("semantic.discovery")

#: Discovery recovery ladder, capped at 60 s (PLAN section 12.2).
DISCOVERY_BACKOFF: Final[tuple[int, ...]] = (2, 4, 8, 16, 30, 60)

#: How often a long backfill pauses to pick up brand-new events.
_HEAD_INTERLEAVE_PAGES: Final[int] = 10
_HEAD_INTERLEAVE_SECONDS: Final[float] = 10.0
_MAX_PAGES_PER_PASS: Final[int] = 10_000


@dataclass(frozen=True, slots=True)
class PassOutcome:
    pages: int = 0
    discovered: int = 0
    refreshed: int = 0
    malformed: int = 0
    completed: bool = False
    error_code: str | None = None


class DiscoveryService:
    """Owns the discovery cursors, the high-water mark, and upstream state."""

    def __init__(
        self,
        *,
        client: Component4Client,
        events: EventRepository,
        sync: SyncRepository,
        settings: Settings,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        on_backfill_complete: Callable[[], Awaitable[None]] | None = None,
    ):
        self._client = client
        self._events = events
        self._sync = sync
        self._settings = settings
        self._sleep = sleep or asyncio.sleep
        self._on_backfill_complete = on_backfill_complete
        self._limiter = RateLimiter(60.0)
        self._last_head_check = 0.0
        self._pages_since_head = 0

    # ---- loop ------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """The discovery task. Retries forever; never gives up on the upstream."""
        failures = 0
        while not stop.is_set():
            try:
                outcome = await self.run_once()
            except asyncio.CancelledError:
                raise
            except UpstreamError as exc:
                failures += 1
                delay = DISCOVERY_BACKOFF[min(failures, len(DISCOVERY_BACKOFF)) - 1]
                self._record_failure(exc.code, failures)
                allowed, suppressed = self._limiter.allow("discovery", exc.code)
                if allowed:
                    logger.warning(
                        "discovery_failed",
                        extra={
                            "error_code": exc.code,
                            "failures": failures,
                            "retry_in_s": delay,
                            "suppressed": suppressed,
                        },
                    )
                await self._wait(stop, delay)
                continue
            except Exception:  # noqa: BLE001 - a bug must not kill the loop
                failures += 1
                logger.exception("discovery_error", extra={"failures": failures})
                await self._wait(stop, DISCOVERY_BACKOFF[min(failures, 6) - 1])
                continue

            failures = 0
            self._sync.update(
                upstream_state=UpstreamState.AVAILABLE,
                consecutive_failures=0,
                last_error_code=outcome.error_code,
            )
            if outcome.completed:
                await self._wait(stop, self._settings.semantic_poll_interval_seconds)
            else:
                await self._wait(stop, 0.0)

    async def run_once(self) -> PassOutcome:
        """One unit of discovery work: a backfill slice, or one overlap poll."""
        state = self._sync.read()
        self._sync.update(last_poll_attempt_at=utc_now())
        if not state.backfill_complete:
            return await self.backfill_slice()
        return await self.poll_overlap()

    # ---- backfill --------------------------------------------------------

    async def backfill_slice(self) -> PassOutcome:
        """Traverse historical pages, pausing regularly to pick up new events.

        The cursor is persisted only after its page's metadata has committed, so
        a crash re-reads one page rather than skipping it.
        """
        state = self._sync.read()
        cursor = state.backfill_cursor
        pages = discovered = refreshed = malformed = 0

        while pages < _MAX_PAGES_PER_PASS:
            try:
                page = await self._client.list_events(cursor=cursor)
            except InvalidCursor:
                # An upstream key or configuration change invalidated the stored
                # cursor. Restart this traversal; event ids make it idempotent.
                logger.warning("backfill_cursor_reset")
                self._sync.update(backfill_cursor=None)
                return PassOutcome(pages=pages, error_code="invalid_cursor")
            except UpstreamContractError as exc:
                logger.error("backfill_contract_error", extra={"error_code": exc.code})
                return PassOutcome(pages=pages, error_code=exc.code)

            inserted, updated = self._events.upsert_page(page.events)
            discovered += inserted
            refreshed += updated
            malformed += page.malformed
            pages += 1
            self._pages_since_head += 1
            self._advance_high_water(page.events)
            # Persisted only after the page committed.
            self._sync.update(backfill_cursor=page.next_cursor)
            cursor = page.next_cursor

            if cursor is None:
                self._sync.update(backfill_complete=True, backfill_cursor=None)
                logger.info(
                    "backfill_complete",
                    extra={"pages": pages, "discovered": discovered},
                )
                if self._on_backfill_complete is not None:
                    await self._on_backfill_complete()
                return PassOutcome(pages, discovered, refreshed, malformed, True)

            if self._should_check_head():
                head = await self.poll_overlap()
                discovered += head.discovered
                refreshed += head.refreshed
                return PassOutcome(pages, discovered, refreshed, malformed, False)
            await asyncio.sleep(0)

        return PassOutcome(pages, discovered, refreshed, malformed, False)

    def _should_check_head(self) -> bool:
        loop_time = asyncio.get_running_loop().time()
        if self._pages_since_head >= _HEAD_INTERLEAVE_PAGES or (
            loop_time - self._last_head_check >= _HEAD_INTERLEAVE_SECONDS
        ):
            self._pages_since_head = 0
            self._last_head_check = loop_time
            return True
        return False

    # ---- incremental -----------------------------------------------------

    async def poll_overlap(self) -> PassOutcome:
        """One complete overlap traversal: every page for one fixed `from`.

        The window deliberately reaches back before the high-water mark, so an
        event committed upstream slightly out of timestamp order is still seen.
        """
        state = self._sync.read()
        high_water = state.high_water_crossed_at or self._events.max_crossed_at()
        start = (
            high_water - timedelta(seconds=self._settings.semantic_overlap_seconds)
            if high_water is not None
            else None
        )

        cursor: str | None = None
        pages = discovered = refreshed = malformed = 0
        newest: datetime | None = None
        while pages < _MAX_PAGES_PER_PASS:
            try:
                page = await self._client.list_events(cursor=cursor, from_=start)
            except InvalidCursor:
                # This pass's cursor only; the traversal restarts from its start.
                logger.warning("overlap_cursor_reset")
                return PassOutcome(pages=pages, error_code="invalid_cursor")
            except UpstreamContractError as exc:
                logger.error("overlap_contract_error", extra={"error_code": exc.code})
                return PassOutcome(pages=pages, error_code=exc.code)

            inserted, updated = self._events.upsert_page(page.events)
            discovered += inserted
            refreshed += updated
            malformed += page.malformed
            pages += 1
            for event in page.events:
                if newest is None or event.crossed_at > newest:
                    newest = event.crossed_at
            cursor = page.next_cursor
            if cursor is None:
                break
            if start is None:
                # No high water yet and no `from` filter: one head page is
                # enough; the backfill owns the rest of the history.
                break
            await asyncio.sleep(0)

        if newest is not None:
            self._sync.advance_high_water(newest)
        # Recorded only after the complete overlap pass.
        self._sync.update(last_successful_poll_at=utc_now())
        if discovered:
            logger.info(
                "poll_discovered",
                extra={"discovered": discovered, "pages": pages, "refreshed": refreshed},
            )
        return PassOutcome(pages, discovered, refreshed, malformed, True)

    # ---- helpers ---------------------------------------------------------

    def _advance_high_water(self, events) -> None:
        newest = None
        for event in events:
            if newest is None or event.crossed_at > newest:
                newest = event.crossed_at
        if newest is not None:
            self._sync.advance_high_water(newest)

    def _record_failure(self, code: str, failures: int) -> None:
        # A failed poll never deletes or invalidates local metadata or vectors.
        self._sync.update(
            upstream_state=UpstreamState.UNAVAILABLE,
            consecutive_failures=failures,
            last_error_code=code,
            next_poll_at=utc_now()
            + timedelta(seconds=DISCOVERY_BACKOFF[min(failures, 6) - 1]),
        )

    async def _wait(self, stop: asyncio.Event, seconds: float) -> None:
        """Sleep, but wake immediately on shutdown."""
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except (TimeoutError, asyncio.TimeoutError):
            return
