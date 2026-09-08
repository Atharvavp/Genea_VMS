"""A StreamWorker double that never touches media, GStreamer, or a model."""

from __future__ import annotations

import threading
from typing import Any

from app.analytics.types import LatestFrame, WorkerConfig, WorkerRuntime
from app.domain.models import RuntimeState, utc_now


class FakeWorker:
    """Records lifecycle calls and publishes runtime like the real worker."""

    created: list["FakeWorker"] = []

    def __init__(
        self,
        *,
        config: WorkerConfig,
        worker_instance_id: str,
        detector: Any = None,
        event_storage: Any = None,
        on_runtime=None,
        stall_seconds: float = 10.0,
        start_raises: bool = False,
        hangs_on_stop: bool = False,
        **_extra: Any,
    ):
        self.config = config
        self.worker_instance_id = worker_instance_id
        self.camera_id = config.camera_id
        self._on_runtime = on_runtime
        self._start_raises = start_raises
        self._hangs = hangs_on_stop
        self.started = False
        self.stopped = False
        self.joined_with: list[float] = []
        self.installed: list[WorkerConfig] = []
        self.latest_frame: LatestFrame | None = None
        self.session_id = config.requested_session_id
        self._alive = False
        self._lock = threading.Lock()
        FakeWorker.created.append(self)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._start_raises:
            raise RuntimeError("thread could not be started")
        self.started = True
        self._alive = True
        self._publish(RuntimeState.STARTING)

    def stop(self) -> None:
        self.stopped = True
        if not self._hangs:
            self._alive = False

    def join(self, timeout: float | None = None) -> None:
        self.joined_with.append(timeout if timeout is not None else -1.0)

    def is_alive(self) -> bool:
        return self._alive

    # -- worker surface -----------------------------------------------------

    def install_config(self, config: WorkerConfig) -> None:
        with self._lock:
            self.installed.append(config)
            self.config = config
            self.session_id = config.requested_session_id
        self._publish(RuntimeState.RUNNING)

    def copy_latest_frame(self) -> LatestFrame | None:
        return self.latest_frame

    def become_running(self) -> None:
        self._publish(RuntimeState.RUNNING)

    def exit_late(self) -> None:
        """Simulate a hung worker that finally exits after the join timed out."""
        self._alive = False
        self._publish(RuntimeState.DISABLED)

    def _publish(self, state: RuntimeState) -> None:
        if self._on_runtime is None:
            return
        self._on_runtime(
            self.camera_id,
            self.worker_instance_id,
            WorkerRuntime(
                camera_id=self.camera_id,
                worker_instance_id=self.worker_instance_id,
                worker_session_id=self.session_id,
                state=state,
                stopping=False,
                last_frame_at=None,
                last_inference_at=None,
                last_event_at=None,
                reconnect_attempt=0,
                applied_config_revision=self.config.config_revision,
                last_error_code=None,
                last_error_message=None,
                updated_at=utc_now(),
            ),
        )


def reset() -> None:
    FakeWorker.created.clear()


def factory(**defaults: Any):
    """Build a worker factory that applies ``defaults`` to every worker."""

    def _make(**kwargs: Any) -> FakeWorker:
        merged = dict(defaults)
        merged.update(kwargs)
        return FakeWorker(**merged)

    return _make
