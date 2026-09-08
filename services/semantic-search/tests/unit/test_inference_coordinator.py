"""Foreground priority, bounded waiters, and off-loop execution."""

from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest

from app.embeddings.manifest import EMBEDDING_DIM
from app.services.inference import (
    BackgroundDeferred,
    InferenceCoordinator,
    InferenceQueueFull,
)


class SlowRuntime:
    """A stand-in that records the thread it ran on and how long it held."""

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.threads: list[str] = []
        self.batch_sizes: list[int] = []
        self.ready = True

    def _work(self, items):
        self.threads.append(threading.current_thread().name)
        self.batch_sizes.append(len(items))
        threading.Event().wait(self.delay)
        rng = np.random.default_rng(len(items))
        raw = rng.standard_normal((len(items), EMBEDDING_DIM)).astype(np.float32)
        return raw / np.linalg.norm(raw, axis=1, keepdims=True)

    def embed_images(self, images):
        return self._work(images)

    def embed_texts(self, texts):
        return self._work(texts)


async def test_model_calls_never_run_on_the_event_loop():
    runtime = SlowRuntime()
    coordinator = InferenceCoordinator(runtime)
    loop_thread = threading.current_thread().name
    await coordinator.embed_text("a bus")
    assert runtime.threads and all(name != loop_thread for name in runtime.threads)


async def test_a_text_vector_is_one_normalised_row():
    coordinator = InferenceCoordinator(SlowRuntime(delay=0.0))
    vector = await coordinator.embed_text("a bus")
    assert vector.shape == (EMBEDDING_DIM,)
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-5


async def test_waiters_are_bounded_and_saturation_is_429_not_a_queue():
    coordinator = InferenceCoordinator(SlowRuntime(delay=0.3), max_waiters=2)
    running = asyncio.create_task(coordinator.embed_text("first"))
    await asyncio.sleep(0.05)
    waiting = [asyncio.create_task(coordinator.embed_text(f"q{i}")) for i in range(2)]
    await asyncio.sleep(0.05)
    assert coordinator.foreground_waiting == 2
    with pytest.raises(InferenceQueueFull):
        await coordinator.embed_text("one too many")
    await asyncio.gather(running, *waiting)


async def test_a_slow_model_times_out_as_queue_full_rather_than_hanging():
    coordinator = InferenceCoordinator(SlowRuntime(delay=0.4), timeout_seconds=0.05)
    running = asyncio.create_task(coordinator.embed_text("first"))
    await asyncio.sleep(0.05)
    with pytest.raises(InferenceQueueFull):
        await coordinator.embed_text("second")
    await running


async def test_the_indexer_yields_while_a_search_is_waiting():
    runtime = SlowRuntime(delay=0.3)
    coordinator = InferenceCoordinator(runtime)
    holding = asyncio.create_task(coordinator.embed_text("holding the model"))
    await asyncio.sleep(0.05)
    waiting = asyncio.create_task(coordinator.embed_text("a person is waiting"))
    await asyncio.sleep(0.05)

    with pytest.raises(BackgroundDeferred):
        await coordinator.embed_background_batch(["a", "b", "c", "d"])
    await asyncio.gather(holding, waiting)

    # With nobody waiting, the indexer proceeds.
    vectors = await coordinator.embed_background_batch(["a", "b", "c", "d"])
    assert vectors.shape == (4, EMBEDDING_DIM)
    assert runtime.batch_sizes[-1] == 4


async def test_a_search_never_waits_behind_more_than_the_running_batch():
    runtime = SlowRuntime(delay=0.15)
    coordinator = InferenceCoordinator(runtime)
    background = asyncio.create_task(coordinator.embed_background_batch(["a", "b", "c", "d"]))
    await asyncio.sleep(0.02)
    started = asyncio.get_running_loop().time()
    await coordinator.embed_text("interactive")
    waited = asyncio.get_running_loop().time() - started
    await background
    assert waited < 0.5, f"search waited {waited:.3f}s behind one background batch"
