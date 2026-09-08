"""Bounded, durable embedding of discovered events.

The indexer turns durable ``pending``/``retryable`` representation rows into
committed vectors. Its guarantees:

* work is claimed for at most ``batch_size`` events at a time and the batch is
  bounded, so memory is bounded regardless of how far behind indexing is;
* crop is embedded before frame, and the model is released between the two, so a
  waiting search never queues behind both halves of an event;
* a committed vector and the index-revision bump share one transaction, and a
  validated snapshot is published after each committed batch;
* three batches favour the newest eligible events and every fourth takes the
  oldest, so live events index promptly without starving the history;
* if a batch fails, each image is retried individually, so one bad event cannot
  block its peers;
* a permanent crop failure removes the event from search; a permanent frame
  failure leaves a searchable crop-only event.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Final

import numpy as np

from app.config import Settings
from app.domain.models import RepresentationKind, utc_now
from app.embeddings.runtime import EmbeddingError, ModelUnavailable
from app.integrations.component4 import (
    Component4Client,
    UpstreamContractError,
    UpstreamError,
    UpstreamGone,
    UpstreamEventNotFound,
)
from app.logging_config import RateLimiter
from app.persistence.repositories import (
    EventRepository,
    RepresentationRepository,
    VectorError,
)
from app.retrieval.snapshot import SnapshotStore, build_snapshot
from app.security.images import ImageRejected, decode_artifact_image
from app.services.inference import BackgroundDeferred, InferenceCoordinator

__all__ = ["IndexerService", "SnapshotRebuilder", "BatchOutcome"]

logger = logging.getLogger("semantic.indexer")

#: Every fourth batch takes the oldest eligible work (PLAN section 12.4).
_OLDEST_EVERY: Final[int] = 4
_IDLE_SLEEP_SECONDS: Final[float] = 1.0
_DEFERRED_SLEEP_SECONDS: Final[float] = 0.05
#: Consecutive model failures before indexing pauses and the model is marked
#: unavailable rather than writing suspect vectors.
_MODEL_FAILURE_THRESHOLD: Final[int] = 5


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    claimed: int = 0
    indexed: int = 0
    retryable: int = 0
    permanent: int = 0
    published: bool = False
    deferred: bool = False


class SnapshotRebuilder:
    """Builds a complete replacement snapshot off-lock and publishes it.

    Vectors that fail validation are excluded from the snapshot and reset to
    ``pending`` in the store, so a corrupt row is repaired rather than served.
    """

    def __init__(
        self,
        *,
        events: EventRepository,
        representations: RepresentationRepository,
        store: SnapshotStore,
    ):
        self._events = events
        self._representations = representations
        self._store = store

    def rebuild(self) -> int:
        """Blocking; callers run it via ``asyncio.to_thread``. Returns revision."""
        vectors, invalid = self._representations.load_indexed_vectors()
        if invalid:
            self._representations.reset_to_pending(invalid, "vector_validation_failed")
            vectors, _ = self._representations.load_indexed_vectors()
        revision = self._representations.index_revision()
        snapshot = build_snapshot(
            events=self._events.all_for_snapshot(),
            vectors=vectors,
            index_revision=revision,
            built_at=utc_now(),
        )
        self._store.publish(snapshot)
        return revision

    async def rebuild_async(self) -> int:
        return await asyncio.to_thread(self.rebuild)


class IndexerService:
    """Claims work, fetches artifacts, embeds them, and commits vectors."""

    def __init__(
        self,
        *,
        client: Component4Client,
        events: EventRepository,
        representations: RepresentationRepository,
        coordinator: InferenceCoordinator,
        rebuilder: SnapshotRebuilder,
        settings: Settings,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ):
        self._client = client
        self._events = events
        self._representations = representations
        self._coordinator = coordinator
        self._rebuilder = rebuilder
        self._settings = settings
        self._sleep = sleep or asyncio.sleep
        self._batch_index = 0
        self._model_failures = 0
        self._limiter = RateLimiter(60.0)
        self.paused_reason: str | None = None

    @property
    def batch_size(self) -> int:
        return self._settings.semantic_index_batch_size

    # ---- loop ------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                outcome = await self.run_batch()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bug must not kill the loop
                logger.exception("indexer_error")
                await self._pause(stop, _IDLE_SLEEP_SECONDS)
                continue
            if outcome.deferred:
                await self._pause(stop, _DEFERRED_SLEEP_SECONDS)
            elif outcome.claimed == 0:
                await self._pause(stop, _IDLE_SLEEP_SECONDS)
            else:
                await asyncio.sleep(0)

    async def run_batch(self) -> BatchOutcome:
        """Claim and process one batch. Returns what happened, for tests."""
        if self.paused_reason is not None and not self._coordinator.model_ready:
            return BatchOutcome(deferred=True)
        if not self._coordinator.model_ready:
            self.paused_reason = "model_unavailable"
            return BatchOutcome(deferred=True)

        newest_first = (self._batch_index % _OLDEST_EVERY) != (_OLDEST_EVERY - 1)
        claimed = await asyncio.to_thread(
            self._representations.claim_batch,
            limit=self.batch_size,
            newest_first=newest_first,
        )
        if not claimed:
            return BatchOutcome()
        self._batch_index += 1

        indexed = retryable = permanent = 0
        deferred = False
        # Crop first: it is what makes the event searchable. The model lock is
        # released between the two kinds, so a search can slip in between.
        for kind in (RepresentationKind.CROP, RepresentationKind.FRAME):
            group = [item for item in claimed if item.kind is kind]
            if not group:
                continue
            result = await self._process_kind(kind, group)
            indexed += result[0]
            retryable += result[1]
            permanent += result[2]
            deferred = deferred or result[3]

        published = False
        if indexed or permanent:
            await self._rebuilder.rebuild_async()
            published = True
        return BatchOutcome(
            claimed=len(claimed),
            indexed=indexed,
            retryable=retryable,
            permanent=permanent,
            published=published,
            deferred=deferred,
        )

    # ---- one kind --------------------------------------------------------

    async def _process_kind(self, kind, group) -> tuple[int, int, int, bool]:
        indexed = retryable = permanent = 0
        images: list = []
        owners: list = []

        for item in group:
            try:
                payload = await self._client.fetch_artifact(item.event_id, kind)
                image = await asyncio.to_thread(
                    decode_artifact_image,
                    payload,
                    max_bytes=self._settings.semantic_upstream_image_max_bytes,
                )
            except (UpstreamGone, UpstreamEventNotFound) as exc:
                self._permanent(item, kind, exc.code)
                permanent += 1
                continue
            except (UpstreamContractError, ImageRejected) as exc:
                # Deterministically bad content: retrying cannot help.
                self._permanent(item, kind, getattr(exc, "code", "invalid_artifact"))
                permanent += 1
                continue
            except UpstreamError as exc:
                self._retry(item, kind, exc.code)
                retryable += 1
                continue
            images.append(image)
            owners.append(item)

        if not images:
            return indexed, retryable, permanent, False

        try:
            vectors = await self._coordinator.embed_background_batch(images)
        except BackgroundDeferred:
            # A search is waiting: give the claims back untouched and retry.
            for item in owners:
                await asyncio.to_thread(
                    self._representations.release_claim, item.event_id, kind
                )
            self._close(images)
            return indexed, retryable, permanent, True
        except Exception as exc:  # noqa: BLE001
            # ANY inference failure, not just the expected ones: a library
            # raising something unforeseen must not leave rows stuck in
            # `indexing` for the rest of the process's life.
            # One failed batch must not poison its peers: retry each image alone.
            single_indexed, single_retryable, single_permanent = await self._retry_individually(
                kind, owners, images, exc
            )
            self._close(images)
            return (
                indexed + single_indexed,
                retryable + single_retryable,
                permanent + single_permanent,
                False,
            )

        self._model_failures = 0
        for item, vector in zip(owners, vectors):
            if self._commit(item, kind, vector):
                indexed += 1
            else:
                retryable += 1
        self._close(images)
        return indexed, retryable, permanent, False

    async def _retry_individually(self, kind, owners, images, batch_error):
        indexed = retryable = permanent = 0
        failures = 0
        for item, image in zip(owners, images):
            try:
                vectors = await self._coordinator.embed_background_batch([image])
            except BackgroundDeferred:
                await asyncio.to_thread(
                    self._representations.release_claim, item.event_id, kind
                )
                continue
            except EmbeddingError as exc:
                # Deterministic non-finite output for this input.
                self._permanent(item, kind, "embedding_invalid")
                permanent += 1
                failures += 1
                logger.warning(
                    "embedding_rejected",
                    extra={"event_id": item.event_id, "kind": str(kind),
                           "reason": type(exc).__name__},
                )
                continue
            except Exception:  # noqa: BLE001 - retryable by default
                self._retry(item, kind, "inference_failed")
                retryable += 1
                failures += 1
                continue
            if self._commit(item, kind, vectors[0]):
                indexed += 1
            else:
                retryable += 1

        self._model_failures += failures
        if self._model_failures >= _MODEL_FAILURE_THRESHOLD and indexed == 0:
            # Widespread failure: pause indexing rather than storing garbage.
            self._coordinator.runtime.mark_unavailable("widespread_inference_failure")
            self.paused_reason = "model_unavailable"
            logger.error(
                "model_marked_unavailable",
                extra={"failures": self._model_failures,
                       "error_type": type(batch_error).__name__},
            )
        return indexed, retryable, permanent

    # ---- outcomes --------------------------------------------------------

    def _commit(self, item, kind, vector: np.ndarray) -> bool:
        try:
            self._representations.mark_indexed(item.event_id, kind, vector)
        except VectorError as exc:
            logger.warning(
                "vector_refused",
                extra={"event_id": item.event_id, "kind": str(kind),
                       "reason": type(exc).__name__},
            )
            self._retry(item, kind, "vector_invalid")
            return False
        return True

    def _retry(self, item, kind, code: str) -> None:
        self._representations.mark_retryable(
            item.event_id,
            kind,
            code,
            attempt=item.attempt_count,
            max_retry_seconds=self._settings.semantic_max_retry_seconds,
        )
        allowed, suppressed = self._limiter.allow("representation", code)
        if allowed:
            logger.warning(
                "representation_retry",
                extra={"error_code": code, "kind": str(kind),
                       "attempt": item.attempt_count, "suppressed": suppressed},
            )

    def _permanent(self, item, kind, code: str) -> None:
        self._representations.mark_permanent_error(item.event_id, kind, code)
        logger.warning(
            "representation_permanent_error",
            extra={"event_id": item.event_id, "kind": str(kind), "error_code": code},
        )

    @staticmethod
    def _close(images) -> None:
        for image in images:
            close = getattr(image, "close", None)
            if callable(close):
                close()

    async def _pause(self, stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except (TimeoutError, asyncio.TimeoutError):
            return
