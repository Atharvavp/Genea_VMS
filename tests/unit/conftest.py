"""Mark only the tests beneath this directory.

``pytest_collection_modifyitems`` receives the whole session's item list, not
only the items under this conftest, so an unguarded marker here would tag every
test in the repository and turn ``-m unit`` into "run everything".
"""

from __future__ import annotations

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items):
    for item in items:
        if _HERE in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.unit)
