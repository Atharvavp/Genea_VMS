# RTSP Camera Simulator V1 — Engineering Handoff

**Repository:** `Genea_VMS`
**Component:** RTSP Camera Simulator (V1)
**Status:** Implemented, tested, and verified end-to-end in Docker
**Date of handoff:** 2026-09-04
**Document purpose:** a complete, self-contained technical description of what exists,
why it was built that way, what was proven by testing, and what was deliberately left
out — detailed enough for an independent reviewer (human or LLM) to audit the design
and find gaps without reading the source first.

> **How to use this document for a review.** Sections 1–11 describe the system as
> built. Section 12 records exactly what was verified and how. Sections 13–15 are the
> honest parts: known limitations, deliberate non-goals, and a list of open questions
> and candidate gaps. A reviewer looking for missing links should start at §13–15,
> then cross-check them against the mechanics in §6–9.

---

## Table of contents

1. [What this system is](#1-what-this-system-is)
2. [Architecture](#2-architecture)
3. [Repository layout and file responsibilities](#3-repository-layout-and-file-responsibilities)
4. [Data model and persistence](#4-data-model-and-persistence)
5. [Configuration](#5-configuration)
6. [Runtime flows, step by step](#6-runtime-flows-step-by-step)
7. [Concurrency and locking model](#7-concurrency-and-locking-model)
8. [The FFmpeg contract](#8-the-ffmpeg-contract)
9. [Security posture](#9-security-posture)
10. [REST API reference](#10-rest-api-reference)
11. [Frontend](#11-frontend)
12. [Testing and verification record](#12-testing-and-verification-record)
13. [Known issues and limitations](#13-known-issues-and-limitations)
14. [Deliberate non-goals for V1](#14-deliberate-non-goals-for-v1)
15. [Open questions and candidate gaps for review](#15-open-questions-and-candidate-gaps-for-review)
16. [Operating the system](#16-operating-the-system)
17. [Appendix: worked examples](#17-appendix-worked-examples)

---

## 1. What this system is

A tool that turns ordinary video files into **virtual RTSP cameras**. You give it a
video (uploaded through a browser, or placed in a mounted directory), tell it how the
camera's output should be encoded, and it publishes a continuously looping RTSP stream
that any RTSP client — VLC, FFplay, a VMS under test — can pull exactly as if it were
a real IP camera.

It exists to make camera-dependent software testable without camera hardware: you can
create ten cameras with different codecs, resolutions and frame rates in a minute, and
take one "offline" by stopping it.

**Explicitly not** a VMS. There is no recording, no playback UI, no preview, no
analytics. It is a *source* of RTSP streams.

### Requirements this implements

Built to a written plan (`RTSP Camera Simulator V1 Plan.md`), which was treated as the
authoritative specification. Everything in the plan is implemented. Two small additions
were made in service of the plan's stated UI intent, both flagged in §15.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Browser (single page, vanilla JS — no build step)                        │
│   • camera cards + status polling every 2 s                              │
│   • add/edit dialog, drag-and-drop upload                                │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ HTTP: multipart/form-data or JSON
┌───────────────────────────────▼──────────────────────────────────────────┐
│ FastAPI (container: simulator, port 8080)                                │
│   api/cameras.py   → parses payload, validates, delegates                │
│   api/errors.py    → single error envelope for every failure             │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────────┐
│ CameraManager  (the only place that combines everything)                 │
│   • CRUD + lifecycle state machine    • per-camera asyncio.Lock          │
│   • startup reconciliation            • process-exit reconciliation      │
│        │              │                 │                 │             │
│        ▼              ▼                 ▼                 ▼             │
│  CameraRepository  SourceStorage   ProbeService   FFmpegProcessManager   │
│    (SQLite)        (files)         (ffprobe)      (child processes)      │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ one process per camera
┌───────────────────────────────▼──────────────────────────────────────────┐
│ ffmpeg -re [-stream_loop -1] -i <file> ... -f rtsp -rtsp_transport tcp   │
│         rtsp://mediamtx:8554/simulator/<stream_path>                     │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ RTSP publish (TCP)
┌───────────────────────────────▼──────────────────────────────────────────┐
│ MediaMTX 1.20.1 (container: mediamtx, port 8554)                         │
│   dynamic paths: `all_others:` — no per-camera reconfiguration           │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ RTSP read (TCP)
                     VLC · FFplay · ffprobe · a VMS under test
```

### Why these boundaries

| Boundary | Reason |
| --- | --- |
| One FastAPI container serving both API and UI | No Node toolchain needed; one image, one process tree, one place where FFmpeg children live. |
| MediaMTX as a separate service | It is the RTSP server. Keeping it separate means the simulator can crash/restart without dropping the server, and MediaMTX is pinned to a known version (`1.20.1`). |
| `CameraManager` as the single orchestrator | Persistence, files, probing and processes must be kept consistent with one another; splitting that across route handlers is where race conditions and orphan processes come from. |
| Process manager knows nothing about the database | It owns children and reports exits; the manager decides what an exit *means*. Makes both independently testable. |
| SQLite via stdlib `sqlite3` | The data is a handful of rows. No ORM, no migration framework, no extra container. |

---

## 3. Repository layout and file responsibilities

```
.
├── docker-compose.yml            simulator + mediamtx, volumes, ports, stop_grace_period
├── .env.example                  every tunable, with defaults
├── README.md                     user-facing docs (quick start → troubleshooting)
├── HANDOFF.md                    this document
├── mediamtx/mediamtx.yml         RTSP only, TCP only, dynamic paths
├── sample-media/                 host dir mounted read-only at /data/local-sources
│   ├── README.md
│   └── sample-320x240.mp4        10 s test pattern, so the app is usable immediately
└── simulator/
    ├── Dockerfile                python:3.12-slim + ffmpeg, editable install
    ├── pyproject.toml            deps, pytest config (integration tests opt-in)
    ├── app/
    │   ├── main.py         (115) app factory, lifespan (startup reconcile / stop-all),
    │   │                         /health, static mount, no-store middleware for the UI
    │   ├── config.py       ( 68) Settings from env; internal vs public RTSP URL construction
    │   ├── logging_config.py(24) one log format for app + uvicorn
    │   ├── api/
    │   │   ├── cameras.py  (155) routes; multipart+JSON parsing; form lifetime management
    │   │   └── errors.py   (117) ApiError hierarchy + handlers → single error envelope
    │   ├── domain/
    │   │   └── models.py   (308) Pydantic models, validation, status enum, slugging
    │   ├── persistence/
    │   │   ├── database.py (59)  connection factory (WAL, busy timeout) + schema DDL
    │   │   └── camera_repository.py (165) row ⇄ CameraRecord, CRUD, uniqueness
    │   └── services/
    │       ├── camera_manager.py  (571) orchestration, locking, state machine, reconciliation
    │       ├── process_manager.py (269) spawn, startup grace, stderr tail, stop/kill, supervise
    │       ├── ffmpeg_command.py  (109) pure argv builder (no I/O, fully unit-testable)
    │       ├── probe.py           (138) ffprobe wrapper + JSON → SourceMetadata
    │       └── source_storage.py  (194) upload staging/promotion, mounted-path containment
    │   └── static/
    │       ├── index.html  (165) version-stamped asset URLs; conditional blocks ship hidden
    │       ├── app.js      (570) modal state via isModalOpen()/setModalOpen()
    │       └── styles.css  (276) [hidden]!important + explicit modal open/closed contract
    └── tests/                     141 unit/API/static-UI tests + 2 integration tests
```

Total: ~2,290 lines of Python, ~1,010 lines of frontend, ~1,700 lines of tests.

### Component contracts

- **`ffmpeg_command.build_publish_command(camera, target_url, binary) -> list[str]`** —
  pure function, no side effects. This is the single source of truth for what FFmpeg is
  asked to do, which is why it is exhaustively unit-tested.
- **`ProbeService.probe(path, display_filename) -> SourceMetadata`** — raises
  `ProbeError` for anything not decodable or lacking a video stream. Never leaks the
  internal path into the error message.
- **`SourceStorage`** — the only component that touches the filesystem for sources.
  `stage_upload` → `promote` → `delete_stored`, plus `resolve_local` for mounted files.
- **`FFmpegProcessManager`** — owns `dict[camera_id, ManagedProcess]`. Exposes
  `start / stop / stop_all / is_running / get / running_camera_ids` and one callback,
  `set_exit_callback(cb)`, invoked with a `ProcessExit(camera_id, returncode,
  stderr_tail, requested)`.
- **`CameraRepository`** — synchronous; every call is dispatched to a worker thread by
  the manager (`asyncio.to_thread`).

---

## 4. Data model and persistence

### SQLite schema (`/data/simulator.db`)

```sql
CREATE TABLE IF NOT EXISTS cameras (
    id                       TEXT PRIMARY KEY,   -- cam_<8 hex>
    name                     TEXT NOT NULL,
    stream_path              TEXT NOT NULL,      -- unique, slug only
    source_kind              TEXT NOT NULL,      -- 'upload' | 'local'
    source_path              TEXT NOT NULL,      -- absolute container path, never exposed
    source_original_filename TEXT NOT NULL,      -- display only
    source_stored_filename   TEXT,               -- non-null only for owned uploads
    source_metadata_json     TEXT NOT NULL,      -- ffprobe result
    codec                    TEXT NOT NULL,
    resolution_json          TEXT NOT NULL,      -- {"mode":"source"} | {"mode":"fixed",...}
    fps_json                 TEXT NOT NULL,
    bitrate_json             TEXT NOT NULL,
    loop                     INTEGER NOT NULL,
    auto_start               INTEGER NOT NULL,
    status                   TEXT NOT NULL,      -- last known; reconciled at startup
    last_error               TEXT,
    created_at               TEXT NOT NULL,      -- ISO-8601
    updated_at               TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cameras_stream_path ON cameras (stream_path);
```

Schema is created with `CREATE TABLE IF NOT EXISTS` at startup. **There is no migration
framework** — see §15.

### What is *not* persisted

PIDs, process handles, stderr buffers, supervisor tasks and per-camera locks are
in-memory only. A PID from a previous container is meaningless, so it is never
written down and never trusted. This is what makes restart reconciliation simple and
correct.

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

Short-lived connection per operation, opened and closed on the same worker thread that
`asyncio.to_thread` dispatched the call to. Consequences: a connection is never shared
across threads (the classic `sqlite3` failure mode), WAL lets reads proceed during a
write, the busy timeout absorbs contention, and the event loop is never blocked by
disk I/O.

### File storage

| Path | Contents | Ownership |
| --- | --- | --- |
| `/data/simulator.db` | camera rows | simulator |
| `/data/videos/<camera_id>_<uuid>.<ext>` | uploaded sources | camera-owned; deleted with the camera |
| `/data/tmp/staging_<uuid>.<ext>` | in-flight uploads | removed on success *and* on every failure path |
| `/data/local-sources/**` | mounted sources (read-only) | **never** written or deleted by the simulator |

Uploaded files are camera-owned and not shared: one camera, one file. Replacing a
source deletes the previous file only *after* the new one has been probed and the DB
row updated.

---

## 5. Configuration

All settings come from the environment via `pydantic-settings`. Compose supplies
defaults; `.env` overrides them.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MEDIAMTX_HOST` / `MEDIAMTX_RTSP_PORT` | `mediamtx` / `8554` | where FFmpeg **publishes** |
| `PUBLIC_RTSP_HOST` / `PUBLIC_RTSP_PORT` | `localhost` / `8554` | what URLs **advertise** to clients |
| `RTSP_PATH_PREFIX` | `simulator` | path prefix in the RTSP URL |
| `SIMULATOR_HTTP_PORT` | `8080` | host port for UI/API |
| `DATA_DIR` / `DATABASE_PATH` / `UPLOAD_VIDEO_DIR` / `SOURCE_VIDEO_DIR` | `/data`, … | storage layout |
| `MAX_UPLOAD_BYTES` | `2147483648` | upload cap (2 GiB) |
| `FFMPEG_BINARY` / `FFPROBE_BINARY` | `ffmpeg` / `ffprobe` | binary names (tests point these at stubs) |
| `FFMPEG_STARTUP_GRACE_SECONDS` | `2.0` | how long a publisher must survive to be `RUNNING` |
| `FFMPEG_STOP_TIMEOUT_SECONDS` | `5.0` | SIGTERM grace before SIGKILL |
| `FFMPEG_STDERR_TAIL_LINES` | `50` | bounded stderr ring buffer per process |
| `FFPROBE_TIMEOUT_SECONDS` | `20.0` | ffprobe watchdog |
| `LOG_LEVEL` | `INFO` | |

**The internal/public URL split matters.** FFmpeg must reach MediaMTX over the Compose
network (`mediamtx:8554`); a user's VLC must reach it via the published host port
(`localhost:8554`). These are separate settings, and the API/UI always show the public
one. Deploying on a remote host means setting `PUBLIC_RTSP_HOST` to that host's
address.

---

## 6. Runtime flows, step by step

### 6.1 Lifecycle state machine

```
                     ┌──────────┐
   create ──────────►│ CREATED  │
                     └────┬─────┘
                          │ (auto_start? start : →STOPPED)
        ┌─────────────────▼──────────────────┐
        │                                    │
   ┌────▼─────┐  start   ┌──────────┐   ok   ├──►┌─────────┐
   │ STOPPED  ├─────────►│ STARTING ├────────┘   │ RUNNING │
   └────▲─────┘          └────┬─────┘            └────┬────┘
        │                     │ died in grace         │
        │ stop                ▼                       │ unexpected exit
        │                ┌─────────┐◄─────────────────┘
        │                │  ERROR  │
        │                └────┬────┘
        │  ┌──────────┐       │ start / stop
        └──┤ STOPPING │◄──────┘
           └──────────┘
   non-looping camera, ffmpeg exit 0  ──►  STOPPED (last_error cleared)
```

| Operation | Allowed from | Notes |
| --- | --- | --- |
| `start` | `CREATED`, `STOPPED`, `ERROR` | idempotent no-op if already `RUNNING`; `409` if an operation is in flight |
| `stop` | `STARTING`, `RUNNING`, `ERROR` | idempotent on `CREATED`/`STOPPED`; clears `last_error` |
| `restart` | `CREATED`, `STOPPED`, `RUNNING`, `ERROR` | stop-then-start under one lock |
| `delete` | any | stops the process first, then row, then owned file |
| `update` | any non-transitional | validates first; restarts only if runtime-affecting |

### 6.2 Create a camera

1. Resolve `stream_path`: explicit (409 if taken) or slugified from the name and
   de-duplicated (`lobby`, `lobby-2`, …).
2. Resolve the source — exactly one of upload / mounted, else `400`.
   - **upload:** stream to `/data/tmp/staging_<uuid>.<ext>`, enforcing `MAX_UPLOAD_BYTES`
     while writing; reject empty files and disallowed extensions.
   - **mounted:** `resolve_local()` — resolve symlinks, assert the result is inside
     `SOURCE_VIDEO_DIR`, assert it is a file.
3. `ffprobe` it. Failure ⇒ delete the staged file, return `422 invalid_source`. No row
   is written, no file is kept.
4. Generate `cam_<8hex>`; move the staged file to
   `/data/videos/<camera_id>_<uuid>.<ext>` (mounted files stay where they are).
5. `INSERT` the row with status `CREATED`. If the insert fails (unique violation), the
   promoted file is deleted before the error propagates.
6. `auto_start` ⇒ run the start flow under the camera's lock; else persist `STOPPED`.
7. Respond `201` with the camera view — **including** when auto-start ended in `ERROR`.

### 6.3 Start a camera

```
lock held ─► already running?           → yes: idempotent, ensure status RUNNING, return
          ─► source file still present? → no:  status ERROR, "source no longer available"
          ─► persist STARTING
          ─► build argv, create_subprocess_exec(stdin=DEVNULL, stdout=DEVNULL, stderr=PIPE)
          ─► register in the process table (before the grace window)
          ─► start stderr reader task (bounded deque, 50 lines)
          ─► await process.wait() with timeout = FFMPEG_STARTUP_GRACE_SECONDS
                 exited  → drain stderr, unregister, persist ERROR + stderr tail
                 timeout → still alive: record started_at/pid, spawn supervisor task,
                           persist RUNNING, clear last_error
```

The grace window is the entire definition of "healthy". Anything that makes FFmpeg
exit immediately — MediaMTX down, unusable file, bad codec parameters — is caught here
and surfaced with FFmpeg's own words, instead of a camera that claims to be running.

### 6.4 Stop a camera

`stop_requested = True` → `SIGTERM` → wait `FFMPEG_STOP_TIMEOUT_SECONDS` → `SIGKILL`
if needed → await the supervisor task → unregister → persist `STOPPED`.

Setting `stop_requested` *before* signalling is what lets the exit handler distinguish
"we asked for this" from "it died". A requested exit never produces an `ERROR`.

### 6.5 Update, and the automatic restart

1. Validate the payload; nothing is persisted if validation fails.
2. Merge onto the existing record (partial `video` objects are merged field-by-field).
3. New source? Stage → probe → promote. The old owned upload is deleted only after the
   DB row is committed; if the DB write fails, the *new* file is removed instead.
4. Compute `restart_needed = was_running AND signature_changed`, where the signature is
   `(stream_path, source_path, loop, video_config)`. Name and auto-start are excluded
   by construction, so renaming never interrupts a stream.
5. Persist, then (if needed) stop-and-start under the same lock.
6. If the restart fails, return the **updated** camera with `status=ERROR` and
   `last_error` — the config change is not silently rolled back, because the user's
   intent was to change it.

### 6.6 Unexpected process exit

The supervisor task awaits `process.wait()`, drains stderr, unregisters the process,
then calls the manager's exit callback:

```python
if event.requested:            # our own stop/restart/delete owns the status
    return
async with per_camera_lock:
    record = repo.get(id)
    if record is None:              return   # deleted meanwhile
    if processes.is_running(id):    return   # a newer publisher already took over
    if not record.loop and event.returncode == 0:
        status = STOPPED, last_error = None  # non-looping clip reached its end
    else:
        status = ERROR, last_error = sanitized(stderr_tail or f"exit {returncode}")
```

Two guards prevent stale writes: the `requested` flag, and the re-check that no newer
publisher is registered.

### 6.7 Application startup (reconciliation)

```
ensure directories → init schema → load all rows
  for each camera:
      status in {RUNNING, STARTING, STOPPING}  →  persist STOPPED   (stale by definition)
      auto_start                               →  queue for start
  asyncio.gather(start(...) for queued, return_exceptions=True)
```

Auto-starts run concurrently and independently: one camera failing to start cannot
prevent the others, and cannot fail application startup.

### 6.8 Application shutdown

FastAPI lifespan calls `processes.stop_all()`, which stops every publisher in parallel
(SIGTERM, then SIGKILL after the timeout). Compose is given `stop_grace_period: 20s`,
comfortably above the 5 s per-process timeout. Measured shutdown: **under 1 second**
with a publisher running, and no `ERROR` states written (the stops are `requested`).

Note the intentional consequence: a camera's *persisted* status at shutdown may remain
`RUNNING`, because shutdown deliberately does not rewrite rows. That is precisely the
stale state §6.7 reconciles on the next boot.

---

## 7. Concurrency and locking model

This is the part most worth reviewing, so it is spelled out in full.

### Locks that exist

| Lock | Scope | Guards |
| --- | --- | --- |
| `CameraManager._locks[camera_id]` | one per camera | every lifecycle and update operation for that camera |
| `CameraManager._locks_guard` | manager-wide | creation/removal of entries in the lock dict only |
| `FFmpegProcessManager._registry_lock` | manager-wide | mutation of the `camera_id → ManagedProcess` dict, and the spawn itself |

**No global lock is ever held across a lifecycle operation.** Starting camera A never
blocks stopping camera B.

### How duplicate publishers are prevented — two independent mechanisms

1. **Per-camera lock.** `start`, `stop`, `restart`, `update` and `delete` all acquire it,
   so two concurrent starts serialise and the second sees `is_running() == True` and
   returns idempotently.
2. **Registry check under the registry lock.** `FFmpegProcessManager.start()` raises
   `AlreadyRunning` if an entry exists, and registers the new process *before* the
   grace window rather than after — closing the window where a concurrent caller could
   see an empty registry.

Verified empirically: five simultaneous `POST /start` requests produced exactly one
`ffmpeg` process in `/proc` (§12).

### `409 operation_in_progress` vs. queueing

Public lifecycle methods check `lock.locked()` *before* awaiting and return `409`
rather than queueing. Rationale: a UI that fires start/stop rapidly should get an
honest "busy" answer instead of a pile of delayed operations executing after the user
has moved on. The alternative (await the lock) would also be defensible — see §15.

### Deadlock analysis

The one dangerous shape is: *the lock holder awaits the process exit, and the exit
handler wants the same lock.*

```
stop()      : holds per-camera lock → awaits supervisor task
supervisor  : awaits process exit   → calls exit callback
exit callback: FIRST LINE → `if event.requested: return`   ← returns before locking
```

Every path that awaits a supervisor while holding the lock (`stop`, `restart`,
`delete`, `stop_all`) sets `stop_requested = True` first, so the callback exits before
it can contend for the lock. The only path that *does* take the lock in the callback
is a genuinely unexpected exit, which by definition happens when no lifecycle operation
is in flight for that camera.

Startup failures never reach the supervisor at all: if the process dies inside the
grace window, `start()` handles it inline and the supervisor task is never created.

### Blocking-call discipline

Every SQLite call goes through `await asyncio.to_thread(...)`. Subprocess I/O is
`asyncio`-native. Upload writes are the one place where a blocking `file.write()`
happens on the event loop thread; chunks are 1 MiB and this was accepted for V1 — noted
in §15.

---

## 8. The FFmpeg contract

### Command shape

```
ffmpeg -hide_banner -nostdin -loglevel warning -re
       [-stream_loop -1]                       # only when loop = true, BEFORE -i
       -i <source_path>
       -map 0:v:0 -an                          # video only; audio always stripped
       [-vf <filter chain>]                    # only when needed
       -c:v <libx264|libx265> <codec args>
       <-crf 23 | -b:v Nk -maxrate Nk -bufsize 2Nk>
       -f rtsp -rtsp_transport tcp rtsp://mediamtx:8554/simulator/<stream_path>
```

| Setting | Arguments |
| --- | --- |
| H.264 | `-c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p` |
| H.265 | `-c:v libx265 -preset veryfast -pix_fmt yuv420p -x265-params log-level=error` |
| Auto bitrate | `-crf 23` |
| Fixed bitrate | `-b:v Nk -maxrate Nk -bufsize 2Nk` |
| Fixed resolution | `scale=W:H:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=W:H:(ow-iw)/2:(oh-ih)/2,setsar=1` |
| Fixed FPS | `fps=N` (appended after any scale/pad) |
| Source resolution with odd dimensions | `scale=trunc(iw/2)*2:trunc(ih/2)*2` (yuv420p requires even) |
| Source resolution and source FPS | no `-vf` at all |

`-re` paces the read at real time, which is what makes the output behave like a live
camera rather than a file dump. Fixed resolution preserves aspect ratio and pads,
rather than distorting.

### Invariants

- The command is **always** a `list[str]` executed with `create_subprocess_exec`. There
  is no `shell=True` and no string interpolation into a shell anywhere in the codebase.
- `-stream_loop` must precede `-i` (input option) — asserted by a test.
- `stdin` is `DEVNULL` and `-nostdin` is set, so FFmpeg can never consume the parent's
  stdin or block waiting on a terminal.
- `stdout` is `DEVNULL`; only `stderr` is piped, read line-by-line into a bounded
  `deque(maxlen=50)` so a chatty process cannot grow memory without bound.

### Why audio is dropped

It simulates a video-only IP camera, avoids audio-codec negotiation failures with
MediaMTX, and keeps the command matrix small. The UI still *reports* whether the source
had audio, so the information is not hidden.

---

## 9. Security posture

This is a lab/test tool with **no authentication** (see §14). Within that, the
following are enforced:

| Risk | Mitigation |
| --- | --- |
| Command injection via camera name / path / numbers | argv list + `create_subprocess_exec`; no shell anywhere |
| Path traversal via upload filename | the client filename is used only for display; storage names are `<camera_id>_<uuid4>.<ext>` generated server-side, and `Path(...).name` strips directories |
| Path traversal / symlink escape via mounted `local_path` | `Path.resolve()` (which resolves symlinks), then an explicit containment assertion against the resolved `SOURCE_VIDEO_DIR` |
| Arbitrary host file used as a source | only `/data/local-sources` is accepted; absolute paths outside it are rejected |
| Deleting files outside the upload directory | `delete_stored()` re-checks that the resolved parent equals the upload directory |
| Unbounded upload | size enforced *while streaming*, not from `Content-Length`; staged file removed on breach |
| Non-video / malicious "video" uploads | `ffprobe` must succeed and must find a real video stream (attached-picture cover art is explicitly excluded) |
| Information disclosure in errors | internal paths are stripped from ffprobe and FFmpeg messages before they reach `last_error` or the API (`source_path` → display filename, `DATA_DIR` → `<data>`) |
| Unbounded memory from a chatty child | stderr kept in a 50-line ring buffer per process |
| SQL injection | parameterised statements only |

Mounted sources are read-only at the Docker level (`:ro`) *and* never targeted by any
delete path in code.

---

## 10. REST API reference

Interactive docs at `/docs`; schema at `/openapi.json`.

| Method | Path | Success | Notes |
| --- | --- | --- | --- |
| `GET` | `/health` | `200` / `503` | reports ffmpeg, ffprobe, database, camera count, running publishers |
| `GET` | `/api/cameras` | `200` | list |
| `POST` | `/api/cameras` | `201` | multipart (`payload` + `file`) or JSON |
| `GET` | `/api/cameras/{id}` | `200` | |
| `PATCH` | `/api/cameras/{id}` | `200` | restarts a running camera when needed |
| `DELETE` | `/api/cameras/{id}` | `204` | stops, deletes row, deletes owned upload |
| `POST` | `/api/cameras/{id}/start` | `200` | |
| `POST` | `/api/cameras/{id}/stop` | `200` | |
| `POST` | `/api/cameras/{id}/restart` | `200` | |
| `GET` | `/api/local-sources` | `200` | files in the mounted directory |

### Error envelope

```json
{"error": {"code": "duplicate_stream_path",
           "message": "Stream path 'lobby' is already in use.",
           "details": {"stream_path": "lobby"}}}
```

| Code | HTTP | Cause |
| --- | --- | --- |
| `bad_request` | 400 | malformed multipart/JSON, no source, or two sources |
| `camera_not_found` | 404 | unknown id |
| `duplicate_stream_path` | 409 | explicit stream path already taken |
| `operation_in_progress` | 409 | a lifecycle operation is already running for that camera |
| `validation_error` | 422 | bad configuration; `details.fields[]` names the offending field |
| `invalid_source` | 422 | unreadable/unsupported/out-of-bounds source file |
| `internal_error` | 500 | unhandled |

### Deliberate status-code decision

**A failed start is not an HTTP error.** `POST /start` returns `200` and create-with-
auto-start returns `201`, with `status: "ERROR"` and a populated `last_error`. The
rationale: a publisher can fail at start time or five minutes later, and a client
should read failure from one place (`status` + `last_error`) rather than two. This is
a judgement call and a fair thing to challenge — see §15.

### Validation rules

| Field | Rule |
| --- | --- |
| `name` | required, trimmed, 1–100 chars |
| `stream_path` | `^[a-z0-9][a-z0-9_-]{0,63}$`, unique; auto-generated from the name when omitted |
| `codec` | `h264` \| `h265` |
| resolution | `{"mode":"source"}` or `{"mode":"fixed","width":W,"height":H}`, W/H even, 2–7680 |
| fps | `{"mode":"source"}` or `{"mode":"fixed","value":F}`, `0 < F ≤ 120` |
| bitrate | `{"mode":"auto"}` or `{"mode":"fixed","kbps":N}`, 64–50000 |
| source | exactly one of an uploaded `file` or `{"kind":"local","local_path":...}` |

Unknown fields are rejected (`extra="forbid"`) so typos fail loudly instead of silently
doing nothing.

---

## 11. Frontend

One page, vanilla JS, no build step (the environment has no Node toolchain, and V1
does not need a framework).

- **Cards** show status badge, RTSP URL with copy button, actions, and two deliberately
  separated blocks: **Source video (input file)** — what ffprobe found — and
  **Virtual camera output (RTSP)** — how the stream is encoded. Keeping these visually
  distinct was an explicit requirement: a 1080p/25fps file published as 720p/10fps must
  not look like a contradiction.
- **Add/Edit dialog** with an upload drop zone, a mounted-file dropdown populated from
  `/api/local-sources` (with the mounted-directory caveat spelled out inline), and
  output settings whose dependent inputs appear only when the relevant mode is
  `fixed`. Editing offers "Keep current source".
- **Polling** every 2 s, paused while a dialog is open or a submission is in flight, so
  a status change made anywhere (including a publisher dying on its own) appears
  without a refresh.
- Actions are disabled during `STARTING`/`STOPPING`; delete asks for confirmation.
- The header badge reflects `/health` (ffmpeg, ffprobe, database, publisher count).

### Modal visibility: the bug, and the hardening that followed

**The bug.** Author CSS (`label { display: block }`, `.modal { display: flex }`)
outranks the user-agent `[hidden] { display: none }` rule — author origin beats UA
origin regardless of specificity. So **every element toggled with `el.hidden` stayed
visible**: the Add Camera dialog rendered on page load, and the conditional
Width/Height/FPS/kbps fields never hid.

**The fix, in three layers**, so the dialog's closed state does not depend on any
single rule:

1. *Generic rule* — restated with author-origin weight. Every conditional field
   (`.resolution-fixed`, `.fps-fixed`, `.bitrate-fixed`, `#keep-source-option`,
   panels, banners) depends on this:

   ```css
   [hidden] { display: none !important; }
   ```

2. *Modal-specific contract* — the modal is now closed **by default** rather than
   flex-by-default, so it stays shut even if the generic rule is ever weakened:

   ```css
   .modal { display: none; /* … */ }
   .modal:not([hidden]) { display: flex; }
   .modal[hidden]        { display: none !important; }
   ```

3. *One source of truth in JS* — the `hidden` attribute on `#modal`. No parallel
   `state.modalOpen` flag exists to drift out of sync:

   ```js
   function isModalOpen() { return !el("modal").hidden; }
   function setModalOpen(open) {
     const modal = el("modal");
     modal.hidden = !open;
     modal.setAttribute("aria-hidden", String(!open));
     document.body.classList.toggle("modal-open", open);   // stops background scroll
   }
   ```

   `openModal()`, `closeModal()`, the Escape/backdrop handlers and `tick()` all go
   through these. `init()` calls `setModalOpen(false)` and `syncConditionalFields()`
   before any timer is registered, so the starting state is deterministic whatever the
   browser restored. Polling never calls `openModal()`.

**Why it appeared to persist after the first fix.** `index.html` referenced
`/static/styles.css` and `/static/app.js` at stable URLs and the server sent no
`Cache-Control`, so a browser tab open across a rebuild could keep serving the pre-fix
stylesheet from cache. Two changes remove that class of problem:

- asset URLs carry a version query string
  (`/static/styles.css?v=modal-visibility-20260904`);
- a middleware in `main.py` sets `Cache-Control: no-store, max-age=0` for `/` and
  `/static/*` only — `/api/*` and `/health` are untouched.

Locked down by `tests/test_static_ui.py` (11 tests) and re-verified with a 36-check
headless-Chromium walkthrough (§12).

---

## 12. Testing and verification record

### Automated tests

**143 tests total: 141 unit/API/static-UI (all passing, ~17 s) + 2 integration (both passing, ~36 s).**

| Module | Tests | Covers |
| --- | --- | --- |
| `test_validation.py` | 33 | slugging (incl. unicode folding, length clamp, fallback), stream-path regex, name trimming, every invalid video-config permutation, source-spec rules, partial updates, unknown-field rejection |
| `test_ffmpeg_command.py` | 11 | exact argv for the default case; `-stream_loop` placement; H.265 args; fixed-bitrate args; filter chains for fixed resolution / fixed FPS / both; odd-dimension correction; audio dropped; TCP transport; "is a list of strings, never a shell string" |
| `test_probe.py` | 10 | ffprobe argv; full-payload parsing; missing optional fields → `null`; `0/0` frame rate; audio-only rejection; cover-art-is-not-video; **real ffprobe on an FFmpeg-generated clip**; real rejection of a text file; missing-binary message; internal path not leaked |
| `test_storage.py` | 18 | staging → promotion → generated names; client filename never used as a path; extension allowlist; size cap with cleanup; empty upload; mounted-path resolution incl. subdirectories; traversal rejection (5 shapes); **symlink escape rejection**; missing file; listing; delete confined to the upload directory |
| `test_lifecycle.py` | 26 | process manager: startup grace, early exit with stderr, missing binary, duplicate start refusal, requested vs. unexpected exit flags, clean exit, **force kill of a SIGTERM-ignoring process**, stop-all. Manager: create with/without auto-start, auto-start failure → `ERROR`, idempotent start/stop, restart, runtime-affecting update restarts, cosmetic update does not, source replacement deletes the old file, delete cleanup, non-looping EOS → `STOPPED`, unexpected exit → `ERROR`, looping exit-0 → `ERROR`, independent cameras, duplicate names → distinct paths, shutdown stops all, **startup reconciliation of stale state + auto-start**, missing source file |
| `test_static_ui.py` | 11 | dialog markup starts `hidden` + `aria-hidden="true"`; every conditional block ships hidden; assets are version-stamped; `[hidden] … !important` present; `.modal` is display-none by default with `:not([hidden])`/`[hidden]` rules; body scroll-lock rule; JS goes through `isModalOpen`/`setModalOpen` with no direct `hidden` writes left; `init()` forces a closed state before timers; `tick()` never opens the modal; `/`+`/static/*` send `no-store` while `/api/*` and `/health` do not |
| `test_api_cameras.py` | 32 | health, index, list/get/create/patch/delete, mounted-source create, local-source listing, every error path (duplicate, missing source, unprobeable, bad extension, invalid config, invalid path, traversal, both sources, malformed multipart, wrong content type, 404s), lifecycle endpoints, start failure → `ERROR`, unexpected exit → `ERROR`, non-looping EOS → `STOPPED`, multiple cameras, OpenAPI |
| `test_integration_rtsp.py` | 2 | **real** publish through MediaMTX, read back with `ffprobe -rtsp_transport tcp`, verify codec+dimensions, stop → path gone, restart → readable; two cameras publishing independently |

**Test design note.** Process supervision is tested against real child processes using
five stub shell scripts (long-running, instant-fail-with-stderr, survive-then-exit-0,
survive-then-crash, ignore-SIGTERM) with the grace period lowered to 0.3 s. This
exercises the actual `asyncio` subprocess machinery — spawn, pipes, signals, kill —
rather than mocks, while staying fast and deterministic. `ffprobe` is faked at the
service boundary for API tests and used for real in `test_probe.py`.

### Manual end-to-end verification (Docker, `docker compose up --build`)

| # | Scenario | Result |
| --- | --- | --- |
| 1 | Upload → auto-start → `ffprobe rtsp://localhost:8554/simulator/parking-entrance` | `h264 320x240 15/1` — **video → FFmpeg → MediaMTX → RTSP client confirmed** |
| 2 | Stop | RTSP path returns `404 Not Found` |
| 3 | Start again | readable again |
| 4 | Edit to 640×480 / 10 fps / 800 kbps | **automatic restart** (pid 79 → 112); RTSP now reports `h264 640x480 10/1` |
| 5 | Edit name only | same pid — no interruption |
| 6 | Second camera from a mounted file, H.265 | `hevc 320x240 12/1`; both cameras publishing independently |
| 7 | `docker compose stop mediamtx` | both cameras → `ERROR` within 1 s with real FFmpeg stderr; simulator stayed healthy; start during the outage failed cleanly |
| 8 | MediaMTX back + Restart | both `RUNNING`, `last_error` cleared, streams readable |
| 9 | Non-looping camera, real 10 s clip | → `STOPPED` at end of stream, `last_error: null` |
| 10 | `docker compose down` + `up` (twice, incl. `--build`) | cameras persisted; auto-start camera resumed publishing; a stale `RUNNING` row with `auto_start=false` reconciled to `STOPPED` |
| 11 | Delete a running camera | `204`; uploaded file removed from `/data/videos`; mounted source untouched; other cameras unaffected |
| 12 | 5 concurrent `POST /start` | exactly **one** `ffmpeg` in `/proc` |
| 13 | Rapid stop/start churn (×3) | exactly one publisher, still readable; no orphans |
| 14 | `docker compose stop` | clean shutdown **< 1 s**: `stopping_all_publishers` → `requested=True` → no spurious `ERROR` |
| 15 | Invalid inputs: non-video, bad extension, duplicate path, traversal, odd width, no source, unknown id | `422 / 422 / 409 / 422 / 422 / 400 / 404`, each with the right code; `/data/tmp` and `/data/videos` left clean |
| 16 | UI rendering (headless Chromium screenshot) | cards, badges, source-vs-output split render correctly |
| 17 | UI dialog (headless Chromium click-through) | modal hidden on load; opens on click; "Keep current source" hidden in add mode; Width/Height/FPS/kbps hidden until mode = fixed; upload/mounted panels swap correctly |
| 18 | **Full modal walkthrough, headless Chromium — 36 assertions, all passing** | see below |

### Modal walkthrough (verification #18)

Driven with Puppeteer against the running Compose stack, re-querying cards on every
interaction because the 2 s poll replaces the list:

| Area | Checks | Result |
| --- | --- | --- |
| Landing page | modal `display:none`, `aria-hidden="true"`, body not scroll-locked, cards render, Add Camera is hit-testable (proves no invisible overlay) | 5/5 |
| Open + every close path | open sets `flex`/`aria-hidden=false`/scroll-lock; closes via `x`, Cancel, Escape, backdrop click | 7/7 |
| Conditional fields | add mode hides fixed inputs and keep-source, shows the upload panel; switching to fixed modes reveals Width/Height/FPS/kbps; mounted panel swaps in | 2/2 |
| Validation, both layers | native `step="2"` blocks an odd width client-side; a duplicate stream path returns 409 and surfaces in `#form-error`; dialog stays open and closable; entered values preserved; nothing created | 6/6 |
| Create by upload | filename echoed in the drop zone; submit closes the dialog; card appears | 2/2 |
| Edit | opens populated with "Keep current source" preselected; Cancel leaves the camera unchanged; rename closes the dialog and updates the card | 4/4 |
| Persistence of closed state | modal stays closed across ~10 s of polling (5 ticks); still closed after a browser refresh | 2/2 |
| Lifecycle from the UI | Start → `RUNNING`, Restart → `RUNNING`, Stop → `STOPPED`, Copy shows a toast, Delete removes the card | 8/8 |

RTSP was re-checked afterwards: running cameras readable (`h264 640x480 15/1`,
`h264 1280x720 10/1`), the stopped one correctly `404`.

### Issues found and fixed during verification

1. **ffprobe error leaked the internal staging path** (`/data/tmp/staging_….mp4`) into
   the API response. Fixed by substituting the display filename; regression test added.
2. **`[hidden]` overridden by author CSS** (§11) — the Add Camera dialog rendered on
   page load. Fixed and re-verified in a real browser.
3. **Unversioned, cacheable UI assets** — a tab open across a rebuild could keep the
   pre-fix stylesheet, making the fix look ineffective. Fixed with version-stamped
   asset URLs plus `Cache-Control: no-store` on `/` and `/static/*` (§11).

---

## 13. Known issues and limitations

**Functional**

- `RUNNING` means *the managed FFmpeg process started cleanly and is still alive*. It is
  not a continuous assertion that the RTSP endpoint is readable (deliberate, per the V1
  brief). A publisher wedged without exiting would still read as `RUNNING`.
- A camera's persisted status can be `RUNNING` while the container is down; it is
  corrected at the next startup, not at shutdown.
- No migration framework. Schema changes to `cameras` in a future version will need
  either a migration step or a volume reset.
- One uploaded file per camera; uploads are not shared between cameras.
- Non-looping cameras end in `STOPPED` and must be started manually again.
- `stream_path` de-duplication for *generated* slugs is a read-then-insert; two
  simultaneous creates with the same name could theoretically collide, in which case
  the unique index rejects one with `409` rather than corrupting anything.

**Operational**

- Publishers are children of the simulator container, so capacity is bounded by that
  container's CPU. Encoding several 1080p cameras at once is CPU-bound; there is no
  admission control or per-camera CPU limit.
- No metrics endpoint, no structured (JSON) logs, no log rotation beyond Docker's.
- Verified on arm64 (Apple Silicon) only. Uploads near the 2 GiB cap were not tested.

**UI**

- The RTSP URL *preview* in the dialog is composed client-side from
  `window.location.hostname` and a hard-coded `:8554`, so it can differ from the
  authoritative URL when `PUBLIC_RTSP_HOST`/`PORT` are overridden. The card always shows
  the real URL from the API; the preview is cosmetic.
- Drag-and-drop is the one UI path not exercised by automation (it needs a real
  drag gesture); `<input type=file>` selection, which shares the same `selectFile()`
  handler, is covered. Clipboard copy is covered via its toast confirmation.
- The static-UI tests assert the served CSS/JS/HTML *text*, not computed styles. That is
  sound only because there is no CSS build or minification step; if one is added, these
  assertions must be replaced with a real browser check.
- No optimistic UI: actions wait for the request, then refresh.

**Testing**

- Upload writes use blocking `file.write()` on the event loop thread in 1 MiB chunks.
  Acceptable at this scale; would matter with many concurrent large uploads.
- No load/soak testing, no chaos testing beyond stopping MediaMTX.

---

## 14. Deliberate non-goals for V1

Explicitly out of scope, by the plan: VMS features, recording, playback, preview or
WebRTC; authentication/authorisation on the UI, API or RTSP; ONVIF, discovery, PTZ;
audio publishing; AI/analytics/RAG; Kubernetes or multi-node scaling; and advanced
failure injection (packet loss, jitter, latency, credential failures). Stopping a
camera is V1's way of taking one offline.

**Run this on a trusted network.** Anyone who can reach port 8080 can create, modify
and delete cameras, and upload files.

---

## 15. Open questions and candidate gaps for review

These are the places where the implementation made a judgement call, or where a
reviewer might reasonably find something missing. They are listed as questions, not
defects.

**API semantics**

1. *Failed start returns `200` with `status: ERROR`.* Should an explicit `POST /start`
   that fails instead return `502`/`409` with the error envelope? Current reasoning is
   in §10; the counter-argument (clients shouldn't have to inspect a body to learn a
   command failed) is legitimate.
2. *`409 operation_in_progress` instead of queueing.* Should a second lifecycle request
   wait for the lock instead of being rejected? Rejecting matches the plan; queueing
   would be friendlier to scripted clients.
3. *Generated stream paths are auto-de-duplicated (`lobby-2`); explicit ones `409`.*
   This is an addition beyond the plan's letter. Is the asymmetry right?
4. *`GET /api/local-sources` is an addition* (not in the plan) made so the UI can offer
   a picker rather than a free-text path. Worth confirming it is wanted.
5. Should `PATCH` support clearing `last_error` explicitly, or is "stop clears it"
   enough?

**Lifecycle and state**

6. Is a fixed 2 s startup grace the right health definition for large files or slow
   hosts, or should it be per-camera / adaptive?
7. Should a camera that fails auto-start at boot be retried (with backoff), or is
   leaving it in `ERROR` for a human correct?
8. Should the simulator ever restart a publisher that died unexpectedly (supervision
   with a retry budget), or is manual restart the right V1 behaviour? Currently: no
   automatic retry, ever.
9. Non-looping cameras end in `STOPPED`. Should that be a distinct state (e.g.
   `COMPLETED`) so it is distinguishable from a user-initiated stop?

**Persistence and data**

10. No migrations. Acceptable for V1; what is the intended upgrade path?
11. Uploads are camera-owned. Would a shared "media library" (many cameras, one file)
    be a better model for the next version?
12. Nothing prunes `/data/videos` on a database reset; orphaned files would need a
    manual sweep. Should there be a startup reconcile for files with no row?

**Operational**

13. No limit on how many cameras can run at once. Should there be a configurable cap,
    or CPU-aware admission control?
14. `/health` reports dependency presence but not per-camera health. Should it expose
    a per-camera summary for external monitoring?
15. Logs are human-readable text. Should they be JSON for ingestion?
16. Should uploads be moved off the event loop thread (`to_thread`) before this sees
    real concurrent use?

**Security**

17. No auth at all. What is the intended deployment boundary — always localhost/lab, or
    will this ever be exposed? If the latter, auth on both HTTP and RTSP is the first
    thing to add.
18. Uploaded files are trusted after ffprobe accepts them. FFmpeg parsing untrusted
    media is a real attack surface; is container isolation considered sufficient?

**Scope**

19. The plan names "failure injection" as the natural next step. Which failures matter
    most for your use case — stream drop, frame stall, bitrate collapse, credential
    rejection, partial packet loss?

---

## 16. Operating the system

```bash
# Start (UI at http://localhost:8080, API docs at /docs)
docker compose up --build

# Watch a camera
ffplay -rtsp_transport tcp rtsp://localhost:8554/simulator/<stream_path>
ffprobe -rtsp_transport tcp -show_streams rtsp://localhost:8554/simulator/<stream_path>

# Tests
docker compose run --rm --no-deps simulator pytest -q      # 141 unit/API/static-UI tests
docker compose exec simulator pytest -m integration -v     # 2 E2E tests (stack must be up)

# Logs
docker compose logs -f simulator

# Stop (cameras persist) / wipe everything
docker compose down
docker compose down -v

# Different ports
SIMULATOR_HTTP_PORT=9080 PUBLIC_RTSP_PORT=9554 docker compose up --build
```

### Log events to grep for

`camera_created`, `camera_updated`, `camera_starting`, `camera_started`,
`camera_stopping`, `camera_stopped`, `camera_restart`, `camera_deleted`,
`ffmpeg_started`, `ffmpeg_exit`, `ffmpeg_exit_unexpected`, `ffmpeg_start_failed`,
`ffmpeg_force_kill`, `ffmpeg_error`, `startup_reconciled`, `stopping_all_publishers`,
`camera_source_missing`. Every line carries `camera_id=` and usually `stream_path=`.

---

## 17. Appendix: worked examples

### Create from an upload, running immediately

```bash
curl -X POST http://localhost:8080/api/cameras \
  -F 'payload={"name":"Parking Entrance","auto_start":true,"loop":true,
                "source":{"kind":"upload"},
                "video":{"codec":"h264",
                         "resolution":{"mode":"fixed","width":1280,"height":720},
                         "fps":{"mode":"fixed","value":15},
                         "bitrate":{"mode":"fixed","kbps":2500}}}' \
  -F 'file=@/path/to/clip.mp4'
```

### Create from a mounted file (pure JSON)

```bash
curl -X POST http://localhost:8080/api/cameras \
  -H 'content-type: application/json' \
  -d '{"name":"Lobby","source":{"kind":"local","local_path":"sample-320x240.mp4"}}'
```

### Change the output — a running camera restarts itself

```bash
curl -X PATCH http://localhost:8080/api/cameras/cam_ab12cd34 \
  -H 'content-type: application/json' \
  -d '{"video":{"resolution":{"mode":"fixed","width":640,"height":480}}}'
```

### Camera response

```json
{
  "id": "cam_ab12cd34",
  "name": "Parking Entrance",
  "stream_path": "parking-entrance",
  "rtsp_url": "rtsp://localhost:8554/simulator/parking-entrance",
  "auto_start": true,
  "loop": true,
  "status": "RUNNING",
  "video": {
    "codec": "h264",
    "resolution": {"mode": "fixed", "width": 1280, "height": 720},
    "fps": {"mode": "fixed", "value": 15},
    "bitrate": {"mode": "fixed", "kbps": 2500}
  },
  "source": {
    "filename": "clip.mp4", "format": "mov,mp4,m4a,3gp,3g2,mj2",
    "duration_seconds": 222.4, "video_codec": "h264",
    "width": 1920, "height": 1080, "fps": 29.97,
    "bitrate_kbps": 2400, "has_audio": true, "size_bytes": 69500000
  },
  "source_kind": "upload",
  "runtime": {"pid": 123, "started_at": "2026-09-04T10:00:00Z"},
  "last_error": null,
  "created_at": "2026-09-04T09:59:58Z",
  "updated_at": "2026-09-04T10:00:00Z"
}
```

### Actual generated FFmpeg command (looping, source resolution, H.264, auto bitrate)

```
ffmpeg -hide_banner -nostdin -loglevel warning -re -stream_loop -1 \
  -i /data/videos/cam_ab12cd34_9f3c….mp4 \
  -map 0:v:0 -an \
  -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -crf 23 \
  -f rtsp -rtsp_transport tcp rtsp://mediamtx:8554/simulator/parking-entrance
```

### Actual generated FFmpeg command (non-looping, 1280×720 @ 15 fps, H.265, 2500 kbps)

```
ffmpeg -hide_banner -nostdin -loglevel warning -re \
  -i /data/local-sources/clip.mp4 \
  -map 0:v:0 -an \
  -vf scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,\
pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=15 \
  -c:v libx265 -preset veryfast -pix_fmt yuv420p -x265-params log-level=error \
  -b:v 2500k -maxrate 2500k -bufsize 5000k \
  -f rtsp -rtsp_transport tcp rtsp://mediamtx:8554/simulator/clip-cam
```

---

*End of handoff. The user-facing guide is in [README.md](README.md); the original
requirements are in `RTSP Camera Simulator V1 Plan.md`.*
