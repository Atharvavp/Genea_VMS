"""Integration tier: real GStreamer, real fixtures, inside the built image."""

from __future__ import annotations

import pathlib
import time

import pytest

from app.analytics.gst_pipeline import GstPipeline


def pytest_collection_modifyitems(items):
    """Mark only the tests that live under this directory.

    A conftest hook receives the whole session's item list, not just the items
    below it, so an unguarded ``add_marker`` here would tag every test in the
    repository and silently break ``-m <tier>`` selection.
    """
    here = pathlib.Path(__file__).parent
    for item in items:
        path = pathlib.Path(str(getattr(item, "fspath", "")))
        if here == path.parent or here in path.parents:
            item.add_marker(pytest.mark.integration)



def wait_for_path(rtsp_base: str, path: str, timeout: float = 40.0) -> None:
    """Block until the fixture RTSP path is actually being published.

    ``ffmpeg`` needs a moment to connect and publish after the control endpoint
    returns, and a pipeline built before then simply fails to resolve the path.
    A short probe pipeline is used because it works for every codec: reaching
    ``pad-added`` proves the path exists, whether or not this build can decode it.
    """
    deadline = time.monotonic() + timeout
    last = "no attempt"
    while time.monotonic() < deadline:
        probe = GstPipeline(
            f"{rtsp_base}/{path}", camera_id="acam_00000000", tcp_timeout_us=2_000_000
        )
        try:
            probe.build()
            if probe.start() is None:
                inner = time.monotonic() + 6.0
                while time.monotonic() < inner:
                    probe.try_pull_frame()
                    error = probe.drain_bus()
                    seen, _linked, _encoding, _link_error = probe.pad_state()
                    if seen:
                        return
                    if error is not None:
                        last = f"{error.outcome}: {error.code}"
                        break
        finally:
            probe.stop()
        time.sleep(0.5)
    raise AssertionError(f"fixture path {path} never became available ({last})")


@pytest.fixture
def stream(publisher, rtsp_base):
    """Start a fixture stream and wait until it is genuinely publishing."""

    def _start(path: str, **spec) -> str:
        publisher.start(path, **spec)
        wait_for_path(rtsp_base, path)
        return f"{rtsp_base}/{path}"

    return _start
