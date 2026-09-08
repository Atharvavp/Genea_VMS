#!/usr/bin/env python3
"""Offline, explicit reindex. The ONLY way to reset the derived index.

There is deliberately no HTTP endpoint for this: an unauthenticated service on a
trusted LAN must not expose a destructive operation. The command takes the same
exclusive process lock the service does, so it refuses to run while the service
is up, and it never deletes anything - the existing database is MOVED into a
timestamped quarantine directory and left there for an operator to remove.

    docker compose stop semantic-search
    docker compose run --rm semantic-search \\
      /opt/venv/bin/python -m scripts.reindex --confirm-reset-derived-index
    docker compose up -d semantic-search

Startup then creates a fresh schema and backfills from Component 4 again. No
user-supplied database path is accepted: the target always comes from settings.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.embeddings.manifest import MANIFEST  # noqa: E402
from app.persistence.database import (  # noqa: E402
    Database,
    ProcessLock,
    ProcessLockError,
    quarantine_database,
)


def integrity_report(db_path: Path) -> dict[str, object]:
    """Describe the store as it is now, before anything is moved."""
    if not db_path.exists():
        return {"exists": False}
    report: dict[str, object] = {"exists": True, "bytes": db_path.stat().st_size}
    try:
        connection = sqlite3.connect(str(db_path))
        connection.row_factory = sqlite3.Row
        report["quick_check"] = connection.execute("PRAGMA quick_check").fetchone()[0]
        row = connection.execute(
            "SELECT schema_version, model_id, embedding_dim, index_revision"
            " FROM system_state WHERE singleton = 1"
        ).fetchone()
        if row is not None:
            report.update(
                {
                    "schema_version": row["schema_version"],
                    "stored_model_id": row["model_id"],
                    "embedding_dim": row["embedding_dim"],
                    "index_revision": row["index_revision"],
                }
            )
        report["events"] = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        report["indexed_representations"] = connection.execute(
            "SELECT COUNT(*) FROM representations WHERE state = 'indexed'"
        ).fetchone()[0]
        connection.close()
    except sqlite3.DatabaseError as exc:
        report["error"] = type(exc).__name__
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-reset-derived-index",
        action="store_true",
        help="required; without it the command only reports",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    db_path = settings.semantic_db_path
    report = integrity_report(db_path)

    print(f"active model      : {MANIFEST.model_id}")
    print(f"model revision    : {MANIFEST.revision}")
    print(f"database          : {db_path}")
    for key, value in report.items():
        print(f"  {key:<24}: {value}")

    if not args.confirm_reset_derived_index:
        print(
            "\nNothing was changed. Re-run with --confirm-reset-derived-index to "
            "quarantine this database and rebuild the derived index from "
            "Component 4."
        )
        return 0

    lock = ProcessLock(settings.lock_path)
    try:
        lock.acquire()
    except ProcessLockError:
        print(
            "\nREFUSED: another process owns this data directory. Stop the "
            "semantic-search service first.",
            file=sys.stderr,
        )
        return 2

    try:
        if not report.get("exists"):
            print("\nNo existing database; startup will create and backfill one.")
            return 0
        # Move, never delete: this copy is the only remaining evidence of
        # whatever state is being discarded.
        destination = quarantine_database(db_path, settings.quarantine_dir)
        # Recreate an empty, correctly stamped store so a misconfigured startup
        # cannot mistake "no database" for "no volume".
        fresh = Database(db_path)
        fresh.ensure_parent()
        fresh.initialize()
        fresh.close()
    finally:
        lock.release()

    print(f"\nquarantined       : {destination}")
    print("The quarantined copy is NOT deleted automatically.")
    print("Next: docker compose up -d semantic-search")
    print("Startup will create schema version 1 and backfill from Component 4.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
