-- Component 5 schema, version 1.
--
-- This database holds ONLY Component 5's derived state: mirrored event metadata
-- and embeddings. It is never Component 4's database, is never shared with any
-- other component, and lives on Component 5's own volume.
--
-- Overall event state (searchable/complete/partial/failed) is deliberately NOT
-- stored: it is computed from representation rows, so it cannot drift.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS system_state (
    singleton          INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version     INTEGER NOT NULL,
    model_id           TEXT NOT NULL,
    model_revision     TEXT NOT NULL,
    model_sha256       TEXT NOT NULL,
    preprocessor_id    TEXT NOT NULL,
    embedding_dim      INTEGER NOT NULL,
    embedding_dtype    TEXT NOT NULL,
    index_revision     INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id            TEXT PRIMARY KEY,
    camera_id           TEXT NOT NULL,
    vms_camera_id       TEXT NOT NULL,
    camera_name         TEXT NOT NULL,
    crossed_at          TEXT NOT NULL,
    object_category     TEXT NOT NULL
        CHECK (object_category IN ('person', 'vehicle')),
    object_class        TEXT NOT NULL
        CHECK (object_class IN
          ('person', 'bicycle', 'car', 'motorcycle', 'bus', 'truck')),
    direction           TEXT NOT NULL
        CHECK (direction IN ('A_TO_B', 'B_TO_A')),
    discovered_at       TEXT NOT NULL,
    source_last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS representations (
    event_id             TEXT NOT NULL,
    kind                 TEXT NOT NULL CHECK (kind IN ('crop', 'frame')),
    model_id             TEXT NOT NULL,
    state                TEXT NOT NULL CHECK
      (state IN ('pending', 'indexing', 'indexed',
                 'retryable', 'permanent_error')),
    embedding            BLOB,
    dimension            INTEGER,
    dtype                TEXT,
    l2_norm              REAL,
    embedding_sha256     TEXT,
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    next_retry_at        TEXT,
    last_error_code      TEXT,
    last_error_at        TEXT,
    indexed_at           TEXT,
    updated_at           TEXT NOT NULL,
    PRIMARY KEY (event_id, kind, model_id),
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sync_state (
    singleton                  INTEGER PRIMARY KEY CHECK (singleton = 1),
    backfill_complete          INTEGER NOT NULL DEFAULT 0,
    backfill_cursor            TEXT,
    full_reconcile_cursor      TEXT,
    high_water_crossed_at      TEXT,
    last_poll_attempt_at       TEXT,
    last_successful_poll_at    TEXT,
    last_full_reconcile_at     TEXT,
    upstream_state             TEXT NOT NULL
      CHECK (upstream_state IN ('unknown', 'available', 'unavailable')),
    consecutive_failures       INTEGER NOT NULL DEFAULT 0,
    next_poll_at               TEXT,
    last_error_code            TEXT,
    updated_at                 TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_events_time
    ON events(crossed_at DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS ix_events_camera_time
    ON events(camera_id, crossed_at DESC);
CREATE INDEX IF NOT EXISTS ix_events_category_class_time
    ON events(object_category, object_class, crossed_at DESC);
CREATE INDEX IF NOT EXISTS ix_events_direction_time
    ON events(direction, crossed_at DESC);
CREATE INDEX IF NOT EXISTS ix_representations_work
    ON representations(state, next_retry_at, kind);
