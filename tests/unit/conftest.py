"""Unit-tier isolation: no ambient Component 4 environment leaks into a test."""

from __future__ import annotations

import os
import pathlib

import pytest

from tests.conftest import ANALYTICS_ENV_PREFIXES


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(ANALYTICS_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)


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
            item.add_marker(pytest.mark.unit)

