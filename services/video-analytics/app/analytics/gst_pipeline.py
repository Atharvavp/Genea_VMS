"""Programmatic GStreamer RTSP -> appsink pipeline for one camera.

    rtspsrc -> rtph264depay -> h264parse -> avdec_h264
            -> videoconvert -> video/x-raw,format=RGB -> appsink(max=1, drop)

Every method except the ``pad-added`` callback runs on the owning worker
thread. The callback runs on a GStreamer streaming thread and may only inspect
and link its own pad and update fixed-size flags under ``_pad_lock``.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any, Final

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
# GstApp must be imported for PyGObject to bind appsink's pull methods onto the
# element instance; without it `try_pull_sample` is simply not there.
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp, GstVideo  # noqa: E402,F401

from app.analytics.types import (  # noqa: E402
    AttemptOutcome,
    FatalReason,
    FramePacket,
    PipelineError,
)
from app.security.rtsp_url import safe_gst_error, scrub_text  # noqa: E402

__all__ = ["GstPipeline", "REQUIRED_FACTORIES", "ensure_gst_initialized"]

logger = logging.getLogger("analytics.gst")

REQUIRED_FACTORIES: Final[tuple[str, ...]] = (
    "rtspsrc",
    "rtph264depay",
    "h264parse",
    "avdec_h264",
    "videoconvert",
    "appsink",
)

#: ``GstRtsp.RTSPLowerTrans.TCP``. Set numerically so the pipeline does not
#: depend on the GstRtsp typelib being installed.
RTSP_LOWER_TRANS_TCP: Final[int] = 4

_INIT_LOCK = threading.Lock()


def ensure_gst_initialized() -> None:
    with _INIT_LOCK:
        if not Gst.is_initialized():
            Gst.init(None)


class GstPipelineBuildError(RuntimeError):
    """A required element could not be created or linked."""


class GstPipeline:
    """One RTSP pull attempt. A new instance is built for every attempt."""

    def __init__(
        self,
        rtsp_url: str,
        *,
        latency_ms: int = 200,
        tcp_timeout_us: int = 5_000_000,
        pull_timeout_ms: int = 200,
        camera_id: str = "",
    ):
        ensure_gst_initialized()
        self._url = rtsp_url
        self._latency_ms = int(latency_ms)
        self._tcp_timeout_us = int(tcp_timeout_us)
        self._pull_timeout_ns = int(pull_timeout_ms) * Gst.MSECOND
        self._camera_id = camera_id

        self._pipeline: Gst.Pipeline | None = None
        self._source: Gst.Element | None = None
        self._depay: Gst.Element | None = None
        self._sink: Gst.Element | None = None
        self._bus: Gst.Bus | None = None
        self._pad_handler_id: int | None = None

        # Fixed-size flags shared with the GStreamer streaming thread.
        self._pad_lock = threading.Lock()
        self._closing = False
        self._video_pad_seen = False
        self._video_linked = False
        self._unsupported_encoding: str | None = None
        self._pad_link_error: str | None = None
        self._pads_seen = 0
        self._pads_ignored = 0

    # -- construction -------------------------------------------------------

    def build(self) -> None:
        missing = [
            name for name in REQUIRED_FACTORIES if Gst.ElementFactory.find(name) is None
        ]
        if missing:
            raise GstPipelineBuildError(
                f"required GStreamer factories are missing: {', '.join(missing)}"
            )

        pipeline = Gst.Pipeline.new(f"analytics-{self._camera_id or 'camera'}")
        elements: dict[str, Gst.Element] = {}
        for name, factory in (
            ("source", "rtspsrc"),
            ("depay", "rtph264depay"),
            ("parser", "h264parse"),
            ("decoder", "avdec_h264"),
            ("convert", "videoconvert"),
            ("rgbcaps", "capsfilter"),
            ("sink", "appsink"),
        ):
            element = Gst.ElementFactory.make(factory, name)
            if element is None:
                raise GstPipelineBuildError(f"element factory {factory} returned None")
            elements[name] = element
            if not pipeline.add(element):
                raise GstPipelineBuildError(f"element {name} could not be added")

        source = elements["source"]
        source.set_property("location", self._url)
        source.set_property("protocols", RTSP_LOWER_TRANS_TCP)
        source.set_property("latency", self._latency_ms)
        source.set_property("drop-on-latency", True)
        source.set_property("tcp-timeout", self._tcp_timeout_us)

        elements["rgbcaps"].set_property(
            "caps", Gst.Caps.from_string("video/x-raw,format=RGB")
        )

        sink = elements["sink"]
        sink.set_property("emit-signals", False)
        sink.set_property("sync", False)
        sink.set_property("max-buffers", 1)
        sink.set_property("drop", True)
        sink.set_property("wait-on-eos", False)

        chain = ("depay", "parser", "decoder", "convert", "rgbcaps", "sink")
        for left, right in zip(chain, chain[1:]):
            if not elements[left].link(elements[right]):
                raise GstPipelineBuildError(f"static link {left} -> {right} failed")

        self._pad_handler_id = source.connect("pad-added", self._on_pad_added)

        self._pipeline = pipeline
        self._source = source
        self._depay = elements["depay"]
        self._sink = sink
        self._bus = pipeline.get_bus()

    def start(self) -> PipelineError | None:
        """Request PLAYING. A refusal is a recoverable attempt failure."""
        if self._pipeline is None:
            raise GstPipelineBuildError("pipeline was not built")
        result = self._pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            return PipelineError(
                outcome=AttemptOutcome.SOURCE_START_FAILED,
                code="pipeline_play_refused",
                message="GStreamer refused to move the pipeline to PLAYING.",
            )
        return None

    # -- dynamic pads -------------------------------------------------------

    def _on_pad_added(self, _source: Gst.Element, pad: Gst.Pad) -> None:
        """Runs on a GStreamer streaming thread. Bounded work only."""
        with self._pad_lock:
            if self._closing:
                return
        try:
            caps = pad.get_current_caps() or pad.query_caps(None)
            if caps is None or caps.get_size() == 0:
                return
            structure = caps.get_structure(0)
            with self._pad_lock:
                self._pads_seen += 1
            if structure.get_name() != "application/x-rtp":
                with self._pad_lock:
                    self._pads_ignored += 1
                return
            media = (structure.get_string("media") or "").lower()
            encoding = (structure.get_string("encoding-name") or "").upper()

            if media != "video":
                with self._pad_lock:
                    self._pads_ignored += 1
                logger.info(
                    "rtsp_pad_ignored",
                    extra={"media": media or "unknown", "encoding": encoding or "unknown"},
                )
                return

            with self._pad_lock:
                self._video_pad_seen = True
                if encoding != "H264":
                    self._unsupported_encoding = encoding or "unknown"
                    return
                if self._video_linked:
                    return

            if self._depay is None:  # pragma: no cover - defensive
                return
            sink_pad = self._depay.get_static_pad("sink")
            if sink_pad is None or sink_pad.is_linked():
                return
            result = pad.link(sink_pad)
            with self._pad_lock:
                if result == Gst.PadLinkReturn.OK:
                    self._video_linked = True
                else:
                    self._pad_link_error = str(result.value_nick or result)
        except Exception as exc:  # noqa: BLE001 - never raise into GStreamer
            with self._pad_lock:
                self._pad_link_error = type(exc).__name__

    def pad_state(self) -> tuple[bool, bool, str | None, str | None]:
        with self._pad_lock:
            return (
                self._video_pad_seen,
                self._video_linked,
                self._unsupported_encoding,
                self._pad_link_error,
            )

    def pad_counts(self) -> tuple[int, int]:
        """(pads seen, pads ignored) - diagnostics for the integration tier."""
        with self._pad_lock:
            return self._pads_seen, self._pads_ignored

    # -- frames -------------------------------------------------------------

    def try_pull_frame(self) -> FramePacket | None:
        """Pull at most one sample, waiting at most ``pull_timeout_ms``.

        Returns ``None`` for a timeout or an unusable sample. Both count toward
        the worker's stall deadline, because neither is a valid frame.
        """
        if self._sink is None:
            return None
        sample = self._sink.try_pull_sample(self._pull_timeout_ns)
        if sample is None:
            return None

        received_at = datetime.now(UTC)
        received_monotonic = _monotonic()

        caps = sample.get_caps()
        buffer = sample.get_buffer()
        if caps is None or buffer is None:
            return None

        info = _video_info_from_caps(caps)
        if info is None:
            return None
        width, height = int(info.width), int(info.height)
        if width <= 1 or height <= 1:
            return None
        if info.finfo is None or info.finfo.format != GstVideo.VideoFormat.RGB:
            return None

        try:
            stride = int(info.stride[0])
            offset = int(info.offset[0])
        except (IndexError, TypeError):
            return None
        row_bytes = width * 3
        if stride < row_bytes or offset < 0:
            return None

        ok, mapinfo = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            rgb = _copy_rgb(mapinfo.data, offset, stride, width, height)
        finally:
            buffer.unmap(mapinfo)
        if rgb is None:
            return None

        pts = buffer.pts
        source_pts_ns = int(pts) if pts != Gst.CLOCK_TIME_NONE else None

        return FramePacket(
            rgb=rgb,
            received_at_utc=received_at.replace(
                microsecond=(received_at.microsecond // 1000) * 1000
            ),
            received_monotonic=received_monotonic,
            source_pts_ns=source_pts_ns,
            width=width,
            height=height,
        )

    # -- bus ----------------------------------------------------------------

    def drain_bus(self) -> PipelineError | None:
        """Non-blocking drain. Returns the first terminating message, if any."""
        if self._bus is None:
            return None
        terminal: PipelineError | None = None
        mask = (
            Gst.MessageType.ERROR
            | Gst.MessageType.EOS
            | Gst.MessageType.WARNING
            | Gst.MessageType.STATE_CHANGED
        )
        while True:
            message = self._bus.pop_filtered(mask)
            if message is None:
                return terminal
            if message.type == Gst.MessageType.ERROR and terminal is None:
                error, debug = message.parse_error()
                code, text = safe_gst_error(error, debug, known_urls=(self._url,))
                terminal = PipelineError(
                    outcome=AttemptOutcome.SOURCE_ERROR, code=code, message=text
                )
            elif message.type == Gst.MessageType.EOS and terminal is None:
                terminal = PipelineError(
                    outcome=AttemptOutcome.SOURCE_EOS,
                    code="source_eos",
                    message="The RTSP source signalled end of stream.",
                )
            elif message.type == Gst.MessageType.WARNING:
                error, debug = message.parse_warning()
                code, text = safe_gst_error(error, debug, known_urls=(self._url,))
                logger.debug("gst_warning", extra={"error_code": code, "detail": text})

    def unsupported_codec_error(self) -> PipelineError | None:
        """Fatal only when a video pad exists and it is not H.264."""
        seen, linked, encoding, link_error = self.pad_state()
        if seen and encoding and not linked:
            return PipelineError(
                outcome=None,
                code="unsupported_codec",
                message=(
                    "The RTSP source offers a video encoding this build cannot "
                    f"decode ({scrub_text(encoding)[:32]}); only H.264 is supported."
                ),
                fatal_reason=FatalReason.UNSUPPORTED_CODEC,
            )
        if link_error and not linked:
            return PipelineError(
                outcome=AttemptOutcome.SOURCE_ERROR,
                code="pad_link_failed",
                message=f"The H.264 video pad could not be linked ({link_error}).",
            )
        return None

    # -- teardown -----------------------------------------------------------

    def stop(self) -> None:
        """Always safe to call, including on a pipeline that never started."""
        with self._pad_lock:
            self._closing = True
        source, pipeline = self._source, self._pipeline
        if source is not None and self._pad_handler_id is not None:
            try:
                source.disconnect(self._pad_handler_id)
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.debug("gst_pad_disconnect_failed")
            self._pad_handler_id = None
        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
                result, _current, _pending = pipeline.get_state(2 * Gst.SECOND)
                if result != Gst.StateChangeReturn.SUCCESS:
                    logger.warning(
                        "gst_teardown_slow",
                        extra={
                            "camera_id": self._camera_id,
                            "state_change": str(result.value_nick or result),
                        },
                    )
            except Exception:  # noqa: BLE001
                logger.warning("gst_teardown_failed", extra={"camera_id": self._camera_id})
        self._pipeline = None
        self._source = None
        self._depay = None
        self._sink = None
        self._bus = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _monotonic() -> float:
    import time

    return time.monotonic()


def _video_info_from_caps(caps: Gst.Caps) -> Any:
    """``VideoInfo`` across the two PyGObject API shapes."""
    factory = getattr(GstVideo.VideoInfo, "new_from_caps", None)
    if factory is not None:
        try:
            return factory(caps)
        except Exception:  # noqa: BLE001
            return None
    info = GstVideo.VideoInfo()
    try:
        return info if info.from_caps(caps) else None
    except Exception:  # noqa: BLE001
        return None


def _copy_rgb(data: bytes, offset: int, stride: int, width: int, height: int):
    """Owned, C-contiguous ``(H, W, 3)`` uint8 copy honouring plane stride."""
    import numpy as np

    raw = np.frombuffer(data, dtype=np.uint8)
    row_bytes = width * 3
    full_span = offset + stride * height
    minimum_span = offset + stride * (height - 1) + row_bytes
    if raw.size >= full_span:
        block = raw[offset:full_span].reshape(height, stride)[:, :row_bytes]
        # `.copy()` is mandatory, not defensive tidiness. `np.frombuffer` returns
        # a read-only view of the mapped GStreamer buffer, and when stride equals
        # the visible row width the slice above is already contiguous, so
        # `ascontiguousarray` alone would hand back that view - which becomes a
        # dangling reference the moment the caller unmaps the buffer.
        return np.ascontiguousarray(block).reshape(height, width, 3).copy()
    if raw.size < minimum_span:
        return None
    # The final row is truncated to its visible bytes; copy row by row.
    out = np.empty((height, row_bytes), dtype=np.uint8)
    for row in range(height):
        start = offset + row * stride
        out[row] = raw[start : start + row_bytes]
    return out.reshape(height, width, 3)
