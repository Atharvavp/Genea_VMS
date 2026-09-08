"""Fair, bounded access to the one embedding model in the process.

The model is a single CPU resource shared by two very different callers: an
interactive search that a person is waiting on, and a background indexer that
may have thousands of images to work through. The policy (PLAN section 10.2):

* every model call runs off the event loop via ``asyncio.to_thread``;
* a background batch is at most ``batch_size`` images, and the indexer releases
  the model between the crop batch and the frame batch;
* the indexer does not take the model while any foreground request is waiting,
  so a search waits for at most the batch already executing - never for the
  historical backfill;
* foreground waiters are bounded at 16 with a ten-second acquisition timeout,
  and saturation is answered with 429 rather than an unbounded queue.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Final, Sequence

import numpy as np

from app.embeddings.runtime import EmbeddingRuntime

__all__ = ["InferenceCoordinator", "InferenceQueueFull", "BackgroundDeferred"]

logger = logging.getLogger("semantic.inference")

MAX_FOREGROUND_WAITERS: Final[int] = 16
FOREGROUND_TIMEOUT_SECONDS: Final[float] = 10.0


class InferenceQueueFull(RuntimeError):
    """Too many searches are already waiting for the model."""


class BackgroundDeferred(RuntimeError):
    """A foreground request is waiting; the indexer yields this turn."""


class InferenceCoordinator:
    """One model, one lock, foreground priority."""

    def __init__(
        self,
        runtime: EmbeddingRuntime,
        *,
        max_waiters: int = MAX_FOREGROUND_WAITERS,
        timeout_seconds: float = FOREGROUND_TIMEOUT_SECONDS,
    ):
        self._runtime = runtime
        self._lock = asyncio.Lock()
        self._max_waiters = int(max_waiters)
        self._timeout = float(timeout_seconds)
        self._waiting = 0

    @property
    def runtime(self) -> EmbeddingRuntime:
        return self._runtime

    @property
    def foreground_waiting(self) -> int:
        return self._waiting

    @property
    def model_ready(self) -> bool:
        return self._runtime.ready

    # ---- foreground ------------------------------------------------------

    async def embed_text(self, text: str) -> np.ndarray:
        vectors = await self._foreground(self._runtime.embed_texts, [text])
        return vectors[0]

    async def embed_query_image(self, image: Any) -> np.ndarray:
        vectors = await self._foreground(self._runtime.embed_images, [image])
        return vectors[0]

    async def _foreground(
        self, call: Callable[[Sequence[Any]], np.ndarray], payload: Sequence[Any]
    ) -> np.ndarray:
        if self._waiting >= self._max_waiters:
            raise InferenceQueueFull("the inference queue is saturated")
        self._waiting += 1
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self._timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise InferenceQueueFull("timed out waiting for the model") from exc
        finally:
            # Decremented as soon as the wait ends, so a running request no
            # longer counts as a waiter the indexer must yield to.
            self._waiting -= 1
        try:
            return await asyncio.to_thread(call, payload)
        finally:
            self._lock.release()

    # ---- background ------------------------------------------------------

    async def embed_background_batch(self, images: Sequence[Any]) -> np.ndarray:
        """Embed one bounded batch, yielding to any waiting search.

        Raises ``BackgroundDeferred`` before taking the model if a foreground
        request is queued, so the indexer can re-check its work and come back.
        """
        if self._waiting > 0:
            raise BackgroundDeferred("a search is waiting for the model")
        await self._lock.acquire()
        try:
            if self._waiting > 0:
                # A search arrived while we were acquiring: give the turn back.
                raise BackgroundDeferred("a search is waiting for the model")
            return await asyncio.to_thread(self._runtime.embed_images, list(images))
        finally:
            self._lock.release()
