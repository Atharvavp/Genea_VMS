#!/usr/bin/env python3
"""Capture a USER-OWNED evaluation corpus from a running Component 4.

The images this writes belong to whoever owns the Component 4 deployment they
came from. They are written to a gitignored cache directory and are NEVER
committed: only the manifest - identities, digests, labels and expected queries
- is checked in, so the evaluation is reproducible without redistributing
someone else's footage.

    python scripts/capture_eval_set.py --limit 40
    python scripts/capture_eval_set.py --verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CACHE = ROOT / "tests" / "assets" / "cache"
MANIFEST = ROOT / "tests" / "assets" / "semantic_eval_manifest.json"

#: Queries a human would expect to surface each label. Deliberately plain
#: language: no prompt engineering is applied anywhere in the service either.
LABEL_QUERIES = {
    "person": ["person", "a person walking", "a pedestrian"],
    "car": ["car", "a car", "a parked car"],
    "bus": ["bus", "a bus", "a bus on the street"],
    "truck": ["truck", "a truck", "a large truck"],
    "bicycle": ["bicycle", "a bicycle"],
    "motorcycle": ["motorcycle", "a motorcycle"],
}


def capture(base: str, limit: int, per_class: int) -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(timeout=30.0, follow_redirects=False)

    events: list[dict] = []
    cursor = None
    while len(events) < 1000:
        params = {"limit": "100"}
        if cursor:
            params["cursor"] = cursor
        page = client.get(f"{base}/api/events", params=params).json()
        events.extend(page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            break

    chosen: list[dict] = []
    counts: dict[str, int] = {}
    for event in events:
        label = event["object_class"]
        if counts.get(label, 0) >= per_class or len(chosen) >= limit:
            continue
        counts[label] = counts.get(label, 0) + 1
        chosen.append(event)

    entries = []
    for event in chosen:
        record = {
            "event_id": event["id"],
            "camera_id": event["camera_id"],
            "camera_name": event["camera_name"],
            "crossed_at": event["crossed_at"],
            "object_category": event["object_category"],
            "object_class": event["object_class"],
            "direction": event["direction"],
            "detector_confidence": event["confidence"],
            "expected_queries": LABEL_QUERIES.get(event["object_class"], []),
            "files": {},
        }
        for kind in ("crop", "frame"):
            response = client.get(f"{base}/api/events/{event['id']}/{kind}")
            if response.status_code != 200:
                continue
            payload = response.content
            name = f"{event['id']}.{kind}.jpg"
            (CACHE / name).write_bytes(payload)
            record["files"][kind] = {
                "name": name,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        if "crop" in record["files"]:
            entries.append(record)

    manifest = {
        "description": (
            "Evaluation corpus for Component 5's semantic retrieval. The IMAGES "
            "are not committed: they are captured from the operator's own "
            "Component 4 deployment into tests/assets/cache/, which is "
            "gitignored. Only identities, digests, labels and expected queries "
            "live in this file."
        ),
        "source": "Component 4 public HTTP API (GET /api/events/{id}/{crop,frame})",
        "owner": "the operator of the Component 4 deployment the images came from",
        "license": "user-owned; not redistributed with this repository",
        "capture_command": "python scripts/capture_eval_set.py --limit 40",
        "label_source": (
            "Component 4's own detector labels. They are NOT ground truth: the "
            "handoff records that a detector class can itself be wrong."
        ),
        "cache_directory": "tests/assets/cache",
        "events": entries,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"captured {len(entries)} events into {CACHE}")
    print(f"class distribution: {counts}")
    print(f"manifest written: {MANIFEST}")
    return 0


def verify() -> int:
    if not MANIFEST.exists():
        print("no manifest; run without --verify first", file=sys.stderr)
        return 1
    manifest = json.loads(MANIFEST.read_text())
    missing = mismatched = 0
    for event in manifest["events"]:
        for kind, info in event["files"].items():
            path = CACHE / info["name"]
            if not path.is_file():
                missing += 1
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != info["sha256"]:
                mismatched += 1
    print(
        f"manifest events={len(manifest['events'])} missing_files={missing} "
        f"digest_mismatches={mismatched}"
    )
    return 1 if mismatched else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8100")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--per-class", type=int, default=10)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    return verify() if args.verify else capture(args.base, args.limit, args.per_class)


if __name__ == "__main__":
    raise SystemExit(main())
