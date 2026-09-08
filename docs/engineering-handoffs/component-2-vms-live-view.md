# VMS Live View (Component 2) — Engineering Handoff

> **Historical document — preserved as audit evidence, not re-validated here.**
> This handoff records the implementation and validation of VMS Live View (Component 2) as it was
> carried out in its own branch and working clone. Every command, count, measurement
> and acceptance result below was executed **there**, before the unified-submission
> refactor, and is reproduced unchanged. It is **not** a claim about the unified
> repository.
>
> The implementation it describes now lives at `services/vms/`, imported byte-identically
> from `feat/vms` at `e5d4fea46429cef1900d99bf3a0bb80cf19774ad`.
> Validation actually executed against the unified repository is recorded separately in
> [HANDOFF_unified_submission.md](HANDOFF_unified_submission.md).

---

**Repository:** `Genea_VMS` (VMS clone)
**Component:** VMS Live View (Component 2)
**Branch:** `feat/vms`
**Status:** Implemented, tested, and verified end-to-end in Docker
**Date of handoff:** 2026-09-06
**Document purpose:** a complete, self-contained technical description of what
exists, why it was built that way, what was proven by testing, and what was
deliberately left out — detailed enough for an independent reviewer (human or
LLM) to audit the design and find gaps without reading the source first.

> **How to use this document for a review.** Sections 1–11 describe the system
> as built. Section 12 records exactly what was verified and how. Sections
> 13–15 are the honest parts: known limitations, deliberate non-goals, and open
> questions. A reviewer hunting for weaknesses should start at §13–15 and
> cross-check them against the mechanics in §6–9.

---

## Table of contents

1. [What this system is](#1-what-this-system-is)
2. [Architecture](#2-architecture)
3. [Repository layout and file responsibilities](#3-repository-layout-and-file-responsibilities)
4. [Data model and persistence](#4-data-model-and-persistence)
5. [Configuration](#5-configuration)
6. [Runtime flows, step by step](#6-runtime-flows-step-by-step)
7. [Concurrency and locking model](#7-concurrency-and-locking-model)
8. [The MediaMTX contract](#8-the-mediamtx-contract)
9. [Security posture](#9-security-posture)
10. [REST API reference](#10-rest-api-reference)
11. [Frontend](#11-frontend)
12. [Testing and verification record](#12-testing-and-verification-record)
13. [Known issues and limitations](#13-known-issues-and-limitations)
14. [Deliberate non-goals](#14-deliberate-non-goals)
15. [Open questions and candidate gaps for review](#15-open-questions-and-candidate-gaps-for-review)
16. [Operating the system](#16-operating-the-system)
17. [Appendix: worked examples](#17-appendix-worked-examples)

---

## 1. What this system is

A VMS live-view service. You register an RTSP camera by URL; it is persisted,
handed to a VMS-owned MediaMTX instance to pull, and delivered to browsers over
WebRTC as a grid of live tiles with a larger focused view.

It exists to be the first real VMS functionality on top of a generic RTSP
source: registration, persistence, ingest, browser delivery, health, and
recovery. It is **not** a recorder, and there is no FFmpeg or GStreamer
anywhere in the live path — MediaMTX pulls RTSP and speaks WebRTC, and no media
byte ever passes through the Python process.

**Explicitly not** the RTSP Camera Simulator. That is Component 1, in a
separate clone with its own Compose stack. The VMS has no code-level,
database-level or API-level dependency on it and sees only an RTSP URL.

### Requirements this implements

Built to the Component 2 PRD and an implementation plan derived from it, with
two refinements requested at implementation time:

1. Camera health (`ONLINE`/`OFFLINE`/`UNKNOWN`) is **runtime state derived from
   MediaMTX**, not persisted desired state.
2. Full MediaMTX reconciliation does **not** run on the 2-second poll. Health
   polling handles routine status; reconciliation is targeted on mutations, and
   full at startup, on detected drift, and after MediaMTX recovery.

Both are implemented as stated. Deviations from the plan's letter are listed in
§15 with rationale.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Browser (single page, vanilla JS — no build step)                        │
│   • camera grid, one isolated WebRTC player per tile                     │
│   • focused view (its own player)                                        │
│   • polls /api/cameras and /health every 2 s                             │
└───────────────┬──────────────────────────────────┬───────────────────────┘
                │ HTTP (JSON)                      │ WHEP (HTTP) + WebRTC/ICE
┌───────────────▼──────────────────────────┐   ┌───▼───────────────────────┐
│ FastAPI  (container: vms, port 8090)     │   │ MediaMTX 1.20.1           │
│   api/cameras.py  routes                 │   │ (container: vms-mediamtx) │
│   api/errors.py   one error envelope     │   │   :8554 RTSP    → 8555    │
└───────────────┬──────────────────────────┘   │   :8889 WebRTC  → 8889    │
                │                              │   :8189 ICE/UDP → 8189    │
┌───────────────▼──────────────────────────┐   │   :9997 API (NOT published)│
│ CameraManager                            │   └───▲───────────────────────┘
│   • CRUD + enable/disable                │       │ Control API
│   • reconciliation (startup/targeted/    │───────┘ (compose network only)
│     drift/recovery)                      │
│   • health poll, in memory               │           ┌───────────────────┐
│   • per-camera asyncio.Lock              │           │ RTSP camera       │
│        │                    │            │           │ (real IP camera,  │
│        ▼                    ▼            │◀──────────│  or the simulator │
│  CameraRepository      MediaMTXClient    │   pulls   │  stack, or any    │
│    (SQLite)            (httpx, async)    │   RTSP    │  RTSP server)     │
└──────────────────────────────────────────┘           └───────────────────┘
```

Live media path, in one line:

```
RTSP source → VMS MediaMTX → WebRTC/WHEP → browser
```

### Why these boundaries

| Boundary | Reason |
| --- | --- |
| One FastAPI container serving both API and UI | No Node toolchain needed; one image, one process. Matches the simulator's shape, which was proven to work on this machine. |
| MediaMTX as a separate service | It *is* the ingest and delivery server. Keeping it separate means the VMS can restart without dropping streams, and MediaMTX is pinned to a known version. |
| VMS owns its own MediaMTX | The simulator's MediaMTX represents virtual cameras. Mixing the two would make the VMS depend on Component 1. |
| No media through Python | MediaMTX pulls RTSP and serves WebRTC natively. Inserting FFmpeg would add CPU cost, latency and a supervision problem for no gain. |
| `CameraManager` as the single orchestrator | Persistence and MediaMTX state must stay consistent; splitting that across route handlers is where races and orphaned paths come from. |
| `MediaMTXClient` knows nothing about cameras | It speaks the Control API and sanitises errors. The manager decides what a failure *means*. Both independently testable. |
| SQLite via stdlib `sqlite3` | A handful of rows. No ORM, no migration framework, no extra container. |
| Health in memory, not in SQLite | A persisted health value is stale the moment the process stops, and would have to be distrusted at startup anyway. Deriving it is strictly simpler than storing and invalidating it. |

---

## 3. Repository layout and file responsibilities

```
.
├── docker-compose.yml            vms + vms-mediamtx, volume, ports        (43)
├── Dockerfile                    python:3.12-slim, no ffmpeg              (21)
├── .env.example                  every tunable, with defaults
├── pyproject.toml                deps; pytest markers (integration, e2e)
├── mediamtx/mediamtx.yml         API + RTSP(TCP) + WebRTC, nothing else   (52)
├── README.md                     user-facing docs
├── HANDOFF.md                    this document
├── THIRD_PARTY_NOTICES.md        MIT attribution for the vendored reader
├── app/
│   ├── main.py            (100) app factory, lifespan, /health, static mount,
│   │                            no-store middleware for the UI
│   ├── config.py          ( 69) Settings; managed-path and WHEP URL derivation
│   ├── logging_config.py  ( 27) one log format; quiets httpx
│   ├── api/
│   │   ├── cameras.py     ( 74) routes only; no logic
│   │   └── errors.py      (125) ApiError hierarchy + handlers, one envelope
│   ├── domain/
│   │   └── models.py      (165) Pydantic request/response models, CameraRecord,
│   │                            health enum, id/timestamp helpers
│   ├── persistence/
│   │   ├── database.py    ( 54) connection factory (WAL, busy timeout) + DDL
│   │   └── camera_repository.py (109) row ↔ CameraRecord, CRUD, uniqueness
│   ├── security/
│   │   └── rtsp_url.py    (170) validation, masking, free-text scrubbing
│   ├── services/
│   │   ├── mediamtx_client.py (216) async Control API wrapper, paging, errors
│   │   └── camera_manager.py  (486) orchestration, locking, reconciliation,
│   │                                health poll
│   └── static/
│       ├── index.html     (108) version-stamped assets; dialogs ship hidden
│       ├── app.js         (593) players, tiles, focus view, polling, forms
│       ├── styles.css     (271) grid, fixed-ratio tiles, modal contract
│       └── vendor/mediamtx-reader.js (687) verbatim from MediaMTX v1.20.1
└── tests/                        215 tests (189 default + 15 integration + 11 e2e)
```

Roughly **1,595 lines of Python**, **972 lines of frontend** (excluding the
vendored reader), **2,748 lines of tests**.

### Component contracts

- **`validate_rtsp_url(value) -> ParsedRtspUrl`** — pure, no I/O. Raises
  `InvalidRtspUrl`. Syntax only: an unreachable but well-formed URL is accepted,
  because the PRD wants it surfaced as `OFFLINE` rather than rejected.
- **`sanitize_rtsp_url(value) -> str`** — masks the password as `***`, keeps
  everything else. Falls back to `rtsp://<unparsable-url>` so a malformed value
  can never leak verbatim through an error path.
- **`sanitize_text(text, *urls) -> str`** — strips control characters, replaces
  each supplied URL with its masked form and each embedded password with `***`.
  Everything that renders free text from MediaMTX goes through this.
- **`MediaMTXClient`** — `get_info`, `list_paths`, `get_path`,
  `list_configured_path_names`, `ensure_path(name, rtsp_url)`,
  `delete_path(name)`, `aclose`. Raises `MediaMTXUnavailable` (unreachable),
  `PathNotFound` (404), `PathRejected` (400), `MediaMTXError` (anything else).
  **Never returns a raw Control API payload** — see §8.
- **`CameraRepository`** — synchronous; every call is dispatched to a worker
  thread by the manager (`asyncio.to_thread`).
- **`CameraManager`** — `initialize`, `startup`, `shutdown`, `list_cameras`,
  `get_camera`, `get_health`, `health_report`, `create_camera`, `update_camera`,
  `delete_camera`, `set_enabled`, `reconcile_all`, `refresh_health`.
  `initialize()` is split from `startup()` so tests can drive the poll manually
  instead of racing a background task.

---

## 4. Data model and persistence

### SQLite schema (`/data/vms.db`)

```sql
CREATE TABLE IF NOT EXISTS cameras (
    id            TEXT PRIMARY KEY,   -- cam_<8 hex>
    name          TEXT NOT NULL,
    rtsp_url      TEXT NOT NULL,      -- the real URL, credentials included
    mediamtx_path TEXT NOT NULL,      -- vms_<id>, derived, never user-chosen
    enabled       INTEGER NOT NULL,
    created_at    TEXT NOT NULL,      -- ISO-8601, millisecond precision
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cameras_mediamtx_path ON cameras (mediamtx_path);
CREATE INDEX IF NOT EXISTS ix_cameras_enabled ON cameras (enabled);
```

Seven columns, all desired state. Created with `CREATE TABLE IF NOT EXISTS` at
startup. **There is no migration framework** — see §15.

### What is deliberately *not* persisted

| Not stored | Why |
| --- | --- |
| Health state (`ONLINE`/`OFFLINE`/`UNKNOWN`) | Derivable from MediaMTX in one call, and meaningless once the process stops. Storing it would mean writing a value at startup that must immediately be distrusted. |
| `last_error`, `last_checked_at` | Same reason; they belong to the current poll. |
| Browser player state | Belongs to one browser session, not to the system. |
| MediaMTX path configuration | MediaMTX holds it; SQLite is the source it is rebuilt from. |

`tests/test_repository.py::test_health_is_not_persisted` asserts the health
columns are absent, and
`test_camera_manager.py::test_health_is_not_persisted` asserts that a fresh
manager over the same database starts with `UNKNOWN` and `checked_at: None`.

### Ordering

`GET /api/cameras` returns `ORDER BY created_at, rowid`. The `rowid` tiebreak
matters: timestamps have millisecond precision, and two cameras created inside
one millisecond would otherwise list in an arbitrary order. This was a real bug
found during verification (§12).

### Connection strategy

```python
@contextmanager
def connect(db_path):
    connection = sqlite3.connect(str(db_path), timeout=15.0)   # per operation
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=10000")
    with connection:        # commit on success, rollback on exception
        yield connection
    connection.close()
```

A short-lived connection per operation, opened and closed on the same worker
thread that `asyncio.to_thread` dispatched the call to. A connection is
therefore never shared across threads (the classic `sqlite3` failure mode), WAL
lets reads proceed during a write, the busy timeout absorbs contention, and the
event loop never blocks on disk I/O.

### Storage

| Path | Contents | Ownership |
| --- | --- | --- |
| `/data/vms.db` | camera rows | VMS, in the `vms-data` named volume |

That is the entire storage footprint. No media, no uploads, no recordings.
Registrations survive `docker compose down` / `up`; `down -v` wipes them.

---

## 5. Configuration

Settings come from the environment via `pydantic-settings`. Two distinct
layers, which is worth being precise about:

**Consumed by the application** (`app/config.py`):

| Field / env var | Default | Purpose |
| --- | --- | --- |
| `MEDIAMTX_API_URL` | `http://vms-mediamtx:9997` | Control API, compose network only |
| `MEDIAMTX_TIMEOUT_SECONDS` | `5.0` | per-request timeout |
| `PUBLIC_WEBRTC_HOST` | `localhost` | host advertised to the browser |
| `PUBLIC_WEBRTC_PORT` | `8889` | port advertised to the browser |
| `PUBLIC_WEBRTC_SCHEME` | `http` | |
| `DATABASE_PATH` | `/data/vms.db` | |
| `CAMERA_HEALTH_POLL_SECONDS` | `2.0` | health refresh interval |
| `RECONCILE_MIN_INTERVAL_SECONDS` | `10.0` | floor between drift-triggered reconciles |
| `LOG_LEVEL` | `INFO` | |

**Consumed by Compose only** (they never reach Python): `VMS_HTTP_PORT`,
`VMS_RTSP_PORT`, `VMS_WEBRTC_HTTP_PORT`, `VMS_WEBRTC_ICE_UDP_PORT`. Compose
maps `VMS_WEBRTC_HTTP_PORT` into the container as `PUBLIC_WEBRTC_PORT`, and
`PUBLIC_WEBRTC_HOST` into MediaMTX as `MTX_WEBRTCADDITIONALHOSTS`, so the URL
the API advertises and the ICE candidates MediaMTX gathers cannot drift apart.

### Derived values

```python
settings.mediamtx_path_for("cam_ab12cd34")  # -> "vms_cam_ab12cd34"
settings.is_managed_path("vms_cam_ab12cd34")  # -> True   (prefix "vms_")
settings.whep_url("vms_cam_ab12cd34")
#   -> "http://localhost:8889/vms_cam_ab12cd34/whep"
```

**The internal/public split matters.** The VMS reaches MediaMTX at
`vms-mediamtx:9997` over the Compose network; the browser reaches it at
`localhost:8889` through a published port. These are separate settings.
Deploying so other machines can watch means setting `PUBLIC_WEBRTC_HOST` to the
host's address — it reaches both the advertised `webrtc_url` and MediaMTX's
`webrtcAdditionalHosts`. Leaving it at `localhost` means only the Docker host's
own browser can play video.

### Ports

| Service | Purpose | Container | Host |
| --- | --- | ---: | ---: |
| `vms` | UI + REST API | 8090 | 8090 |
| `vms-mediamtx` | RTSP ingest | 8554 | **8555** |
| `vms-mediamtx` | WebRTC HTTP / WHEP | 8889 | 8889 |
| `vms-mediamtx` | WebRTC ICE | 8189/udp | 8189/udp |
| `vms-mediamtx` | Control API | 9997 | **not published** |

`8555` keeps clear of the simulator's `8554` so both stacks run side by side.
The ICE UDP port is published on the same number it listens on, because an ICE
candidate advertises the port it was gathered from — remapping it would produce
candidates the browser cannot reach.

---

## 6. Runtime flows, step by step

### 6.1 Health state model

Two state models are kept rigorously apart.

**Camera health** — "can the VMS obtain this configured RTSP source?" Derived
from MediaMTX, identical for every viewer:

```
                        MediaMTX reachable, path present, available=true
        ┌──────────────────────────────────────────────────► ONLINE ─┐
        │                                                             │
   UNKNOWN ◄── MediaMTX unreachable / path not configured yet /       │ source
        │      camera disabled                                        │ drops
        │                                                             ▼
        └──────────────────────────────────────────────────── OFFLINE
                        path present, available=false
```

**Browser player state** — "what is happening to *this* browser's WebRTC
session?" `CONNECTING → LIVE`, `LIVE → RECONNECTING → LIVE`, or `ERROR`. It
lives only in the browser and is never sent to the server.

A tile shows both badges, so "the camera is down" and "my connection is down"
are distinguishable at a glance.

### 6.2 Application startup

```
lifespan startup
  → manager.startup()
      → initialize(): ensure /data, CREATE TABLE IF NOT EXISTS
      → create_task(_run_loop())        # does not block startup
  → uvicorn serves immediately
```

`_pending_reconcile` is initialised to `"startup"` in the constructor, so the
loop's first pass performs the full reconcile. Nothing waits on MediaMTX: if it
is down, the UI and API serve normally, every camera reads `UNKNOWN`, and the
system converges the moment MediaMTX answers (verified — §12).

### 6.3 The poll loop

```python
while True:
    if pending_reconcile is not None:
        reason, pending_reconcile = pending_reconcile, None
        await reconcile_all(reason)
    await refresh_health()
    await sleep(CAMERA_HEALTH_POLL_SECONDS)
```

Every exception except `CancelledError` is caught and logged: a poll must never
kill the loop.

### 6.4 `refresh_health` — one call, two jobs

```
read all camera rows (SQLite, worker thread)
GET /v3/paths/list  ─── fails ──► mark every camera UNKNOWN, mediamtx_available=False,
        │                          warn once (later failures log at DEBUG), return
        │ succeeds
        ▼
   was unavailable? ──► log mediamtx_recovered; refresh version;
        │                request reconcile ("mediamtx-recovered", force=True)
        ▼
   for each camera:
       disabled                  → UNKNOWN + "camera is disabled"
       enabled, path missing     → UNKNOWN + drift = True
       enabled, available=true   → ONLINE
       enabled, available=false  → OFFLINE + generic hint
        │
        ▼
   orphans = managed paths in MediaMTX with no enabled owner
   prune health entries for cameras that no longer exist
   if drift or orphans → request reconcile ("drift", rate-limited)
```

The key economy: the response that produces health is also the drift detector.
A MediaMTX restart shows up as "all my paths are gone" without a second API
call to ask whether it restarted.

### 6.5 Reconciliation triggers

| Trigger | Scope | Rate-limited? |
| --- | --- | --- |
| Startup | Full | No |
| Camera created / updated / enabled / disabled / deleted | That camera only | No |
| Drift found by the poll | Full | Yes (`RECONCILE_MIN_INTERVAL_SECONDS`, 10 s) |
| MediaMTX recovery after being unreachable | Full | **No** — it may have restarted empty |

`reconcile_all` itself:

```
under _reconcile_lock:
  pending_before = pending_reconcile          # None when called from the loop
  read all rows; desired = {path: record for enabled rows}
  GET /v3/config/paths/list ─── fails ──► mark all UNKNOWN and return
                                          (recovery detection will re-request)
  delete every `vms_`-prefixed path that is not in `desired`   ← stale cleanup
  for each desired camera id:
      under that camera's lock:
          re-read the row (it may have changed since the list)
          skip if gone or now disabled
          apply it
  last_reconcile_at = now
  clear pending_reconcile *only if* it still equals pending_before
```

That last condition matters: a request raised *during* the pass concerns a
camera this pass already failed on, so swallowing it would delay the retry
until the next drift detection. There is a test for it.

Paths not starting with `vms_` are never touched. Reconciliation cleans up only
what it owns.

### 6.6 Create a camera

1. Validate the payload (`CameraCreate`): name trimmed to 1–100 chars, RTSP URL
   syntactically valid, unknown fields rejected.
2. Generate `cam_<8hex>`; derive `mediamtx_path = vms_cam_<8hex>`.
3. Under that camera's lock: `INSERT` the row. **Desired state is persisted
   first** — a MediaMTX outage must not lose the registration.
4. Log `camera_created` with the *masked* URL.
5. Apply to MediaMTX (§6.9).
6. Respond `201` with the camera view, including whatever health the apply step
   learned — `ONLINE`, `OFFLINE`, or `UNKNOWN` if MediaMTX could not be reached.

### 6.7 Update a camera

```
under the camera's lock:
  merge the provided fields onto the stored record
      (fields absent from the request body are kept — see §10)
  nothing changed?  → return the current view, no writes, no MediaMTX call
  UPDATE the row
  name-only change  → return; MediaMTX is not touched at all
  URL or enabled changed → apply (§6.9)
```

The `mediamtx_path` is derived from the id and never recomputed, so changing a
camera's URL replaces the *contents* of the same path. The `webrtc_url` the
browser is using stays valid and the player merely reconnects. A rename is
invisible to MediaMTX and to every player, including the renamed camera's own.

### 6.8 Delete a camera

```
under the camera's lock:
  read the record (404 if missing)
  DELETE the row
  drop the in-memory health entry
  DELETE the MediaMTX path — best effort
      on failure: note the error, request a reconcile
                  (a `vms_` path with no enabled owner is deleted by the
                   next full pass)
```

### 6.9 `_apply` — one camera's desired state

```
enabled:
    POST /v3/config/paths/replace/{path}   (upsert: create or update)
    GET  /v3/paths/get/{path}              (so the response carries real state,
                                            not UNKNOWN)
    → ONLINE if available else OFFLINE
disabled:
    DELETE /v3/config/paths/delete/{path}  (404 is not an error)
    → UNKNOWN + "camera is disabled"

PathRejected (400):  keep the camera, health OFFLINE + sanitised reason
MediaMTXError:       keep the camera, health UNKNOWN + sanitised reason,
                     request a reconcile
```

Nothing here raises to the API. That is deliberate and is the single most
challengeable decision in the system — see §15.

### 6.10 Shutdown

`lifespan` calls `manager.shutdown()`: cancel the poll task, await it, close
the httpx client. No MediaMTX state is torn down, because MediaMTX paths are
derived state that the next startup reconciles. A VMS restart therefore does
not interrupt any stream — MediaMTX keeps pulling throughout, and browsers
never notice.

---

## 7. Concurrency and locking model

### Locks that exist

| Lock | Scope | Guards |
| --- | --- | --- |
| `CameraManager._locks[camera_id]` | one per camera (`defaultdict`) | every mutation for that camera, and each camera's slice of a reconcile |
| `CameraManager._reconcile_lock` | manager-wide | one full reconcile at a time |

**No global lock is ever held across a mutation.** Creating camera A never
blocks editing camera B.

### Lock ordering, and why there is no deadlock

There is exactly one ordering in the codebase:

```
reconcile_all:  _reconcile_lock  →  _locks[camera_id]   (acquired, released, next camera)
mutations:                          _locks[camera_id]    (only)
```

A mutation never acquires `_reconcile_lock`, so the cycle that would deadlock
cannot form. `_apply` — called from both paths — takes no lock itself; it is
always called by a holder.

### Why a reconcile cannot clobber a concurrent edit

`reconcile_all` reads the row list once to decide *which* cameras to apply, but
then re-reads each row **under that camera's lock** immediately before applying
it. A `PATCH` that lands between the list and the apply is therefore either
already visible (the re-read sees it) or blocked until the apply finishes (and
then applies the new value itself). The stale-list race is closed by
construction rather than by timing.

### Blocking-call discipline

Every SQLite call goes through `await asyncio.to_thread(...)`. Every MediaMTX
call is `httpx`-async. There is no blocking I/O on the event loop thread
anywhere in the request path — unlike the simulator, there are no uploads and
no child processes.

### Contention policy: wait, do not reject

The simulator returns `409 operation_in_progress` when a camera's lock is held,
because its operations take seconds (an FFmpeg startup grace window). Here a
mutation is one HTTP call to a service on the same Docker network, typically a
few milliseconds, so callers simply await the lock. There is no
`operation_in_progress` error and no queue-depth surprise. This is a
defensible-either-way call; see §15.

### Lock lifetime

`_locks` is a `defaultdict`, so a lock is created on first use and dropped when
its camera is deleted. A coroutine already waiting on a deleted camera's lock
holds a reference to the same object, acquires it normally, and then fails its
`_require` check with `CameraNotFound` — which is the correct answer.

---

## 8. The MediaMTX contract

Everything below was **verified empirically** against
`bluenviron/mediamtx:1.20.1`, not read from documentation. The integration
suite re-verifies each claim on every run.

### Endpoints used

| Need | Call | Notes |
| --- | --- | --- |
| Create **or** update a path | `POST /v3/config/paths/replace/{name}` | **Upserts.** One call covers both, and no camera change restarts the server |
| Remove a path | `DELETE /v3/config/paths/delete/{name}` | `404` when already gone → treated as success |
| Which paths are configured | `GET /v3/config/paths/list` | **Echoes every source URL, password included** |
| Runtime state of all paths | `GET /v3/paths/list` | `available` is the health signal |
| Runtime state of one path | `GET /v3/paths/get/{name}` | Used once after an apply |
| Version | `GET /v3/info` | Shown in the header badge |

All list endpoints are paged (`page`, `itemsPerPage`); the client follows
`pageCount` rather than assuming one page.

### Path payload

```json
{"source": "<the RTSP URL>", "sourceOnDemand": false,
 "rtspTransport": "tcp", "record": false}
```

| Field | Why this value |
| --- | --- |
| `sourceOnDemand: false` | Pull always. Health then answers "can the VMS obtain this source?" independently of whether anyone is watching — which is what the grid's status column has to mean. MediaMTX retries an offline source by itself and recovers when it returns. |
| `rtspTransport: tcp` | UDP RTP/RTCP does not survive ordinary Docker port mapping. Matches how the simulator publishes. |
| `record: false` | Stated explicitly so that enabling recording later (Component 3) is a deliberate act, not a default that drifted. |

### Four findings that shaped the design

1. **`replace` upserts.** Verified: replacing a non-existent path returns `200`
   and creates it. So there is no `add`/409/`replace` dance and no separate
   `add_path` method — `ensure_path` is the whole write surface.
2. **`GET /v3/paths/static-sources/get/{name}` does not exist in 1.20.1**
   (`404`). There is therefore no per-source `lastError` to display, and an
   `OFFLINE` camera can only get a generic message. Log scraping was rejected
   as a substitute (§15).
3. **`online` is useless as a health signal.** It stays `true` for a
   configured-but-unreachable static source. `available` is the honest field,
   and is what `ONLINE`/`OFFLINE` maps from.
4. **The config API echoes credentials.** `GET /v3/config/paths/get/{name}`
   returns `"source": "rtsp://admin:hunter2@..."` in clear. This is why
   `list_configured_path_names()` extracts nothing but names, and why an
   integration test asserts the raw response really does contain the password —
   so that the reason for the restriction cannot quietly rot.

### `mediamtx/mediamtx.yml`

```yaml
api: true                    # Control API on :9997, never published to the host
authInternalUsers:           # default grants `api` to 127.0.0.1 only, which
  - user: any                # would lock out the VMS container
    ips: []
    permissions: [publish, read, playback, api]
rtsp: true
rtspTransports: [tcp]
webrtc: true
webrtcLocalUDPAddress: :8189 # one fixed ICE port, so one published UDP port
webrtcAllowOrigins: ["*"]
webrtcAdditionalHosts: [localhost]
rtmp: false
hls: false
srt: false
moq: false                   # on by default in 1.20.1; it generates a cert at boot
paths: {}                    # no `all_others`: nothing can publish, and only
                             # VMS-created paths exist
```

Two details worth flagging to a reviewer:

- **The auth widening is safe only because 9997 is not published.** If anyone
  publishes that port, real credentials must be added first. A test asserts no
  Compose `ports:` entry ends in `:9997`.
- **`paths: {}` with no `all_others` means the VMS MediaMTX accepts no
  publishers at all.** It is a pure pull-and-serve instance. This was discovered
  the hard way: an early integration test tried to publish a test stream into it
  and was refused, which turned out to be the correct behaviour and is now an
  explicit assertion.

---

## 9. Security posture

This is a lab/test tool with **no authentication** (§14). Within that, the
following are enforced:

| Risk | Mitigation |
| --- | --- |
| RTSP credentials in an API response | The raw URL is never serialised. `CameraView` has no field carrying it; `rtsp_url_display` masks the password as `***`. Asserted by API tests and an OpenAPI schema test |
| RTSP credentials in the UI | The edit form leaves a credentialed URL blank and treats an empty field as "keep the stored source". Asserted by a static-UI test and a browser test that greps the rendered page for the password |
| RTSP credentials in logs | Every log line mentioning a URL passes through `sanitize_rtsp_url`. Request bodies are never logged |
| RTSP credentials echoed back by MediaMTX | The client extracts only path names from config responses, and every error message is scrubbed of both the URL and the bare password before it propagates |
| Credentials in validation errors | Pydantic quotes the offending value; `_pydantic_details` sanitises each message. A test posts `http://admin:hunter2@cam/live` and asserts the 422 body has no `hunter2` |
| Log / header injection via a URL | Control characters (C0, DEL, C1) and whitespace are rejected at validation; anything rendered is control-character stripped regardless |
| SSRF-ish scheme abuse | Only `rtsp://` and `rtsps://` are accepted; a host is required; a fragment is rejected; length is capped at 2048 |
| Unmanaged MediaMTX paths | `paths: {}` with no `all_others` — nothing can publish to the VMS MediaMTX, and only VMS-created paths exist |
| Unauthenticated Control API | Port 9997 is not published; only the Compose network reaches it. Asserted by a test that parses `docker-compose.yml` |
| Reconciliation deleting someone else's path | Only names with the `vms_` prefix are ever deleted. A test plants a non-managed path and asserts it survives |
| SQL injection | Parameterised statements only |
| Stale UI after a rebuild | Version-stamped asset URLs plus `Cache-Control: no-store` on `/` and `/static/*` only — `/api/*` and `/health` untouched |

**Credentials are stored unencrypted in SQLite.** That is a deliberate V1 limit
for a trusted local/lab deployment. Encryption or a secret store belongs to a
later security component.

**The WHEP endpoint has no authentication either.** Anyone who can reach port
8889 and knows a path name can watch that camera. Path names are
`vms_cam_<8 hex>` (~4×10⁹ of them), which is obscurity, not security. On a
trusted network this is consistent with the rest of the posture; exposing 8889
beyond one would need MediaMTX read authentication in front of it.

---

## 10. REST API reference

Interactive docs at `/docs`; schema at `/openapi.json`.

| Method | Path | Success | Notes |
| --- | --- | --- | --- |
| `GET` | `/health` | `200` / `503` | `503` when the DB fails or MediaMTX is unreachable |
| `GET` | `/api/cameras` | `200` | list, in creation order |
| `POST` | `/api/cameras` | `201` | accepts an unreachable-but-valid URL |
| `GET` | `/api/cameras/{id}` | `200` | |
| `PATCH` | `/api/cameras/{id}` | `200` | partial; omitted fields are kept |
| `DELETE` | `/api/cameras/{id}` | `204` | removes the row and the path |
| `POST` | `/api/cameras/{id}/enable` | `200` | |
| `POST` | `/api/cameras/{id}/disable` | `200` | |
| `GET` | `/api/cameras/{id}/status` | `200` | health only |

There is **no playback/session endpoint**. `webrtc_url` points straight at
MediaMTX's WHEP endpoint, which is everything the browser needs; inventing a
signalling proxy would add a failure mode for no benefit.

### Camera response

```json
{
  "id": "cam_ab12cd34",
  "name": "Parking Entrance",
  "rtsp_url_display": "rtsp://admin:***@10.0.0.9:554/Streaming/Channels/101",
  "has_credentials": true,
  "enabled": true,
  "mediamtx_path": "vms_cam_ab12cd34",
  "webrtc_url": "http://localhost:8889/vms_cam_ab12cd34/whep",
  "health": {
    "state": "ONLINE",
    "last_error": null,
    "checked_at": "2026-09-06T00:00:00.000Z",
    "mediamtx_available": true
  },
  "created_at": "2026-09-06T00:00:00.000Z",
  "updated_at": "2026-09-06T00:00:00.000Z"
}
```

### The `PATCH` contract

Fields **absent from the request body** are kept. `rtsp_url` is therefore
omitted, not blanked, to keep the stored source — which is what the edit form
does for a camera whose URL carries credentials, since the raw URL is never
sent to the browser. An explicit `""` is a `422`, because silently interpreting
an empty string as "no change" would make a genuine client bug invisible.
`model_fields_set` is what distinguishes "absent" from "sent as null".

### Error envelope

```json
{"error": {"code": "validation_error", "message": "...", "details": {"fields": []}}}
```

| Code | HTTP | Cause |
| --- | --- | --- |
| `validation_error` | 422 | bad payload; `details.fields[]` names the offending field |
| `camera_not_found` | 404 | unknown id |
| `internal_error` | 500 | unhandled; logged with a traceback, never echoed |

### Two failures that are deliberately *not* request errors

1. **MediaMTX unavailable.** A registration is desired state; an outage does not
   reject it. The change is stored, the camera reports `UNKNOWN` with a
   sanitised reason, and reconciliation applies it when MediaMTX returns. The
   outage surfaces through `/health` (503), not as a 503 on the write.
2. **A source MediaMTX refuses** (`400 invalid source`). The camera is kept and
   reported `OFFLINE` — the PRD's stated semantics for a source the VMS cannot
   obtain.

Consequence: `POST /api/cameras` returning `201` means "registered", not
"streaming". Clients read success from `health.state`, in one place. This is
the same judgement the simulator made for failed starts, and it is equally
open to challenge (§15).

### Validation rules

| Field | Rule |
| --- | --- |
| `name` | required, trimmed, 1–100 chars, not blank |
| `rtsp_url` | `rtsp://` or `rtsps://`, host required, optional port 1–65535, no fragment, no whitespace, no control characters, ≤ 2048 chars |
| `enabled` | boolean, defaults to `true` |
| anything else | rejected (`extra="forbid"`) — including `mediamtx_path` and `id`, which the VMS owns |

---

## 11. Frontend

One page, vanilla JS, no build step, no framework. The two state models of §6.1
are visible as two separate badges on every tile.

### Structure

- **Grid** — `renderGrid(cameras)` creates a tile per new camera id, patches
  existing tiles in place, and removes tiles for cameras that are gone. It never
  writes `innerHTML` on the grid, because that would destroy every `<video>`
  element and restart every stream on each 2-second poll.
- **Tile** — fixed 16:9 frame (so the grid never reflows when a camera drops),
  a `<video muted autoplay playsinline>`, an overlay for non-playing states,
  name, health badge, player badge, masked source URL, an error line, and the
  actions Focus / Enable-Disable / Edit / Copy URL / Delete.
- **Focused view** — a modal with its own larger player.
- **Add/Edit dialog** — Name, RTSP URL, Enabled. No simulator discovery, no
  import, no path field.

### The `Player` class

One WebRTC/WHEP session bound to one `<video>`:

```
constructor → state = CONNECTING, new MediaMTXWebRTCReader({url, onTrack, onError})
onTrack     → video.srcObject = stream; muted; play().catch(ignore)
'playing'   → state = LIVE            (the element really is rendering frames)
onError     → message contains "retrying" ? RECONNECTING : ERROR
close()     → reader.close(); srcObject = null; listener removed
```

The vendored reader retries by itself every ~2 s, which is why a transient
failure maps to `RECONNECTING` rather than a teardown: **a source coming back
never requires recreating the camera or the player.** `LIVE` is driven by the
`playing` event rather than by `onTrack`, so the badge means "frames are
arriving", not "negotiation succeeded".

### Player lifecycle rules

A running player is restarted **only** when `enabled` or `webrtc_url` changes:

```js
const shouldPlay = camera.enabled && state.focusedId !== camera.id;
if (!shouldPlay) { stopPlayer(camera.id); return; }
if (existing && existing.url === camera.webrtc_url) return;   // keep it
```

Neither `camera.name` nor `camera.health` appears in that decision, so a rename
or a health flap cannot interrupt playback. A static-UI test asserts those two
identifiers are absent from the function body.

Players are stopped when a tile is removed, the camera is disabled, its
`webrtc_url` changes, or the focused view takes over. Opening the focused view
**stops that camera's tile player** and starts one in the modal, so a camera is
never pulled twice; closing it hands the session back. Every other tile is
untouched throughout — asserted in the browser.

### Polling

`/api/cameras` and `/health` every 2 s. Polling is never paused, because the
grid is the product; it is safe because the poll only patches text, badges and
overlays, and never touches form inputs or opens a dialog (there is a test for
the latter).

### Modal visibility hardening

The simulator's handoff documents a real bug: author CSS such as
`.modal { display: flex }` outranks the user-agent `[hidden] { display: none }`
rule, so elements toggled with `el.hidden` stayed visible. That lesson is
carried over here from the start, in three layers:

1. `[hidden] { display: none !important; }` — restated with author weight;
2. `.modal { display: none }` + `.modal:not([hidden]) { display: flex }` +
   `.modal[hidden] { display: none !important }` — the dialog is closed by
   default, so it stays shut even if layer 1 is ever weakened;
3. one JS helper, `setModalOpen(id, open)`, owning the `hidden` attribute for
   both dialogs; `init()` forces both closed before any timer is registered.

Assets carry a version query string and the server sends `no-store` for `/` and
`/static/*`, so a tab open across a rebuild cannot keep serving the old
stylesheet — the failure mode that made the simulator's fix *look* ineffective.

Nine static-UI tests lock all of this down, and the browser suite verifies the
behaviour for real.

---

## 12. Testing and verification record

### Automated tests — 215 total, all passing

**189 default (0.4 s) + 15 integration (~6 s) + 11 browser E2E (~4.5 min).**

| Module | Tests | Covers |
| --- | ---: | --- |
| `test_rtsp_url_security.py` | 35 | scheme/host/port validation, IPv6 literals, 15 rejection shapes (fragment, whitespace, NUL, newline, bad port, over-length…), password masking incl. percent-encoded and IPv6 forms, username-only URLs, unparsable fallback, free-text scrubbing, control-character stripping |
| `test_validation.py` | 25 | request models, name trimming, credentialed URL kept verbatim, 10 invalid payloads, `mediamtx_path`/`id` rejected as caller input, `provided_fields` semantics incl. `enabled=false`, blank URL rejected, id shape and uniqueness over 50 draws, settings-derived WHEP/managed-path URLs, env overrides |
| `test_repository.py` | 13 | exact column set, **absence of health columns**, WAL, CRUD, unique path, duplicate id, update preserves `created_at` and path, delete idempotence, counts, credentialed URL round-trips verbatim, insertion order under identical timestamps |
| `test_mediamtx_client.py` | 17 | exact method, path and body for every call; pagination; `available`/`ready`/tracks mapping; sparse payload tolerance; 404→`PathNotFound`, 400→`PathRejected`, 401→`MediaMTXError`, connect/timeout→`MediaMTXUnavailable`; config listing drops the credentialed payload; error text carries no password and no control characters |
| `test_camera_manager.py` | 36 | create enabled/disabled/offline/rejected/with-MediaMTX-down; rename does not touch MediaMTX; URL change replaces the same path; omitted URL keeps the credentialed source; no-op update writes nothing; disable/enable round trip keeps the WebRTC URL; delete with MediaMTX down then cleanup; health transitions both ways; `UNKNOWN` while unreachable; one offline camera does not affect another; **health is not persisted**; startup reconcile; stale `vms_` paths removed and foreign paths spared; drift detection; restart detection; orphan detection; recovery reconcile; rate limiting and its force-override; a URL edited during an outage applied on recovery; reconcile continues past a rejected source; mid-pass reconcile request survives; an ongoing outage is not logged every poll; health report |
| `test_api_cameras.py` | 34 | every route and status code; the exact `CameraView` key set; **no response carries the raw URL**; OpenAPI has no `rtsp_url` property; validation errors do not echo a password; `PATCH` name-only makes no MediaMTX call; blank/unknown fields rejected; 404s; `204` with an empty body; MediaMTX outage does not fail mutations; failure isolation across cameras; `no-store` on the UI but not on `/api` or `/health` |
| `test_static_ui.py` | 29 | both dialogs ship hidden; every conditional block ships hidden; the three CSS layers; one JS visibility helper; `init()` closes before timers; `tick()` never opens a dialog; assets version-stamped; vendored reader is the pinned one and loads first; **no custom signalling** (`RTCPeerConnection` and `/whep` absent from app code); the four player states; health and player badges are distinct elements; restart conditions exclude name and health; tiles patched not rebuilt; focus hands the session over; only the removed camera's player is closed; video elements muted/autoplay/inline; fixed aspect ratio; only the masked URL is rendered; edit form blanks a credentialed URL; blank URL omitted from the request; no simulator coupling; no out-of-scope words |
| `test_integration_mediamtx.py` | 15 | **real MediaMTX 1.20.1**, started from the config this repo ships |
| `test_e2e_live_view.py` | 11 | **real Chromium**, full chain |

Integration and browser suites are opt-in (`-m integration`, `-m e2e`) so the
default run stays a sub-second unit suite.

### What the integration suite proves against a real server

It starts two containers on a private network: the VMS MediaMTX from
`mediamtx/mediamtx.yml`, and a **stock** MediaMTX standing in for the camera.

- the shipped config boots and reports `v1.20.1`;
- the Control API accepts the VMS without credentials (the widened
  `authInternalUsers` works), and `docker-compose.yml` publishes no `:9997`;
- RTMP, HLS, SRT and MoQ do not start; RTSP, WebRTC and the API do;
- add → read → replace → delete a path with no server restart; delete of a
  missing path returns `false`; `replace` creates a missing path;
- several paths are independent;
- `/v3/paths/static-sources/get/{name}` really is a `404` in this version;
- a source MediaMTX cannot parse raises `PathRejected`;
- an unreachable Control API raises `MediaMTXUnavailable`;
- **the raw config endpoint really does return `hunter2`**, while the client's
  output does not;
- **the VMS MediaMTX refuses publishers**: an FFmpeg publish into it fails
  with `400 Bad Request`, while the identical command succeeds against the
  stock stand-in server — so the refusal is this config's doing;
- **a live stream flips `available` to true and back**: FFmpeg publishes a test
  pattern into the stand-in camera server, a managed path pulls it, `available`
  becomes true with an H264 track, the publisher is killed, `available` returns
  to false and the path stays configured;
- the manager reconciles against the real server: enabled configured, disabled
  not, health `OFFLINE` for an unreachable source, a planted stale `vms_` path
  removed, disabling removes the path.

### What the browser suite proves

Both stacks running; the simulator is used purely as a signal generator.

| Test | Assertion |
| --- | --- |
| Camera plays | `player=LIVE`, `readyState ≥ 2`, `videoWidth=320`, health `ONLINE`, and **the frame hash changes** after 1.5 s — real moving pixels, not one stuck picture |
| Delivery origin | The only WHEP requests go to `localhost:8889`; the browser makes **no** request to `:8554` or `:8080` |
| Four cameras | All four LIVE; deleting one removes its tile while the other three keep advancing frames |
| Rename | The tile's name changes and the player never leaves `LIVE`; a bystander camera also stays `LIVE` |
| URL change | Same `mediamtx_path` and `webrtc_url`; the tile shows the new source and returns to LIVE playback |
| Disable / enable | Tile shows `DISABLED` with `srcObject === null`; a second camera keeps playing; re-enabling resumes playback |
| Source loss and return | LIVE → source stopped → health `OFFLINE` → source restarted → LIVE again, with the **same `created_at` and `mediamtx_path`** — no recreation |
| Focused view | Opens, plays (`videoWidth > 0`, `LIVE`), the focused camera's own tile has `srcObject === null`, another tile stays `LIVE`, closing hands the session back |
| Browser refresh | Playback recovers after `reload()` |
| UI add and delete | Dialog starts hidden, opens, submits, closes only on success, the tile appears and plays, delete removes it |
| Credentials | `hunter2` appears nowhere in the DOM; `admin:***@…` does; the edit form's URL field is empty and the keep-source hint is visible |

Every test also fails on any uncaught page error.

### Manual verification (Docker, both stacks)

| # | Scenario | Result |
| --- | --- | --- |
| 1 | Simulator started independently; four cameras created; `ffprobe -rtsp_transport tcp` | `h264 320x240 15/1` |
| 2 | VMS started independently while the simulator was already running | healthy, MediaMTX reachable |
| 3 | Four simulator URLs registered via the API | all four `OFFLINE` at first, all `ONLINE` within ~6 s |
| 4 | Grid in headless Chromium | four tiles LIVE, four distinct frames, separate ONLINE/LIVE badges, masked URLs |
| 5 | Focused view | large player LIVE, `MediaMTX v1.20.1 · 4/4 online` in the header, focused tile idle, others LIVE |
| 6 | `docker compose restart vms-mediamtx` | poll logged `reconcile_requested reason=drift` then `reconciled reason=drift enabled=4`; all four back `ONLINE`; players recovered |
| 7 | `docker compose up -d --build vms` (backend restart) | registrations survived; `reconciled reason=startup enabled=4`; streams never interrupted |
| 8 | `docker compose down` then `up` (volume retained) | identical camera ids and names; all `ONLINE` again |
| 9 | MediaMTX stopped, then VMS restarted against it | `/` and `/api/cameras` served `200`, `/health` `503`, all cameras `UNKNOWN` |
| 10 | Camera added *during* that outage | accepted, `UNKNOWN`, sanitised reason |
| 11 | MediaMTX started again | all five cameras `ONLINE`; reconcile applied the one added during the outage |
| 12 | Tests run inside the container (`docker compose run --rm --no-deps vms pytest -q`) | 189 passed |

### Issues found and fixed during verification

1. **List ordering was nondeterministic.** `created_at` had second precision, so
   cameras created in the same second tied and `ORDER BY created_at, id`
   ordered them by a random hex id. Fixed with millisecond timestamps **and** a
   `rowid` tiebreak (true insertion order, independent of clock resolution);
   regression test added.
2. **MediaMTX version was never fetched** — `/health` reported `"version": null`
   and the header badge read "MediaMTX ok". Now fetched on the first successful
   poll and again after a recovery.
3. **`httpx` logged one INFO line per poll**, i.e. every 2 s, burying everything
   else. Its logger is now pinned to `WARNING`.
4. **An ongoing MediaMTX outage warned every 2 s.** Repeat unreachability now
   logs at `DEBUG`; the first failure and the recovery still log loudly. Test
   added.
5. **A reconcile requested mid-pass was swallowed** by the unconditional
   `pending_reconcile = None` at the end of `reconcile_all`. Now cleared only if
   it still matches what was pending when the pass began. Test added.
6. **The first live-stream integration test was wrong, not the code.** It tried
   to publish FFmpeg into the VMS MediaMTX, which refuses publishers by design.
   Rewritten to publish into a stand-in camera server on a private network — the
   real topology — and the refusal itself became an assertion.

---

## 13. Known issues and limitations

**Functional**

- A WebRTC session that stalls **without** the peer connection failing can keep
  reading `LIVE` until ICE consent checks time out (~30 s). The health badge,
  which comes from MediaMTX, is authoritative in that window. Frame-progress
  detection would close the gap; it was judged out of scope for V1.
- MediaMTX 1.20.1 exposes no per-source error, so an `OFFLINE` camera gets a
  generic message rather than "authentication failed" or "no route to host".
  `docker compose logs vms-mediamtx` has the detail.
- `UNKNOWN` conflates three different situations — MediaMTX unreachable, path
  not configured yet, and camera disabled — distinguished only by
  `last_error` text. The UI shows `DISABLED` as a separate badge, but the API
  model does not.
- A MediaMTX restart detected via **drift** (rather than via unreachability)
  goes through the rate-limited path, so convergence can take up to
  `RECONCILE_MIN_INTERVAL_SECONDS` (10 s) rather than one poll. Observed
  convergence in practice: ~3 s. The same path also does not refresh the cached
  MediaMTX version.
- No migration framework. A future schema change needs a migration step or a
  volume reset.
- Playback depends on codecs MediaMTX can pass through to WebRTC without
  transcoding. H.264 is the practical baseline; H.265 will not play in most
  browsers, and the VMS will show `ONLINE` while the tile stays black.

**Operational**

- Single-process assumption. Two VMS replicas pointed at one MediaMTX would
  both reconcile and fight over paths. There is no leader election and no
  advisory lock.
- The poll reads every camera row from SQLite every 2 s. Trivial at four
  cameras, a full table scan every 2 s at five hundred.
- No metrics endpoint, no structured (JSON) logs, no log rotation beyond
  Docker's.
- No capacity control. Four cameras is a functional demonstration; nothing
  limits how many can be enabled or admits load.
- Verified on arm64 (Apple Silicon) with Docker Desktop only.

**UI**

- The static-UI tests assert the served CSS/JS/HTML *text*, not computed
  styles. That is sound only because there is no CSS build or minification
  step; if one is added, those assertions must be replaced with browser checks.
  (The browser suite already covers the behaviour that matters most.)
- No optimistic UI: an action waits for its request, then the next poll
  refreshes the grid.
- `GET /api/cameras` is unpaginated, and the grid renders every camera.

**Testing**

- The E2E suite depends on the simulator being up and on Playwright's Chromium
  (which ships proprietary codecs, hence H.264 playback). Its cleanup is
  fixture-based; a hard crash mid-run can leave `e2e-<tag>-*` cameras behind in
  the simulator.
- No load, soak or chaos testing beyond stopping and restarting MediaMTX and
  the source.

---

## 14. Deliberate non-goals

Explicitly out of scope for Component 2, per the PRD: recording, historical
playback, timeline UI, snapshots, AI/analytics, object detection, events,
search/RAG, GStreamer or FFmpeg in the live path, unnecessary transcoding,
simulator management or integration, ONVIF, discovery, PTZ, authentication,
multi-user support, Kubernetes, and production-scale observability.

**Run this on a trusted network.** Anyone who can reach port 8090 can add,
edit and delete cameras; anyone who can reach 8889 and knows a path can watch.

### The Component 3 boundary

Recording is Component 3 and nothing here anticipates it beyond keeping the
attachment point clean:

```
                          ┌── WebRTC ──► Browser live   (implemented)
RTSP camera ──► MediaMTX ─┤
                          └── recording consumer        (Component 3)
```

MediaMTX already holds every camera as a named, stable path, so a recorder can
read those paths — or MediaMTX's own `record` can be switched on per path —
without touching the live delivery path. `record: false` is set **explicitly**
on every managed path so that enabling it later is a deliberate act rather than
a default that drifted. No recording schema, retention policy, storage layout
or playback logic exists in this component.

---

## 15. Open questions and candidate gaps for review

These are places where the implementation made a judgement call, or where a
reviewer might reasonably find something missing. They are questions, not
defects.

**API semantics**

1. *A MediaMTX outage does not fail a mutation.* `POST /api/cameras` returns
   `201` with `health.state: "UNKNOWN"`. The plan listed a `mediamtx_unavailable`
   503; it is not implemented, because the same plan's manager semantics say the
   desired state must be kept. Should an explicit write instead return `503`
   with the envelope, forcing the client to retry? The counter-argument —
   clients should not have to inspect a body to learn a command did not fully
   take effect — is legitimate.
2. *A source MediaMTX rejects is `OFFLINE`, not `422`.* Same trade-off. A typo
   MediaMTX parses more strictly than we do produces a registered-but-broken
   camera rather than a rejected request.
3. *No `409 operation_in_progress`.* Callers await the per-camera lock instead.
   Correct while mutations are single-digit milliseconds; wrong if a future
   operation becomes slow.
4. *`PATCH` treats an omitted `rtsp_url` as "keep".* This makes "clear the URL"
   unexpressible — deliberately, since a camera without a source is not a
   camera. Is an explicit `keep_source` flag clearer than an absent field?
5. *No pagination or filtering on `GET /api/cameras`.* Fine for tens of
   cameras. At what point does it need `?enabled=` and a page cursor?

**Health and reconciliation**

6. Is 2 s the right poll interval, and 10 s the right drift floor? Both are
   configurable but neither is adaptive.
7. Should a MediaMTX restart detected via drift bypass the rate limit the way a
   recovery does? It is the same event seen through a different signal.
8. `UNKNOWN` covers three distinct situations (§13). Should `DISABLED` be a
   fourth health state in the API, matching what the UI already displays?
9. Nothing records *when* a camera went offline. Should health carry
   `since` / `last_online_at` so the grid can say "offline for 3 minutes"?
10. Log scraping was rejected as a substitute for the missing per-source
    `lastError`. Is a generic message genuinely enough for an operator, or
    should the VMS probe the RTSP source itself (an `OPTIONS`/`DESCRIBE`) to
    distinguish auth failure from unreachable host?

**Concurrency and scale**

11. The single-process assumption (§13). If the VMS is ever run with more than
    one replica, what arbitrates path ownership?
12. `_locks` holds one `asyncio.Lock` per camera for the process's lifetime.
    Bounded by camera count, but never swept.
13. Should the poll cache the camera list instead of re-reading SQLite every
    2 s?

**Frontend**

14. The focused view opens a **second** WebRTC session and stops the tile's.
    Moving the existing `MediaStream` to the larger element would avoid a
    renegotiation entirely. Which is better depends on whether "the tile keeps
    its own session warm" matters more than one extra connect.
15. `LIVE` means the `playing` event fired, not that frames are still arriving
    (§13). Should the player watch `requestVideoFrameCallback` or
    `video.currentTime` progress and degrade to `RECONNECTING` on a stall?
16. The reader retries forever with no cap and no user-visible "gave up" state.
17. Polling is never paused, including while the add/edit dialog is open. Safe
    today because the poll never touches form inputs — but it is an invariant a
    future edit could break silently.

**Security**

18. RTSP credentials are stored unencrypted (§9). What is the intended
    deployment boundary — always localhost/lab, or will this be exposed?
19. The WHEP endpoint is unauthenticated (§9). If port 8889 ever leaves the
    trusted network, MediaMTX read authentication is the first thing to add,
    and the VMS would then have to mint and hand out credentials or tokens.
20. `webrtcAllowOrigins: ["*"]`. Should it be pinned to the VMS origin?

**Scope**

21. The PRD names recording as Component 3. Does the attachment point in §14
    match how you intend to build it — MediaMTX-native recording per path, or a
    separate consumer reading those paths?

---

## 16. Operating the system

```bash
# Start (UI at http://localhost:8090, API docs at /docs)
docker compose up --build

# Register a camera
curl -X POST http://localhost:8090/api/cameras \
  -H 'content-type: application/json' \
  -d '{"name":"Parking Entrance","rtsp_url":"rtsp://10.0.0.9:554/Streaming/Channels/101"}'

# Watch the raw stream outside the browser
ffprobe -rtsp_transport tcp rtsp://localhost:8555/vms_cam_ab12cd34

# Tests
docker compose run --rm --no-deps vms pytest -q     # 189 unit / API / static UI
.venv/bin/python -m pytest -m integration           # 15, needs Docker
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -m e2e                   # 11, needs both stacks up

# Logs
docker compose logs -f vms
docker compose logs -f vms-mediamtx

# Stop (registrations persist) / wipe everything
docker compose down
docker compose down -v

# Different ports
VMS_HTTP_PORT=9090 VMS_WEBRTC_HTTP_PORT=9889 docker compose up --build
```

### With the RTSP Camera Simulator

```bash
# simulator clone, its own Compose stack
docker compose up -d --build                    # http://localhost:8080
# create a camera in its UI, then confirm the stream exists
ffprobe -rtsp_transport tcp rtsp://localhost:8554/simulator/parking-entrance

# this clone
docker compose up -d --build                    # http://localhost:8090
# register: rtsp://host.docker.internal:8554/simulator/parking-entrance
```

`host.docker.internal` is how the VMS container reaches the simulator's port on
the host. A real camera would use its own address; the VMS cannot tell the
difference and contains no code that knows the simulator exists.

### Log events to grep for

`vms_started`, `vms_stopped`, `camera_created`, `camera_updated`,
`camera_deleted`, `camera_source_rejected`, `mediamtx_path_ensured`,
`mediamtx_path_deleted`, `reconcile_requested`, `reconciled`,
`mediamtx_error`, `mediamtx_unreachable`, `mediamtx_recovered`,
`health_poll_failed`. Camera-scoped lines carry `camera_id=`, and any URL in a
log line is masked.

### Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Camera stuck `OFFLINE` | Check the source: `ffprobe -rtsp_transport tcp <url>`. From Docker the host is `host.docker.internal`, not `localhost` |
| Everything `UNKNOWN` | MediaMTX unreachable; check `/health` and `docker compose logs vms-mediamtx` |
| Health `ONLINE` but the tile stays `CONNECTING` | ICE cannot get through. Confirm `8189/udp` is published and `PUBLIC_WEBRTC_HOST` matches the address the browser used |
| Black tile from another machine | `PUBLIC_WEBRTC_HOST` is still `localhost` |
| `ONLINE` but permanently black | Codec is not WebRTC-compatible (e.g. H.265). MediaMTX does not transcode |
| UI looks stale after a rebuild | Assets are version-stamped and `no-store`; a hard refresh clears anything older |

---

## 17. Appendix: worked examples

### Register a camera and watch it become live

```bash
curl -sX POST http://localhost:8090/api/cameras \
  -H 'content-type: application/json' \
  -d '{"name":"Parking Entrance",
       "rtsp_url":"rtsp://host.docker.internal:8554/simulator/parking-entrance"}'
# → 201, health.state = "OFFLINE"  (configured; MediaMTX has not connected yet)

sleep 5
curl -s http://localhost:8090/api/cameras/cam_ab12cd34/status
# → {"state":"ONLINE","last_error":null,
#    "checked_at":"2026-09-06T00:00:05.412Z","mediamtx_available":true}
```

### A camera with credentials, end to end

```bash
curl -sX POST http://localhost:8090/api/cameras \
  -H 'content-type: application/json' \
  -d '{"name":"Gate","rtsp_url":"rtsp://admin:hunter2@10.0.0.9:554/Streaming"}'
```

What each layer sees:

| Layer | Value |
| --- | --- |
| SQLite `rtsp_url` | `rtsp://admin:hunter2@10.0.0.9:554/Streaming` |
| MediaMTX path `source` | `rtsp://admin:hunter2@10.0.0.9:554/Streaming` |
| API `rtsp_url_display` | `rtsp://admin:***@10.0.0.9:554/Streaming` |
| API `has_credentials` | `true` |
| UI card, edit form, DOM | the masked form only; the field starts empty |
| Log line | `camera_created camera_id=… source=rtsp://admin:***@10.0.0.9:554/Streaming` |
| MediaMTX error text | scrubbed of both the URL and the bare password before it propagates |

### Rename versus re-source

```bash
# Rename: no MediaMTX call at all, playback never interrupted.
curl -sX PATCH http://localhost:8090/api/cameras/cam_ab12cd34 \
  -H 'content-type: application/json' -d '{"name":"Front Gate"}'

# New source: same path replaced in place; webrtc_url unchanged;
# the player reconnects rather than being recreated.
curl -sX PATCH http://localhost:8090/api/cameras/cam_ab12cd34 \
  -H 'content-type: application/json' \
  -d '{"rtsp_url":"rtsp://host.docker.internal:8554/simulator/lobby"}'

# Keep a credentialed source while editing something else: omit rtsp_url.
curl -sX PATCH http://localhost:8090/api/cameras/cam_ab12cd34 \
  -H 'content-type: application/json' -d '{"name":"Gate","enabled":true}'
```

### The MediaMTX calls a single create actually makes

```http
POST /v3/config/paths/replace/vms_cam_ab12cd34
{"source":"rtsp://…","sourceOnDemand":false,"rtspTransport":"tcp","record":false}

GET  /v3/paths/get/vms_cam_ab12cd34
```

Two calls. No server restart, no config file rewrite, no effect on any other
path.

### `/health`

```json
{
  "status": "ok",
  "database": "ok",
  "mediamtx": {
    "reachable": true,
    "version": "v1.20.1",
    "api_url": "http://vms-mediamtx:9997"
  },
  "cameras": {"total": 4, "enabled": 4, "online": 4},
  "webrtc_base_url": "http://localhost:8889"
}
```

`200` when the database is readable and MediaMTX is reachable; `503` with
`"status": "degraded"` otherwise.

---

*End of handoff. The user-facing guide is in [README.md](../../services/vms/README.md); the
vendored reader's licence is in [THIRD_PARTY_NOTICES.md](../../services/vms/THIRD_PARTY_NOTICES.md).*
