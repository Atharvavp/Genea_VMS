"""StreamWorker against real media: state machine, reconnect, and isolation."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.analytics.stream_worker import StreamWorker
from app.analytics.types import WorkerRuntime
from app.domain.models import LineDirection, RuntimeState
from app.persistence.database import Database
from app.persistence.event_repository import EventRepository
from app.persistence.event_storage import EventStorage
from tests.fakes import factories
from tests.fakes.detector import FakeDetector

pytestmark = pytest.mark.integration


class RuntimeRecorder:
    """Collects every published runtime snapshot, like the real registry."""

    def __init__(self) -> None:
        self.snapshots: list[WorkerRuntime] = []
        self._lock = threading.Lock()

    def __call__(self, _camera_id: str, _instance_id: str, runtime: WorkerRuntime):
        with self._lock:
            self.snapshots.append(runtime)

    @property
    def states(self) -> list[RuntimeState]:
        with self._lock:
            return [snapshot.state for snapshot in self.snapshots]

    @property
    def sessions(self) -> list[str]:
        with self._lock:
            return [
                snapshot.worker_session_id
                for snapshot in self.snapshots
                if snapshot.worker_session_id
            ]

    def wait_for(self, state: RuntimeState, timeout: float = 40.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if state in self.states:
                return True
            time.sleep(0.1)
        return False

    def latest(self) -> WorkerRuntime | None:
        with self._lock:
            return self.snapshots[-1] if self.snapshots else None


@pytest.fixture
def storage(tmp_path: Path) -> EventStorage:
    data_root = tmp_path / "data"
    (data_root / "events").mkdir(parents=True)
    database = Database(data_root / "analytics.db")
    database.initialize()
    return EventStorage(
        data_root=data_root,
        events_root=data_root / "events",
        repository=EventRepository(database),
    )


def build_worker(url: str, storage: EventStorage, recorder: RuntimeRecorder, **over):
    camera = factories.camera(rtsp_url=url)
    config = factories.worker_config(
        camera_record=camera, line_record=over.pop("line", None)
    )
    sessions = iter(f"ws_{index:032x}" for index in range(100, 400))
    return StreamWorker(
        config=config,
        worker_instance_id="wi_" + "1" * 32,
        detector=over.pop("detector", FakeDetector()),
        event_storage=storage,
        on_runtime=recorder,
        stall_seconds=over.pop("stall_seconds", 8.0),
        session_factory=lambda: next(sessions),
        jitter=lambda _low, _high: 0.0,  # deterministic, zero-delay backoff
        **over,
    )


def stop_worker(worker: StreamWorker, timeout: float = 15.0) -> None:
    worker.stop()
    worker.join(timeout)
    assert not worker.is_alive(), "the worker thread did not exit"


# -- the happy path ---------------------------------------------------------


def test_starting_reaches_running_on_the_first_decoded_frame(stream, storage):
    url = stream("worker_basic", codec="h264")
    recorder = RuntimeRecorder()
    worker = build_worker(url, storage, recorder)
    worker.start()
    try:
        assert recorder.wait_for(RuntimeState.RUNNING)
        states = recorder.states
        assert states[0] is RuntimeState.STARTING
        assert states.index(RuntimeState.RUNNING) > 0
        assert worker.copy_latest_frame() is not None
    finally:
        stop_worker(worker)
    assert recorder.latest().state is RuntimeState.DISABLED


def test_running_persists_without_a_line_or_any_detection(stream, storage):
    url = stream("worker_noline", codec="h264")
    recorder = RuntimeRecorder()
    detector = FakeDetector()
    worker = build_worker(url, storage, recorder, detector=detector)
    worker.start()
    try:
        assert recorder.wait_for(RuntimeState.RUNNING)
        time.sleep(2.0)
        assert recorder.latest().state is RuntimeState.RUNNING
        assert detector.calls == 0, "no line means no inference"
        assert recorder.latest().frames_received > 5
    finally:
        stop_worker(worker)


def test_the_latest_frame_is_an_owned_copy(stream, storage):
    url = stream("worker_frame", codec="h264")
    recorder = RuntimeRecorder()
    worker = build_worker(url, storage, recorder)
    worker.start()
    try:
        assert recorder.wait_for(RuntimeState.RUNNING)
        first = worker.copy_latest_frame()
        assert first is not None
        first.rgb[0, 0, 0] = 42
        second = worker.copy_latest_frame()
        assert second is not None
        assert second.rgb is not first.rgb
        assert first.sequence >= 1
    finally:
        stop_worker(worker)


# -- reconnect --------------------------------------------------------------


def test_losing_the_source_reconnects_with_a_brand_new_session(
    publisher, stream, storage
):
    url = stream("worker_flap", codec="h264")
    recorder = RuntimeRecorder()
    worker = build_worker(url, storage, recorder, stall_seconds=5.0)
    worker.start()
    try:
        assert recorder.wait_for(RuntimeState.RUNNING)
        first_session = recorder.latest().worker_session_id

        publisher.stop("worker_flap")
        assert recorder.wait_for(RuntimeState.RECONNECTING, timeout=40.0)
        reconnecting = next(
            snapshot
            for snapshot in reversed(recorder.snapshots)
            if snapshot.state is RuntimeState.RECONNECTING
        )
        assert reconnecting.reconnect_attempt >= 1
        assert reconnecting.last_error_code

        publisher.start("worker_flap", codec="h264")
        deadline = time.monotonic() + 60
        recovered = None
        while time.monotonic() < deadline:
            latest = recorder.latest()
            if (
                latest.state is RuntimeState.RUNNING
                and latest.worker_session_id != first_session
            ):
                recovered = latest
                break
            time.sleep(0.25)
        assert recovered is not None, "the worker never recovered on a new session"
        assert recovered.reconnect_attempt == 0
        assert recovered.last_error_code is None
        # Tracker and crossing state are never carried across a reconnect.
        assert len(set(recorder.sessions)) >= 2
    finally:
        stop_worker(worker, timeout=20.0)


def test_an_unreachable_source_reconnects_forever_without_a_storm(storage):
    recorder = RuntimeRecorder()
    worker = build_worker(
        "rtsp://127.0.0.1:1/nothing", storage, recorder, stall_seconds=5.0
    )
    worker.start()
    try:
        assert recorder.wait_for(RuntimeState.RECONNECTING, timeout=40.0)
        time.sleep(3.0)
        assert worker.is_alive(), "a recoverable failure must not end the worker"
        attempts = [
            snapshot.reconnect_attempt
            for snapshot in recorder.snapshots
            if snapshot.state is RuntimeState.RECONNECTING
        ]
        assert attempts == sorted(attempts)
        assert attempts[-1] < 200, "the retry loop is spinning far too fast"
    finally:
        stop_worker(worker, timeout=20.0)


def test_stop_interrupts_the_backoff_wait(storage):
    recorder = RuntimeRecorder()
    worker = build_worker(
        "rtsp://127.0.0.1:1/nothing", storage, recorder, stall_seconds=5.0
    )
    worker.jitter = lambda _low, _high: 1.0  # type: ignore[method-assign]
    worker.start()
    try:
        assert recorder.wait_for(RuntimeState.RECONNECTING, timeout=40.0)
        started = time.monotonic()
        worker.stop()
        worker.join(6.0)
        assert not worker.is_alive()
        assert (time.monotonic() - started) < 6.0
    finally:
        if worker.is_alive():  # pragma: no cover
            stop_worker(worker)


def test_stop_interrupts_an_appsink_wait_promptly(stream, storage):
    url = stream("worker_stop", codec="h264")
    recorder = RuntimeRecorder()
    worker = build_worker(url, storage, recorder)
    worker.start()
    try:
        assert recorder.wait_for(RuntimeState.RUNNING)
        started = time.monotonic()
        worker.stop()
        worker.join(6.0)
        assert not worker.is_alive()
        assert (time.monotonic() - started) < 6.0
    finally:
        if worker.is_alive():  # pragma: no cover
            stop_worker(worker)


# -- fatal failures ---------------------------------------------------------


def test_an_unsupported_codec_is_fatal_and_ends_only_this_worker(stream, storage):
    url = stream("worker_h265", codec="h265")
    recorder = RuntimeRecorder()
    worker = build_worker(url, storage, recorder, stall_seconds=6.0)
    worker.start()
    try:
        assert recorder.wait_for(RuntimeState.ERROR, timeout=45.0)
        worker.join(10.0)
        assert not worker.is_alive(), "a fatal failure must end the worker thread"
        final = recorder.latest()
        assert final.state is RuntimeState.ERROR
        assert final.last_error_code == "unsupported_codec"
    finally:
        if worker.is_alive():  # pragma: no cover
            stop_worker(worker)


def test_a_detector_failure_is_fatal_for_that_worker_only(stream, storage):
    from app.analytics.detector import DetectorFailure

    class ExplodingDetector(FakeDetector):
        def try_detect(self, rgb, confidence, enabled_categories):
            raise DetectorFailure("inference exploded")

    url = stream("worker_detfail", codec="h264")
    recorder = RuntimeRecorder()
    worker = build_worker(
        url,
        storage,
        recorder,
        detector=ExplodingDetector(),
        line=factories.line(),
    )
    worker.start()
    try:
        assert recorder.wait_for(RuntimeState.ERROR, timeout=40.0)
        worker.join(10.0)
        assert not worker.is_alive()
        assert recorder.latest().last_error_code == "detector_failed"
    finally:
        if worker.is_alive():  # pragma: no cover
            stop_worker(worker)


def test_an_event_storage_failure_stops_only_that_worker(stream, storage, monkeypatch):
    """A camera whose disk fails must not take the process down with it."""
    from app.persistence.event_storage import EventStorageError

    class BrokenStorage:
        def commit_event(self, _candidate):
            raise EventStorageError("No space left on device")

    detector = FakeDetector(
        script=[[_person_at(x)] for x in range(0, 400, 20)] * 20,
    )
    url = stream("worker_storagefail", codec="h264")
    recorder = RuntimeRecorder()
    worker = build_worker(
        url,
        storage,
        recorder,
        detector=detector,
        # BOTH: the synthetic object sweeps left-to-right, which is B_TO_A for a
        # line drawn top-to-bottom, so a single-direction policy would suppress it.
        line=factories.line(
            a=(0.5, 0.05), b=(0.5, 0.95), direction=LineDirection.BOTH
        ),
    )
    worker._storage = BrokenStorage()  # noqa: SLF001
    worker.start()
    try:
        assert recorder.wait_for(RuntimeState.ERROR, timeout=60.0)
        worker.join(10.0)
        assert not worker.is_alive()
        assert recorder.latest().last_error_code == "event_persistence_failed"
    finally:
        if worker.is_alive():  # pragma: no cover
            stop_worker(worker)


def _person_at(x: float):
    from tests.fakes.detector import person

    return person(float(x), 100.0, size=40.0)


# -- hot configuration ------------------------------------------------------


def test_installing_a_new_session_id_resets_without_rebuilding_the_pipeline(
    stream, storage
):
    url = stream("worker_hotcfg", codec="h264")
    recorder = RuntimeRecorder()
    worker = build_worker(url, storage, recorder)
    worker.start()
    try:
        assert recorder.wait_for(RuntimeState.RUNNING)
        before = recorder.latest()
        replacement = factories.worker_config(
            camera_record=factories.camera(rtsp_url=url, inference_fps=9.0),
            line_record=factories.line(),
            revision=before.applied_config_revision + 1,
            session_id="ws_" + "e" * 32,
        )
        worker.install_config(replacement)

        deadline = time.monotonic() + 20
        applied = None
        while time.monotonic() < deadline:
            latest = recorder.latest()
            if latest.applied_config_revision == replacement.config_revision:
                applied = latest
                break
            time.sleep(0.1)
        assert applied is not None, "the worker never applied the new revision"
        assert applied.worker_session_id == "ws_" + "e" * 32
        assert applied.state is RuntimeState.RUNNING
        assert worker.is_alive(), "a hot config change must not replace the thread"
        assert applied.frames_received >= before.frames_received
    finally:
        stop_worker(worker)
