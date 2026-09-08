"""Measured behaviour at 1k, 5k, and 10k events with two vectors per event.

No SLA is hard-coded from a spike. The one gate here is the acceptance
requirement the plan states: exact steady-state search at 10k events must remain
interactive (sub-second). Everything else is reported for the handoff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.benchmark_index import measure

REPORT = Path(os.environ.get("SEMANTIC_SCALE_REPORT", "/tmp/scale_report.json"))


@pytest.mark.parametrize("count", [1000, 5000, 10000])
def test_index_scale(count, tmp_path, record_property):
    row = measure(count, queries=200, data_dir=tmp_path)
    print("\n" + json.dumps(row, indent=2))
    record_property("scale", row)

    assert row["vectors"] == count * 2
    assert row["invalid_vectors"] == 0
    # The acceptance gate: interactive exact search at scale.
    assert row["search_full_ms"]["p99"] < 1000.0
    assert row["search_filtered_ms"]["p99"] <= row["search_full_ms"]["p99"] * 3

    existing = json.loads(REPORT.read_text()) if REPORT.exists() else []
    existing = [item for item in existing if item["events"] != count] + [row]
    REPORT.write_text(json.dumps(sorted(existing, key=lambda r: r["events"]), indent=2))


def test_process_memory_after_a_10k_snapshot(tmp_path):
    import psutil

    process = psutil.Process()
    before = process.memory_info().rss
    row = measure(10000, queries=20, data_dir=tmp_path)
    after = process.memory_info().rss
    print(
        f"\n10k rss_before={before / 1e6:.1f}MB rss_after={after / 1e6:.1f}MB "
        f"snapshot_vectors={row['snapshot_vector_bytes'] / 1e6:.1f}MB"
    )
    # 20k x 768 float32 is ~61.4 MB of vectors; the snapshot must not be a
    # multiple of that.
    assert row["snapshot_vector_bytes"] < 80 * 1_000_000
