"""The test tiers must stay selectable.

A conftest hook receives the whole session's item list. An unguarded
``add_marker`` in ``tests/<tier>/conftest.py`` therefore tags every test in the
repository, which silently turns ``-m unit`` into "run everything" - including
the integration and browser tiers, which then fail for want of their fixtures.
This is a regression test for exactly that mistake.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def collect(marker: str) -> set[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            marker,
            "tests",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode in (0, 5), result.stdout[-2000:] + result.stderr[-2000:]
    return {
        line.split("::", 1)[0]
        for line in result.stdout.splitlines()
        if "::" in line and line.startswith("tests/")
    }


@pytest.mark.parametrize(
    "marker,directory",
    [("unit", "tests/unit"), ("integration", "tests/integration"), ("e2e", "tests/e2e")],
)
def test_a_tier_marker_selects_only_its_own_directory(marker: str, directory: str):
    selected = collect(marker)
    assert selected, f"marker {marker} selected nothing"
    stray = {path for path in selected if not path.startswith(directory)}
    assert not stray, (
        f"-m {marker} also selected {sorted(stray)}; a conftest is marking items "
        "outside its own directory"
    )


def test_the_four_camera_marker_is_a_strict_subset_of_integration():
    assert collect("four_camera") <= collect("integration")


def test_the_real_model_marker_is_a_strict_subset_of_integration():
    assert collect("real_model") <= collect("integration")
