"""One camera = one thread = one pipeline, tracker, and crossing engine.

The worker owns everything on the media path for its camera and shares nothing
with any other worker except the process-wide detector, which it acquires
without blocking. It never touches camera or line rows, never mutates desired
state, never performs HTTP, and never schedules work onto the event loop: it
publishes bounded immutable runtime snapshots through a callback instead.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, Final

from app.analytics.crossing import CrossingEngine, TrackerStateOverflow
from app.analytics.detector import Detector, DetectorFailure, DetectorUnavailable
from app.analytics.gst_pipeline import GstPipeline, GstPipelineBuildError
from app.analytics.sampling import FrameSampler
from app.analytics.tracker import ByteTrackAdapter, TrackerInvariantError
from app.analytics.types import (
    AttemptOutcome,
    CommitOutcome,
    EventCandidate,
    FatalReason,
    FramePacket,
    LatestFrame,
    PipelineError,
    WorkerConfig,
    WorkerRuntime,
)
from app.domain.models import RuntimeState, new_worker_session_id, utc_now
from app.logging_config import RateLimiter
from app.persistence.event_storage import EventStorage, EventStorageError
from app.security.rtsp_url import scrub_text

__all__ = ["StreamWorker", "LatestFrameStore", "RuntimeCallback"]

logger = logging.getLogger("analytics.worker")

MAX_BACKOFF_SECONDS: Final[float] = 30.0
EVENT_RETRY_DELAYS: Final[tuple[float, ...]] = (0.0, 0.05, 0.20)

#: Runtime is published on every state change, and otherwise at most this often.
#: Without a heartbeat the registry - and therefore the Cameras view's frame age
#: and counters - would freeze at the values from the last transition.
RUNTIME_HEARTBEAT_SECONDS: Final[float] = 1.0

RuntimeCallback = Callable[[str, str, WorkerRuntime], None]


class LatestFrameStore:
    """The single most recent decoded frame for one camera.

    There is no deque: a snapshot reader always wants the newest frame, and a
    backlog would only add memory and staleness.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sequence = 0
        self._packet: FramePacket | None = None

    def publish(self, packet: FramePacket) -> int:
        with self._lock:
            self._sequence += 1
            self._packet = packet
            return self._sequence

    def clear(self) -> None:
        with self._lock:
            self._packet = None

    def copy(self, now_monotonic: float | None = None) -> LatestFrame | None:
        """Copy the array under the lock, then encode outside it."""
        with self._lock:
            packet = self._packet
            sequence = self._sequence
        if packet is None:
            return None
        moment = time.monotonic() if now_monotonic is None else now_monotonic
        return LatestFrame(
            sequence=sequence,
            rgb=packet.rgb.copy(),
            received_at_utc=packet.received_at_utc,
            width=packet.width,
            height=packet.height,
            age_seconds=max(0.0, moment - packet.received_monotonic),
        )


class StreamWorker(threading.Thread):
    """Non-daemon worker thread. Exactly one live instance per enabled camera."""

    def __init__(
        self,
        *,
        config: WorkerConfig,
        worker_instance_id: str,
        detector: Detector,
        event_storage: EventStorage,
        on_runtime: RuntimeCallback,
        stall_seconds: float = 10.0,
        pipeline_factory: Callable[[WorkerConfig, str], GstPipeline] | None = None,
        session_factory: Callable[[], str] = new_worker_session_id,
        jitter: Callable[[float, float], float] = random.uniform,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__(
            name=f"analytics-worker-{config.camera_id}", daemon=False
        )
        self._config = config
        self._config_lock = threading.Lock()
        self._pending_config: WorkerConfig | None = None
        self._instance_id = worker_instance_id
        self._detector = detector
        self._storage = event_storage
        self._on_runtime = on_runtime
        self._stall_seconds = float(stall_seconds)
        self._pipeline_factory = pipeline_factory or _default_pipeline_factory
        self._new_session = session_factory
        self._jitter = jitter
        self._clock = clock

        self._stop_event = threading.Event()
        self._frames = LatestFrameStore()
        self._sampler = FrameSampler()
        self._tracker: ByteTrackAdapter | None = None
        self._crossing: CrossingEngine | None = None
        self._session_id: str | None = None
        self._log_limiter = RateLimiter(30.0)
        self._last_publish_monotonic = 0.0

        self._state = RuntimeState.STARTING
        self._stopping = False
        self._reconnect_attempt = 0
        self._applied_revision = config.config_revision
        self._last_frame_at = None
        self._last_inference_at = None
        self._last_event_at = None
        self._last_error: tuple[str, str] | None = None
        self._frames_received = 0
        self._inferences_run = 0
        self._busy_drops = 0
        self._events_recorded = 0

    # -- public, thread-safe surface ----------------------------------------

    @property
    def worker_instance_id(self) -> str:
        return self._instance_id

    @property
    def camera_id(self) -> str:
        return self._config.camera_id

    def stop(self) -> None:
        """Idempotent and non-blocking; only sets an event."""
        self._stop_event.set()

    def install_config(self, config: WorkerConfig) -> None:
        """Hand the worker a new immutable snapshot. Never blocks on media."""
        with self._config_lock:
            self._pending_config = config

    def copy_latest_frame(self) -> LatestFrame | None:
        return self._frames.copy()

    def runtime_snapshot(self) -> WorkerRuntime:
        return self._build_runtime()

    # -- runtime publication ------------------------------------------------

    def _build_runtime(self) -> WorkerRuntime:
        code, message = self._last_error or (None, None)
        return WorkerRuntime(
            camera_id=self._config.camera_id,
            worker_instance_id=self._instance_id,
            worker_session_id=self._session_id,
            state=self._state,
            stopping=self._stopping,
            last_frame_at=self._last_frame_at,
            last_inference_at=self._last_inference_at,
            last_event_at=self._last_event_at,
            reconnect_attempt=self._reconnect_attempt,
            applied_config_revision=self._applied_revision,
            last_error_code=code,
            last_error_message=message,
            updated_at=utc_now(),
            frames_received=self._frames_received,
            inferences_run=self._inferences_run,
            inference_busy_drops=self._busy_drops,
            events_recorded=self._events_recorded,
        )

    def _publish(
        self,
        state: RuntimeState | None = None,
        *,
        error: tuple[str, str] | None = None,
        clear_error: bool = False,
    ) -> None:
        if state is not None:
            self._state = state
        if error is not None:
            self._last_error = error
        elif clear_error:
            self._last_error = None
        self._last_publish_monotonic = self._clock()
        try:
            self._on_runtime(self._config.camera_id, self._instance_id, self._build_runtime())
        except Exception:  # noqa: BLE001 - a callback must never kill the worker
            logger.warning(
                "runtime_publish_failed", extra={"camera_id": self._config.camera_id}
            )

    # -- session lifecycle --------------------------------------------------

    def _begin_session(self, session_id: str) -> None:
        """Only the worker thread mutates tracker and crossing state."""
        config = self._config
        self._session_id = session_id
        self._sampler.reset()
        self._tracker = ByteTrackAdapter(
            inference_fps=config.inference_fps,
            confidence_threshold=config.confidence_threshold,
        )
        line = config.active_line
        self._crossing = (
            CrossingEngine(
                camera_id=config.camera_id, line=line, session_id=session_id
            )
            if line is not None
            else None
        )
        logger.info(
            "worker_session_started",
            extra={
                "camera_id": config.camera_id,
                "worker_instance": self._instance_id,
                "worker_session": session_id,
                "config_revision": config.config_revision,
                "line_configured": line is not None,
            },
        )

    def _apply_pending_config(self) -> None:
        """Adopt a new snapshot atomically at the top of an iteration."""
        with self._config_lock:
            pending = self._pending_config
            self._pending_config = None
        if pending is None:
            return
        previous = self._config
        self._config = pending
        self._applied_revision = pending.config_revision
        if pending.requested_session_id != previous.requested_session_id:
            self._begin_session(pending.requested_session_id)
        elif self._crossing is not None or pending.active_line is not None:
            # Geometry may have changed within the same requested session only
            # for a name-only patch, which never alters the line.
            line = pending.active_line
            if line is None:
                self._crossing = None
            elif self._crossing is None and self._session_id is not None:
                self._crossing = CrossingEngine(
                    camera_id=pending.camera_id,
                    line=line,
                    session_id=self._session_id,
                )
        self._publish()

    # -- main loop ----------------------------------------------------------

    def run(self) -> None:  # noqa: C901 - the attempt loop is intentionally linear
        fatal: PipelineError | None = None
        try:
            self._publish(RuntimeState.STARTING)
            first_session = self._config.requested_session_id
            while not self._stop_event.is_set():
                session_id = (
                    first_session
                    if self._session_id is None
                    else self._new_session()
                )
                first_session = ""
                self._begin_session(session_id)
                outcome = self._run_attempt()
                if outcome is None or outcome.outcome == AttemptOutcome.STOPPED:
                    break
                if outcome.is_fatal:
                    fatal = outcome
                    break
                self._reconnect_attempt += 1
                self._publish(
                    RuntimeState.RECONNECTING,
                    error=(outcome.code, outcome.message),
                )
                allowed, suppressed = self._log_limiter.allow(
                    self._config.camera_id, outcome.code
                )
                if allowed:
                    logger.warning(
                        "worker_source_failed",
                        extra={
                            "camera_id": self._config.camera_id,
                            "error_code": outcome.code,
                            "attempt": self._reconnect_attempt,
                            "suppressed": suppressed,
                            "detail": outcome.message,
                        },
                    )
                if self._stop_event.wait(self._backoff_delay()):
                    break
        except Exception as exc:  # noqa: BLE001
            fatal = PipelineError(
                outcome=None,
                code=FatalReason.INTERNAL_INVARIANT.value,
                message=f"worker failed unexpectedly: {type(exc).__name__}",
                fatal_reason=FatalReason.INTERNAL_INVARIANT,
            )
            logger.exception(
                "worker_internal_error", extra={"camera_id": self._config.camera_id}
            )
        finally:
            self._frames.clear()
            self._tracker = None
            self._crossing = None
            if fatal is not None:
                self._publish(RuntimeState.ERROR, error=(fatal.code, fatal.message))
                logger.error(
                    "worker_fatal",
                    extra={
                        "camera_id": self._config.camera_id,
                        "worker_instance": self._instance_id,
                        "error_code": fatal.code,
                    },
                )
            else:
                self._session_id = None
                self._publish(RuntimeState.DISABLED, clear_error=True)
            logger.info(
                "worker_exit",
                extra={
                    "camera_id": self._config.camera_id,
                    "worker_instance": self._instance_id,
                    "frames": self._frames_received,
                    "inferences": self._inferences_run,
                    "busy_drops": self._busy_drops,
                    "events": self._events_recorded,
                },
            )

    def _backoff_delay(self) -> float:
        index = max(0, self._reconnect_attempt - 1)
        nominal = min(MAX_BACKOFF_SECONDS, float(2**min(index, 20)))
        return max(0.0, nominal * self._jitter(0.8, 1.2))

    def _run_attempt(self) -> PipelineError | None:
        """One complete pipeline lifetime. Returns why it ended."""
        config = self._config
        try:
            pipeline = self._pipeline_factory(config, self._config.camera_id)
            pipeline.build()
        except GstPipelineBuildError as exc:
            return PipelineError(
                outcome=None,
                code=FatalReason.INTERNAL_INVARIANT.value,
                message=scrub_text(str(exc), (config.rtsp_url,))[:300],
                fatal_reason=FatalReason.INTERNAL_INVARIANT,
            )
        except Exception as exc:  # noqa: BLE001
            return PipelineError(
                outcome=AttemptOutcome.SOURCE_START_FAILED,
                code="pipeline_build_failed",
                message=f"the pipeline could not be created ({type(exc).__name__}).",
            )

        try:
            start_error = pipeline.start()
            if start_error is not None:
                return start_error
            return self._pump(pipeline)
        finally:
            pipeline.stop()

    def _pump(self, pipeline: GstPipeline) -> PipelineError | None:
        deadline = self._clock() + self._stall_seconds
        while True:
            if self._stop_event.is_set():
                return PipelineError(
                    outcome=AttemptOutcome.STOPPED, code="stopped", message="stopped"
                )
            self._apply_pending_config()

            packet = pipeline.try_pull_frame()
            bus_error = pipeline.drain_bus()
            codec_error = pipeline.unsupported_codec_error()

            if packet is not None:
                deadline = self._clock() + self._stall_seconds
                self._on_frame(packet)
                if self._stop_event.is_set():
                    return PipelineError(
                        outcome=AttemptOutcome.STOPPED, code="stopped", message="stopped"
                    )
                fatal = self._process(packet)
                if fatal is not None:
                    return fatal
            elif self._clock() >= deadline:
                return PipelineError(
                    outcome=AttemptOutcome.SOURCE_STALL,
                    code="source_stall",
                    message=(
                        f"No valid video frame arrived within {self._stall_seconds:g} "
                        "seconds."
                    ),
                )

            if codec_error is not None and codec_error.is_fatal:
                return codec_error
            if bus_error is not None:
                return bus_error
            if codec_error is not None:
                return codec_error

    def _on_frame(self, packet: FramePacket) -> None:
        self._frames.publish(packet)
        self._frames_received += 1
        self._last_frame_at = packet.received_at_utc
        if self._state != RuntimeState.RUNNING:
            self._reconnect_attempt = 0
            self._log_limiter.reset(self._config.camera_id)
            self._publish(RuntimeState.RUNNING, clear_error=True)
            return
        self._reconnect_attempt = 0
        if (self._clock() - self._last_publish_monotonic) >= RUNTIME_HEARTBEAT_SECONDS:
            self._publish()

    # -- analytics ----------------------------------------------------------

    def _process(self, packet: FramePacket) -> PipelineError | None:
        config = self._config
        if not self._sampler.should_process(packet.received_monotonic, config.inference_fps):
            return None
        if self._crossing is None or config.active_line is None:
            # Cadence has advanced; without a line there is nothing to detect for.
            return None

        try:
            batch = self._detector.try_detect(
                packet.rgb, config.confidence_threshold, config.enabled_categories
            )
        except DetectorUnavailable as exc:
            return self._fatal(FatalReason.MODEL_UNAVAILABLE, str(exc))
        except DetectorFailure as exc:
            return self._fatal(FatalReason.DETECTOR_FAILED, str(exc))

        if batch is None:
            self._busy_drops += 1
            return None

        self._inferences_run += 1
        self._last_inference_at = packet.received_at_utc

        tracker = self._tracker
        if tracker is None:  # pragma: no cover - defensive
            return self._fatal(
                FatalReason.INTERNAL_INVARIANT, "worker has no tracker for its session"
            )
        try:
            tracked = tracker.update(
                batch.detections,
                frame=packet.rgb,
                timestamp=packet.received_monotonic,
            )
        except TrackerInvariantError as exc:
            return self._fatal(FatalReason.TRACKER_INVARIANT, str(exc))

        try:
            for item in tracked:
                observation = self._crossing.observe(
                    item.track_id,
                    item.normalized_centroid(packet.width, packet.height),
                    packet.received_monotonic,
                )
                if observation is None:
                    continue
                fatal = self._persist_event(item, observation, packet)
                if fatal is not None:
                    return fatal
            self._crossing.prune(tracker.active_track_ids())
        except TrackerStateOverflow as exc:
            return self._fatal(FatalReason.TRACKER_STATE_OVERFLOW, str(exc))
        return None

    def _persist_event(self, item, observation, packet: FramePacket) -> PipelineError | None:
        config = self._config
        line = config.active_line
        engine = self._crossing
        if line is None or engine is None:  # pragma: no cover - defensive
            return None
        candidate = EventCandidate(
            camera_id=config.camera_id,
            vms_camera_id=config.vms_camera_id,
            camera_name=config.camera_name,
            line_id=line.id,
            line_name=line.name,
            worker_session_id=engine.session_id,
            track_id=item.track_id,
            object_category=item.object_category,
            object_class=item.object_class,
            direction=observation.direction,
            confidence=item.confidence,
            crossed_at=packet.received_at_utc,
            bbox_pixels=(item.x1, item.y1, item.x2, item.y2),
            centroid_normalized=item.normalized_centroid(packet.width, packet.height),
            frame_width=packet.width,
            frame_height=packet.height,
            source_pts_ns=packet.source_pts_ns,
            rgb=packet.rgb,
        )

        last_error = "unknown"
        for attempt, delay in enumerate(EVENT_RETRY_DELAYS):
            if delay and self._stop_event.wait(delay):
                return PipelineError(
                    outcome=AttemptOutcome.STOPPED, code="stopped", message="stopped"
                )
            try:
                result = self._storage.commit_event(candidate)
            except EventStorageError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "event_persist_retry",
                    extra={
                        "camera_id": config.camera_id,
                        "attempt": attempt + 1,
                        "detail": str(exc)[:200],
                    },
                )
                continue
            except Exception as exc:  # noqa: BLE001
                last_error = type(exc).__name__
                continue

            if result.outcome == CommitOutcome.INVALID_CROP:
                # A dropped invalid detection, not a storage failure.
                return None
            engine.mark_persisted(observation)
            if result.outcome == CommitOutcome.INSERTED:
                self._events_recorded += 1
                logger.info(
                    "event_recorded",
                    extra={
                        "camera_id": config.camera_id,
                        "event_id": result.event_id,
                        "worker_session": engine.session_id,
                        "track_id": item.track_id,
                        "direction": observation.direction.value,
                        "object_class": item.object_class,
                    },
                )
            self._last_event_at = result.crossed_at
            self._publish()
            return None

        return self._fatal(
            FatalReason.EVENT_PERSISTENCE_FAILED,
            f"the event could not be persisted after "
            f"{len(EVENT_RETRY_DELAYS)} attempts ({last_error}).",
        )

    def _fatal(self, reason: FatalReason, message: str) -> PipelineError:
        return PipelineError(
            outcome=None,
            code=reason.value,
            message=scrub_text(message, (self._config.rtsp_url,))[:300],
            fatal_reason=reason,
        )


def _default_pipeline_factory(config: WorkerConfig, camera_id: str) -> GstPipeline:
    return GstPipeline(config.rtsp_url, camera_id=camera_id)
