"""Mark only the tests beneath this directory (see tests/unit/conftest.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items):
    for item in items:
        if _HERE in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.real_model)
