#!/usr/bin/env python3
"""Prepare a writable Ultralytics config directory without network telemetry.

Ultralytics writes ``settings.json`` and ``persistent_cache.json`` into
``YOLO_CONFIG_DIR`` on import. The shipped image keeps every application
directory read-only, so a seed copy is baked at build time and copied into
``/tmp`` by the detector before the library is imported.

``--harden`` is the build-time half: it disables the library's telemetry sync
so the analytics service makes no outbound call of its own.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

DEFAULT_SEED = "/opt/ultralytics-seed"


def harden(seed_dir: Path) -> int:
    changed = 0
    for path in seed_dir.rglob("settings.json"):
        data = json.loads(path.read_text())
        data["sync"] = False
        data["hub"] = False
        path.write_text(json.dumps(data, indent=2))
        print(f"disabled ultralytics telemetry sync in {path}")
        changed += 1
    return 0 if changed else 1


def seed_config_dir(
    config_dir: str | os.PathLike[str] | None = None,
    seed_dir: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Copy the baked config into ``YOLO_CONFIG_DIR`` if it is not there yet.

    Returns the prepared directory, or ``None`` when there is nothing to seed.
    Never raises: a failure here only means Ultralytics recreates its defaults.
    """
    target = Path(config_dir or os.environ.get("YOLO_CONFIG_DIR") or "")
    source = Path(seed_dir or os.environ.get("ULTRALYTICS_SEED_DIR") or DEFAULT_SEED)
    if not str(target) or not source.is_dir():
        return None
    try:
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            destination = target / item.name
            if destination.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, destination)
            else:
                shutil.copy2(item, destination)
        return target
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harden", metavar="SEED_DIR", default=None)
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args(argv)
    if args.harden:
        return harden(Path(args.harden))
    if args.seed:
        prepared = seed_config_dir()
        print(f"ultralytics config dir: {prepared}")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
