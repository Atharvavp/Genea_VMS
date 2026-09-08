"""Real RTSP ingestion against a private MediaMTX fixture.

None of this touches the VMS: the fixtures live on the analytics-test network
and are created and destroyed with the test overlay.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
import pytest

from app.analytics.gst_pipeline import (
    REQUIRED_FACTORIES,
    GstPipeline,
    ensure_gst_initialized,
)
from app.analytics.types import AttemptOutcome, FatalReason

pytestmark = pytest.mark.integration

FRAME_TIMEOUT = 30.0


def pull_frames(pipeline: GstPipeline, count: int, timeout: float = FRAME_TIMEOUT):
    frames = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(frames) < count:
        packet = pipeline.try_pull_frame()
        if packet is not None:
            frames.append(packet)
        error = pipeline.drain_bus()
        if error is not None:
            return frames, error
    return frames, None


def wait_for_first_frame(pipeline: GstPipeline, timeout: float = FRAME_TIMEOUT):
    frames, error = pull_frames(pipeline, 1, timeout)
    assert error is None, f"pipeline failed before the first frame: {error}"
    assert frames, "no frame arrived within the timeout"
    return frames[0]


# 1 -------------------------------------------------------------------------


def test_every_required_factory_exists_in_this_image():
    ensure_gst_initialized()
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    missing = [
        name for name in REQUIRED_FACTORIES if Gst.ElementFactory.find(name) is None
    ]
    assert missing == []


# 2 -------------------------------------------------------------------------


def test_h264_over_tcp_produces_owned_rgb_frames(stream):
    url = stream("gst_h264", codec="h264", pattern="moving", width=320, height=240)
    pipeline = GstPipeline(url, camera_id="acam_00000001")
    pipeline.build()
    assert pipeline.start() is None
    try:
        frames, error = pull_frames(pipeline, 10)
        assert error is None, error
        assert len(frames) >= 10
        for packet in frames:
            assert packet.rgb.shape == (240, 320, 3)
            assert packet.rgb.dtype == np.uint8
            assert packet.rgb.flags["C_CONTIGUOUS"]
            assert packet.width == 320 and packet.height == 240
            assert packet.received_at_utc.tzinfo is not None
        # The array is owned: mutating it cannot corrupt GStreamer memory.
        frames[0].rgb[0, 0, 0] = 7
        assert frames[0].rgb[0, 0, 0] == 7

        # The decoded content really tracks the source. The fixture is a white
        # square sweeping across a dark frame with a ~4.5 s period, so the first
        # handful of frames may legitimately arrive before it enters view;
        # sample long enough to see both extremes rather than assuming a phase.
        darkest, brightest = 255, 0
        deadline = time.monotonic() + 12.0
        for packet in frames:
            darkest = min(darkest, int(packet.rgb.min()))
            brightest = max(brightest, int(packet.rgb.max()))
        while time.monotonic() < deadline and not (darkest < 40 and brightest > 200):
            extra = pipeline.try_pull_frame()
            if extra is not None:
                darkest = min(darkest, int(extra.rgb.min()))
                brightest = max(brightest, int(extra.rgb.max()))
            assert pipeline.drain_bus() is None
        assert darkest < 40, f"no dark pixel was ever decoded (min={darkest})"
        assert brightest > 200, f"the moving object never appeared (max={brightest})"
    finally:
        pipeline.stop()


def test_the_receipt_clock_advances_with_real_frames(stream):
    pipeline = GstPipeline(stream("gst_clock", codec="h264"), camera_id="acam_00000001")
    pipeline.build()
    pipeline.start()
    try:
        frames, _error = pull_frames(pipeline, 6)
        assert len(frames) >= 6
        monotonics = [packet.received_monotonic for packet in frames]
        assert monotonics == sorted(monotonics)
        assert monotonics[-1] > monotonics[0]
        assert all(packet.source_pts_ns is not None for packet in frames)
    finally:
        pipeline.stop()


# 3 -------------------------------------------------------------------------


def test_an_audio_pad_is_ignored_while_video_keeps_flowing(stream):
    pipeline = GstPipeline(stream("gst_av", codec="h264", audio=True), camera_id="acam_00000001")
    pipeline.build()
    pipeline.start()
    try:
        frames, error = pull_frames(pipeline, 10)
        assert error is None, error
        assert len(frames) >= 10
        seen, ignored = pipeline.pad_counts()
        assert seen >= 2, "the source should expose an audio pad as well as video"
        assert ignored >= 1, "the audio pad should have been ignored"
        video_seen, linked, unsupported, link_error = pipeline.pad_state()
        assert video_seen and linked
        assert unsupported is None and link_error is None
    finally:
        pipeline.stop()


# 4 -------------------------------------------------------------------------


def test_the_appsink_retains_at_most_one_frame_for_a_slow_consumer(stream):
    """A stalled consumer must be handed the newest frame, not a backlog.

    ``max-buffers=1`` with ``drop=true`` means the sink keeps only the freshest
    retained sample. The observable consequence, measured here, is that after a
    two-second stall the frames that come back are *current*: a burst of
    stall-length history would appear as ~50 immediately available frames.
    """
    import time as _time

    pipeline = GstPipeline(
        stream("gst_slow", codec="h264", fps=25), camera_id="acam_00000001"
    )
    pipeline.build()
    pipeline.start()
    try:
        sink = pipeline._sink  # noqa: SLF001 - asserting the configured policy
        assert sink.get_property("max-buffers") == 1
        assert sink.get_property("drop") is True
        assert sink.get_property("sync") is False

        wait_for_first_frame(pipeline)
        stall_seconds = 2.0
        _time.sleep(stall_seconds)

        # Anything the sink had queued is available immediately; anything beyond
        # that is the live stream continuing at 25 fps.
        window = 0.15
        drained = []
        started = _time.monotonic()
        while _time.monotonic() - started < window:
            packet = pipeline.try_pull_frame()
            if packet is None:
                break
            drained.append(packet)

        assert drained, "no frame was available after the stall"
        buffered_estimate = int(25 * stall_seconds)
        assert len(drained) < buffered_estimate / 4, (
            f"{len(drained)} frames were available within {window:g}s after a "
            f"{stall_seconds:g}s stall; roughly {buffered_estimate} were produced "
            "during it, so the sink is handing over a backlog"
        )
        # The very first frame handed back is current, not two seconds stale.
        assert (_time.monotonic() - drained[0].received_monotonic) < 0.5
    finally:
        pipeline.stop()


# 5 -------------------------------------------------------------------------


def test_stopping_reaches_null_promptly_from_the_owning_thread(stream):
    pipeline = GstPipeline(stream("gst_stop", codec="h264"), camera_id="acam_00000001")
    pipeline.build()
    pipeline.start()
    wait_for_first_frame(pipeline)

    finished = threading.Event()

    def worker():
        pipeline.stop()
        finished.set()

    thread = threading.Thread(target=worker)
    started = time.monotonic()
    thread.start()
    assert finished.wait(2.5), "teardown did not complete within 2.5 seconds"
    thread.join(2.0)
    assert not thread.is_alive()
    assert (time.monotonic() - started) < 2.5


def test_stop_is_safe_on_a_pipeline_that_never_started():
    pipeline = GstPipeline("rtsp://127.0.0.1:1/never", camera_id="acam_00000001")
    pipeline.stop()
    pipeline.build()
    pipeline.stop()


# 6 -------------------------------------------------------------------------


def test_losing_the_publisher_ends_the_attempt_and_a_restart_recovers(
    publisher, rtsp_base, stream
):
    pipeline = GstPipeline(stream("gst_flap", codec="h264"), camera_id="acam_00000001")
    pipeline.build()
    pipeline.start()
    try:
        wait_for_first_frame(pipeline)
        publisher.stop("gst_flap")

        outcome = None
        deadline = time.monotonic() + 25
        last_frame_at = time.monotonic()
        while time.monotonic() < deadline:
            if pipeline.try_pull_frame() is not None:
                last_frame_at = time.monotonic()
            error = pipeline.drain_bus()
            if error is not None:
                outcome = error.outcome
                break
            if time.monotonic() - last_frame_at > 10.0:
                outcome = AttemptOutcome.SOURCE_STALL
                break
        assert outcome in (
            AttemptOutcome.SOURCE_ERROR,
            AttemptOutcome.SOURCE_EOS,
            AttemptOutcome.SOURCE_STALL,
        ), f"losing the source produced {outcome}"
    finally:
        pipeline.stop()

    rebuilt = GstPipeline(stream("gst_flap", codec="h264"), camera_id="acam_00000001")
    rebuilt.build()
    rebuilt.start()
    try:
        assert wait_for_first_frame(rebuilt) is not None
    finally:
        rebuilt.stop()


def test_an_unreachable_source_fails_the_attempt_without_hanging():
    pipeline = GstPipeline(
        "rtsp://127.0.0.1:1/nothing", camera_id="acam_00000001", tcp_timeout_us=2_000_000
    )
    pipeline.build()
    pipeline.start()
    try:
        deadline = time.monotonic() + 20
        error = None
        while time.monotonic() < deadline and error is None:
            pipeline.try_pull_frame()
            error = pipeline.drain_bus()
        assert error is not None
        assert error.outcome is AttemptOutcome.SOURCE_ERROR
    finally:
        pipeline.stop()


# 7 -------------------------------------------------------------------------


def test_an_unsupported_video_codec_is_fatal_not_a_retry_storm(stream):
    pipeline = GstPipeline(stream("gst_h265", codec="h265"), camera_id="acam_00000001")
    pipeline.build()
    pipeline.start()
    try:
        fatal = None
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and fatal is None:
            pipeline.try_pull_frame()
            pipeline.drain_bus()
            candidate = pipeline.unsupported_codec_error()
            if candidate is not None and candidate.is_fatal:
                fatal = candidate
        assert fatal is not None, "an H.265 source must be reported as unsupported"
        assert fatal.fatal_reason is FatalReason.UNSUPPORTED_CODEC
        assert fatal.code == "unsupported_codec"
        assert "H.264" in fatal.message
    finally:
        pipeline.stop()


# 8 -------------------------------------------------------------------------


def test_credentials_never_reach_logs_or_error_messages(caplog):
    secret = "hunter2"
    url = f"rtsp://admin:{secret}@127.0.0.1:1/private"
    pipeline = GstPipeline(url, camera_id="acam_00000001", tcp_timeout_us=2_000_000)
    pipeline.build()
    pipeline.start()
    try:
        with caplog.at_level(logging.DEBUG):
            deadline = time.monotonic() + 20
            error = None
            while time.monotonic() < deadline and error is None:
                pipeline.try_pull_frame()
                error = pipeline.drain_bus()
        assert error is not None
        assert secret not in error.message
        assert secret not in error.code
        assert secret not in caplog.text
        assert "admin" not in error.message
    finally:
        pipeline.stop()
    assert secret not in caplog.text
