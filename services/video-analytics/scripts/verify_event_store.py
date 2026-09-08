#!/usr/bin/env python3
"""Read-only consistency report for the analytics event store.

Exits non-zero when the store is not internally consistent: a committed row
whose artifact is missing or unsafe, a leftover temporary directory, or an event
directory with no row. It never deletes or repairs anything.
"""

from __future__ import annotations

import argparse
import os
import stat as stat_module
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.models import CAMERA_ID_RE, EVENT_ID_RE  # noqa: E402
from app.persistence.database import Database  # noqa: E402
from app.persistence.event_repository import EventRepository  # noqa: E402
from app.persistence.event_storage import EventStorage  # noqa: E402
from app.security.event_paths import DAY_RE, TEMP_DIR_RE  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the analytics event store")
    parser.add_argument(
        "--data-dir", default=os.environ.get("ANALYTICS_DATA_DIR", "/data")
    )
    parser.add_argument("--db-path", default=os.environ.get("ANALYTICS_DB_PATH"))
    parser.add_argument("--event-dir", default=os.environ.get("ANALYTICS_EVENT_DIR"))
    args = parser.parse_args(argv)

    data_root = Path(args.data_dir)
    db_path = Path(args.db_path) if args.db_path else data_root / "analytics.db"
    event_dir = Path(args.event_dir) if args.event_dir else data_root / "events"

    if not data_root.is_dir():
        print(f"data directory {data_root} does not exist; nothing to verify")
        return 0

    problems: list[str] = []

    if not db_path.is_file():
        print(f"no database at {db_path}; treating the store as empty")
        rows: set[str] = set()
        storage = None
    else:
        database = Database(db_path)
        version = database.initialize()
        repository = EventRepository(database)
        storage = EventStorage(
            data_root=data_root, events_root=event_dir, repository=repository
        )
        rows = repository.all_event_ids()
        print(f"schema_version={version} events={len(rows)}")
        for event_id, snapshot_path, crop_path in repository.iter_paths():
            for kind, relative in (("frame", snapshot_path), ("crop", crop_path)):
                if storage.resolve_image(relative) is None:
                    problems.append(f"missing_or_unsafe_artifact event={event_id} kind={kind}")

    temp_dirs = 0
    orphans = 0
    unknown = 0
    if event_dir.is_dir():
        for camera_dir in sorted(event_dir.iterdir()):
            if not _plain_dir(camera_dir) or not CAMERA_ID_RE.fullmatch(camera_dir.name):
                unknown += 1
                problems.append(f"unknown_path {camera_dir.name}")
                continue
            for day_dir in sorted(camera_dir.iterdir()):
                if not _plain_dir(day_dir) or not DAY_RE.fullmatch(day_dir.name):
                    unknown += 1
                    problems.append(f"unknown_path {camera_dir.name}/{day_dir.name}")
                    continue
                for entry in sorted(day_dir.iterdir()):
                    if not _plain_dir(entry):
                        unknown += 1
                        problems.append(f"unknown_path {entry.name}")
                        continue
                    if TEMP_DIR_RE.fullmatch(entry.name):
                        temp_dirs += 1
                        problems.append(f"temp_directory_left {entry.name}")
                        continue
                    if not EVENT_ID_RE.fullmatch(entry.name):
                        unknown += 1
                        problems.append(f"unknown_path {entry.name}")
                        continue
                    if entry.name not in rows:
                        orphans += 1
                        problems.append(f"orphan_event_directory {entry.name}")

    print(
        f"temp_directories={temp_dirs} orphan_directories={orphans} "
        f"unknown_paths={unknown} problems={len(problems)}"
    )
    for problem in problems:
        print(f"  PROBLEM {problem}")
    if problems:
        return 1
    print("event store is consistent")
    return 0


def _plain_dir(path: Path) -> bool:
    try:
        return stat_module.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
