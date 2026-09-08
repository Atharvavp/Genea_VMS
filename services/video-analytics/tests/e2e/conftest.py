"""Browser tier: real Chromium against the deterministic in-process server."""

from __future__ import annotations

import pathlib
from pathlib import Path

import pytest

from tests.fakes import worker as worker_fakes
from tests.fakes.e2e_server import E2EServer


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
            item.add_marker(pytest.mark.e2e)



@pytest.fixture
def server(tmp_path: Path):
    worker_fakes.reset()
    instance = E2EServer(tmp_path / "data")
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()
        worker_fakes.reset()


#: Chromium logs a console "error" for every non-2xx or aborted resource load.
#: Those are protocol states this UI handles deliberately - 409 snapshot-not-ready,
#: 404 line-not-configured, 410 missing artifact, and the simulated outage - not
#: page faults, so they are excluded from the uncaught-error gate.
_RESOURCE_NOISE = ("Failed to load resource",)


@pytest.fixture
def page_errors(page):
    """Fail any test in which the page raised an uncaught script error."""
    errors: list[str] = []

    def _console(message):
        if message.type != "error":
            return
        if any(noise in message.text for noise in _RESOURCE_NOISE):
            return
        errors.append(message.text)

    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    page.on("console", _console)
    yield errors


@pytest.fixture
def ui(page, server, page_errors):
    page.set_default_timeout(15000)
    page.set_viewport_size({"width": 1400, "height": 900})
    yield page
    assert not page_errors, f"the page reported errors: {page_errors}"
