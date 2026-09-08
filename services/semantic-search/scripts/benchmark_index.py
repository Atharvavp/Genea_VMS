#!/usr/bin/env python3
"""Measure the local index at 1k/5k/10k events. Reports numbers; asserts nothing.

The vectors are synthetic (the model is not loaded), because what is being
measured here is the index and the search path: SQLite load and validation,
snapshot construction, exact matrix search, and the memory the snapshot holds.

    python scripts/benchmark_index.py --events 1000 5000 10000
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.models import (  # noqa: E402
    Direction,
    EventRecord,
    ObjectCategory,
    ObjectClass,
    RepresentationKind,
    SearchFilters,
    utc_now,
)
from app.embeddings.manifest import EMBEDDING_DIM  # noqa: E402
from app.persistence.database import Database  # noqa: E402
from app.persistence.repositories import (  # noqa: E402
    EventRepository,
    RepresentationRepository,
)
from app.retrieval.ranking import rank_events  # noqa: E402
from app.retrieval.snapshot import build_snapshot  # noqa: E402

_CLASSES = list(ObjectClass)
_CATEGORY = {
    ObjectClass.PERSON: ObjectCategory.PERSON,
}


def _category(object_class: ObjectClass) -> ObjectCategory:
    return _CATEGORY.get(object_class, ObjectCategory.VEHICLE)


def synthetic_events(count: int) -> list[EventRecord]:
    base = utc_now() - timedelta(seconds=count * 30)
    now = utc_now()
    events = []
    for index in range(count):
        object_class = _CLASSES[index % len(_CLASSES)]
        events.append(
            EventRecord(
                event_id=f"evt_{index:032x}",
                camera_id=f"acam_{index % 10:08x}",
                vms_camera_id=f"cam_{index % 10:08x}",
                camera_name=f"Camera {index % 10}",
                crossed_at=base + timedelta(seconds=index * 30),
                object_category=_category(object_class),
                object_class=object_class,
                direction=Direction.A_TO_B if index % 2 else Direction.B_TO_A,
                discovered_at=now,
                source_last_seen_at=now,
            )
        )
    return events


def unit_matrix(rows: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    raw = rng.standard_normal((rows, EMBEDDING_DIM)).astype(np.float32)
    return np.ascontiguousarray(raw / np.linalg.norm(raw, axis=1, keepdims=True))


def _percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50": round(statistics.median(ordered), 4),
        "p95": round(ordered[int(len(ordered) * 0.95) - 1], 4),
        "p99": round(ordered[int(len(ordered) * 0.99) - 1], 4),
        "max": round(ordered[-1], 4),
    }


def measure(count: int, *, queries: int = 200, data_dir: Path | None = None) -> dict:
    directory = data_dir or Path(tempfile.mkdtemp(prefix="c5-bench-"))
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / f"bench-{count}.db"
    database = Database(db_path)
    database.ensure_parent()
    database.initialize()
    events_repo = EventRepository(database)
    vectors_repo = RepresentationRepository(database)

    events = synthetic_events(count)
    crops = unit_matrix(count, seed=1)
    frames = unit_matrix(count, seed=2)

    started = time.perf_counter()
    for offset in range(0, count, 500):
        events_repo.upsert_page(events[offset : offset + 500])
    insert_metadata_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    for index, event in enumerate(events):
        vectors_repo.mark_indexed(event.event_id, RepresentationKind.CROP, crops[index])
        vectors_repo.mark_indexed(event.event_id, RepresentationKind.FRAME, frames[index])
    insert_vectors_ms = (time.perf_counter() - started) * 1000

    database.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db_bytes = sum(
        Path(str(db_path) + suffix).stat().st_size
        for suffix in ("", "-wal", "-shm")
        if Path(str(db_path) + suffix).exists()
    )

    started = time.perf_counter()
    loaded, invalid = vectors_repo.load_indexed_vectors()
    load_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    all_events = events_repo.all_for_snapshot()
    snapshot = build_snapshot(
        events=all_events,
        vectors=loaded,
        index_revision=vectors_repo.index_revision(),
        built_at=utc_now(),
    )
    build_ms = (time.perf_counter() - started) * 1000

    snapshot_bytes = int(snapshot.crop_matrix.nbytes + snapshot.frame_matrix.nbytes)

    rng = np.random.default_rng(99)
    raw = rng.standard_normal((queries, EMBEDDING_DIM)).astype(np.float32)
    query_vectors = raw / np.linalg.norm(raw, axis=1, keepdims=True)

    full: list[float] = []
    for vector in query_vectors:
        started = time.perf_counter()
        rank_events(snapshot, vector, top_k=20)
        full.append((time.perf_counter() - started) * 1000)

    narrow = SearchFilters(camera_id="acam_00000001")
    filtered: list[float] = []
    for vector in query_vectors:
        started = time.perf_counter()
        rank_events(snapshot, vector, filters=narrow, top_k=20)
        filtered.append((time.perf_counter() - started) * 1000)

    # One incremental commit plus a full snapshot republish.
    extra = synthetic_events(count + 1)[-1:]
    events_repo.upsert_page(extra)
    started = time.perf_counter()
    vectors_repo.mark_indexed(extra[0].event_id, RepresentationKind.CROP, crops[0])
    reloaded, _ = vectors_repo.load_indexed_vectors()
    build_snapshot(
        events=events_repo.all_for_snapshot(),
        vectors=reloaded,
        index_revision=vectors_repo.index_revision(),
        built_at=utc_now(),
    )
    incremental_ms = (time.perf_counter() - started) * 1000

    database.close()
    gc.collect()
    return {
        "events": count,
        "vectors": len(loaded),
        "invalid_vectors": len(invalid),
        "sqlite_bytes": db_bytes,
        "snapshot_vector_bytes": snapshot_bytes,
        "insert_metadata_ms": round(insert_metadata_ms, 1),
        "insert_vectors_ms": round(insert_vectors_ms, 1),
        "sqlite_load_validate_ms": round(load_ms, 1),
        "snapshot_build_ms": round(build_ms, 1),
        "incremental_publish_ms": round(incremental_ms, 1),
        "search_full_ms": _percentiles(full),
        "search_filtered_ms": _percentiles(filtered),
        "candidate_count_filtered": rank_events(
            snapshot, query_vectors[0], filters=narrow, top_k=20
        ).candidate_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, nargs="+", default=[1000, 5000, 10000])
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    results = [measure(count, queries=args.queries) for count in args.events]
    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    for row in results:
        print(
            f"events={row['events']:>6} vectors={row['vectors']:>6} "
            f"sqlite={row['sqlite_bytes'] / 1e6:7.1f}MB "
            f"snapshot={row['snapshot_vector_bytes'] / 1e6:6.1f}MB "
            f"load={row['sqlite_load_validate_ms']:7.1f}ms "
            f"build={row['snapshot_build_ms']:6.1f}ms "
            f"search_p50={row['search_full_ms']['p50']:.3f}ms "
            f"p99={row['search_full_ms']['p99']:.3f}ms "
            f"filtered_p50={row['search_filtered_ms']['p50']:.3f}ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
