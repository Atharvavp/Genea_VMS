# Standalone Video Analytics & Event Detection (Component 4) — Engineering Handoff

> **Historical document — preserved as audit evidence, not re-validated here.**
> This handoff records the implementation and validation of Video Analytics (Component 4) as it was
> carried out in its own branch and working clone. Every command, count, measurement
> and acceptance result below was executed **there**, before the unified-submission
> refactor, and is reproduced unchanged. It is **not** a claim about the unified
> repository.
>
> The implementation it describes now lives at `services/video-analytics/`, imported byte-identically
> from `feat/video_analytics` at `786062c64fd51c20bafb02c04031ad8558c925de`.
> Validation actually executed against the unified repository is recorded separately in
> [HANDOFF_unified_submission.md](HANDOFF_unified_submission.md).

---

**Repository:** `Genea_VMS` (analytics clone, `ai-services/Genea_VMS`)
**Component:** Standalone Video Analytics & Event Detection (Component 4)
**Branch:** `feat/video_analytics`
**Base commit at start:** `bdd4b72987da6aa0c44b73934646793eea9748bb` (`Initial commit`, one file: `README.md`)
**Status:** Implemented, tested, and verified end-to-end in Docker against the running VMS
**Date of handoff:** 2026-09-07
**Plan implemented:** `PLAN_component_4.md` (P0 only)

> **How to review this document.** §§1–9 describe what was built and why. §10 is
> the verification record — what was actually executed, with its output, not what
> was intended. §§11–13 are the honest parts: deviations from the plan and the
> evidence for each, known limitations, and the runtime validations that remain
> open. A reviewer hunting for weaknesses should start at §11.

---

## Table of contents

1. [What was built](#1-what-was-built)
2. [Architecture as implemented](#2-architecture-as-implemented)
3. [Repository layout](#3-repository-layout)
4. [State ownership and concurrency](#4-state-ownership-and-concurrency)
5. [The media path](#5-the-media-path)
6. [Detection, tracking, and crossing](#6-detection-tracking-and-crossing)
7. [Event durability](#7-event-durability)
8. [External contracts](#8-external-contracts)
9. [Security posture](#9-security-posture)
10. [Verification record](#10-verification-record)
11. [Deviations from the plan, with evidence](#11-deviations-from-the-plan-with-evidence)
12. [Known limitations](#12-known-limitations)
13. [Runtime validations still open](#13-runtime-validations-still-open)
14. [Files added](#14-files-added)
15. [Environment state created for acceptance](#15-environment-state-created-for-acceptance)

---

## 1. What was built

A single-process FastAPI service plus a dependency-free dashboard that consumes
H.264 RTSP paths already published by the VMS MediaMTX instance, runs CPU-only
YOLO11n detection and ByteTrack tracking per camera, and records an event each
time a confirmed track crosses a configured finite line in an allowed direction.

Roughly **7,100 lines of application Python**, **1,700 lines of frontend**
(HTML + CSS + JS, no build step), **7,900 lines of tests**, and **515 lines of
operational scripts**.

**Every VMS-isolation constraint in the plan holds and was verified at runtime:**

| Constraint | How it is enforced | Verified by |
| --- | --- | --- |
| No VMS code imported | Nothing in `app/` imports from the VMS clone; the container has no VMS source | `test_static_ui`, `grep`, image contents |
| No VMS database or recording filesystem access | No VMS volume is mounted; `docker-compose.yml` declares only `analytics-data` | Compose file, container mounts |
| No MediaMTX Control API call | Port 9997 is neither published nor referenced; the only outbound HTTP is `{VMS_API_BASE_URL}/api/recordings` | `test_static_ui`, `VMSRecordingClient` |
| No MediaMTX path mutation | RTSP is read-only; nothing writes to MediaMTX | design + acceptance §10.7 |
| Failure isolation | Stopping analytics leaves VMS live and recording untouched | acceptance §10.7 step 13 |

---

## 2. Architecture as implemented

```
Browser  http://localhost:8100
  vanilla JS: Cameras | Configure (snapshot + canvas) | Events
        │ JSON / JPEG
┌───────┴──────────────────────────────────────────────────────────────┐
│ analytics container — one Uvicorn process, one worker, :8100         │
│                                                                      │
│ FastAPI event loop                                                   │
│   ├── REST / static routes                                           │
│   ├── AnalyticsCameraManager   (desired state + worker registry)     │
│   ├── EventService             (read-only; cannot touch a worker)    │
│   └── VMSRecordingClient ── HTTP read only ──► VMS API :8090         │
│                                                                      │
│ process-wide YoloDetector  (CPU; non-queuing admission lock)         │
│                                                                      │
│ one non-daemon thread per enabled camera                             │
│   rtspsrc(TCP) → rtph264depay → h264parse → avdec_h264               │
│     → videoconvert → RGB caps → appsink(max-buffers=1, drop)         │
│     → cadence gate → detector → ByteTrack → crossing → event writer  │
│                                                                      │
│ /data/analytics.db    /data/events/<camera>/<day>/<event>/*.jpg      │
└───────┬──────────────────────────────────────────────────────────────┘
        │ TCP RTSP, read only
        ▼   VMS MediaMTX host port :8555
```

One Uvicorn worker is mandatory: multiple processes would create duplicate
stream workers and break the in-memory registry that owns them.

---

## 3. Repository layout

```
app/
  main.py                 app factory, lifespan ordering, /health, static mount
  config.py               frozen Settings, every validator in PLAN §22.1
  logging_config.py       one key/value record format, rate limiter, key guard
  api/       errors.py cameras.py lines.py events.py
  domain/    models.py    enums, ids, timestamps, records, request/response models
  persistence/            database.py + camera/line/event repositories + event_storage.py
  security/  rtsp_url.py event_paths.py
  analytics/ types.py sampling.py detector.py tracker.py crossing.py
             gst_pipeline.py stream_worker.py
  services/  camera_manager.py event_service.py vms_recordings.py presentation.py
  static/    index.html app.js styles.css
scripts/     validate_runtime.py verify_event_store.py seed_ultralytics.py
tests/       conftest.py, fakes/, fixtures/, unit/, integration/, e2e/
requirements/ torch-cpu.txt runtime.lock test.lock   (all hash-locked)
```

`app/services/presentation.py` is the one addition to the planned tree; see §11.

---

## 4. State ownership and concurrency

| State | Owner | Protection |
| --- | --- | --- |
| Camera and line desired state | SQLite | per-camera `asyncio.Lock`, `BEGIN IMMEDIATE` |
| Worker registry, runtime snapshots | `AnalyticsCameraManager` | short `threading.RLock` sections that only swap references |
| Pipeline, sampler, tracker, crossing state | one `StreamWorker` | thread confinement |
| Latest decoded frame | worker | dedicated `threading.Lock`; the array is copied under it and encoded outside it |
| Hot `WorkerConfig` | worker | dedicated config lock; adopted atomically at the top of an iteration |
| Shared model | `YoloDetector` | `acquire(blocking=False)` — losing admission drops the opportunity |
| Event rows and images | `EventStorage` | DB transaction + atomic rename |

Rules that are actually enforced in code, not merely documented:

* the event loop never waits on a `threading.Lock` around slow work;
* the manager never holds the registry lock while awaiting a DB call, joining a
  thread, or performing HTTP;
* a worker callback is discarded unless **both** `camera_id` and
  `worker_instance_id` match the current registry entry
  (`test_camera_manager::test_a_callback_from_a_retired_instance_is_ignored`);
* GStreamer's `pad-added` callback, which runs on a streaming thread, only
  inspects and links its own pad and sets fixed-size flags under `_pad_lock`.

`worker_instance_id` fences retired threads; `worker_session_id` defines tracker
and deduplication continuity. They are deliberately different identifiers.

### Worker state machine

`DISABLED → STARTING → RUNNING`, with `RECONNECTING` on any recoverable source
failure and `ERROR` on a fatal one. `RUNNING` is entered on the first valid RGB
frame and is about frames, not events: no line, no detections, a cadence skip, or
a detector-busy drop all leave a live camera `RUNNING`.

Runtime is published on every transition **plus a one-second heartbeat**, so the
registry's frame age and counters stay current without taking the registry lock
at frame rate (see §11).

---

## 5. The media path

* `rtspsrc` with `protocols=TCP` (numeric `4`), `latency=200`,
  `drop-on-latency=true`, `tcp-timeout=5 s`.
* `appsink` with `emit-signals=false`, `sync=false`, `max-buffers=1`,
  `drop=true`, `wait-on-eos=false`, polled with `try_pull_sample(200 ms)` so all
  sample handling stays on the worker thread.
* Dynamic pads are filtered on `application/x-rtp` + `media=video` +
  `encoding-name=H264`. Audio pads are counted and ignored; a video pad with any
  other encoding is a **fatal** `unsupported_codec`, not a retry storm.
* Frames are copied out stride-aware into owned, C-contiguous `uint8` arrays.
* Recoverable failures retry forever with `min(30, 2**n) × uniform(0.8, 1.2)`
  seconds; the attempt counter resets only after a valid frame.
* Every attempt and every exit sets the pipeline to `NULL` and waits at most
  2 seconds for the state change.

---

## 6. Detection, tracking, and crossing

* **Detector.** One `YoloDetector` for the process. `YOLO.predict` (never
  `YOLO.track`) with `device="cpu"`, `imgsz=640`, `verbose=False`, and the
  camera's confidence. RGB is converted to contiguous BGR because that is
  Ultralytics' ndarray convention. Only COCO ids 0, 1, 2, 3, 5, 7 are accepted;
  `train` and `boat` are deliberately **not** remapped to `vehicle`.
* **Tracker.** `trackers.ByteTrackTracker` instantiated directly, one per worker
  session, with every constructor value passed explicitly. Monotonic timestamps
  are forwarded so dropped and busy frames express real elapsed time.
* **Crossing.** `signed_distance(P) = cross(B - A, P - A) / |B - A|`, a 0.01
  normalised deadband, a finite-segment intersection test with `1e-9` tolerance,
  and a per-track anchor that is always advanced after a transition is evaluated
  — so an extension-only crossing cannot fire later without another genuine side
  change.

The browser overlay draws its arrow along the same positive normal
`n = (-dy, dx)/|v|`, and a browser test asserts the arrow really is drawn into
the B half-plane
(`test_analytics_ui::test_the_direction_arrow_points_into_the_b_half_plane`).

---

## 7. Event durability

```
encode both JPEGs → validate non-empty → hidden temp dir (0700)
→ write + fsync each file (0640) → fsync temp dir
→ atomic os.rename into place → fsync day parent → INSERT row
```

A committed row therefore never intentionally points at a partially written
image. The one window this admits — a final directory that exists before its row
commits — is closed by `reconcile_startup()`, which runs before any worker starts.

* Failure before the rename removes only the validated temp directory.
* Failure after the rename removes only that generated final directory.
* A unique conflict at insert removes the loser and returns the existing row.
* **A committed row is never deleted because its image is missing.** The image
  route returns 410 and the row stays; `verify_event_store.py` reports it.

Deduplication is identical in memory and in SQL:
`(camera_id, line_id, worker_session_id, track_id, direction)`. The in-memory key
is recorded **only after** persistence returned `INSERTED` or proved
`ALREADY_EXISTS`.

---

## 8. External contracts

**Component 2 (live view).** Consumed as RTSP only, at
`rtsp://host.docker.internal:8555/vms_<camera-id>`. `vms_camera_id` is validated
against `^cam_[0-9a-f]{8}$` and then treated as an opaque string; no MediaMTX
path is ever derived from it or called.

**Component 3 (recording).** One bounded `GET /api/recordings?camera_id&date`
per user action, no retry, with a connect/read/write/pool budget of
2 / 5 / 2 / 2 seconds. Timestamps are parsed as timezone-aware UTC, the provided
`end_time` must agree with `start + duration_seconds` to within one millisecond,
and matching is half-open `start <= crossed_at < end`. The **VMS's own**
`playback_url` is returned verbatim; Component 4 never proxies or rewrites it.

---

## 9. Security posture

No authentication; trusted local network only. Within that:

* non-root uid 10001, `cap_drop: ALL`, `no-new-privileges`, no Docker socket;
* raw RTSP credentials exist only in SQLite and the `rtspsrc location` property,
  and are scrubbed in three layers (known URL → known secret → generic
  `scheme://userinfo@`) before any log, error, or response;
* stored image paths are built solely from validated ids and a parsed UTC date,
  resolved beneath the data root, and refused if any component is a symlink;
  no endpoint accepts a path;
* event page cursors are HMAC-SHA256 signed and bound to their filter set;
* `X-Content-Type-Options`, `Referrer-Policy`, and a restrictive CSP with
  `frame-ancestors 'none'` on every response;
* every dependency is hash-locked and installed with `--require-hashes`; the
  model is checksum-verified at build **and** at load; the runtime image has no
  compiler, no `curl`, and no download path.

Credentials are **not** encrypted at rest in SQLite.

---

## 10. Verification record

Everything in this section was executed on this machine against this code. Where
a number appears, it is a measurement, not an estimate.

**Host and toolchain**

| Item | Value |
| --- | --- |
| Host | macOS, `arm64` (Apple Silicon) |
| Docker | server 29.7.2, `linux/arm64`; Compose v5.5.0 |
| Base image | `ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517` |
| Analytics image | `genea-analytics-analytics:latest`, 2.93 GB |
| Tests image | `genea-analytics-tests:latest`, 4.52 GB (adds Playwright + Chromium) |
| Branch / base commit | `feat/video_analytics` / `bdd4b72` |

**Resolved runtime, read out of the built image**

```
ok    platform machine=aarch64 system=Linux python=3.12.3
      import fastapi==0.116.1   uvicorn==0.35.0   pydantic==2.13.5
      import pydantic_settings==2.10.1   httpx==0.28.1   numpy==2.5.3
      import PIL==11.3.0   supervision==0.30.2   trackers==2.6.0
      import ultralytics==8.4.49
ok    gstreamer core=1.24.2 factories=6 gstrtsp_typelib=present
ok    torch torch=2.7.1+cpu torchvision=0.22.1 cuda=False
ok    model-metadata path=/opt/models/yolo11n.pt bytes=5613764
      sha256=0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1
ok    tracker bytetrack adapter constructed, updated and reset
ok    inference blank_frame_detections=0
```

The model checksum matches the value frozen in the plan exactly. Every Ubuntu
package version pinned in `PLAN` §15.1 resolved as written, with no substitution.

### 10.1 Automated test results

| Tier | Selection | Tests | Result |
| --- | --- | --- | --- |
| unit | `-m unit` | 524 | **passed** (76 deselected), 15.4 s |
| integration | `-m 'integration and not real_model and not four_camera'` | 38 | **passed** |
| real model | `-m real_model` | 10 | **passed**, 6.4 s |
| four camera | `-m four_camera` | 2 | **passed**, six runs on an unloaded host; see §10.5.1 |
| browser | `-m e2e` | 26 | **passed**, 35.1 s |

Total: **600 automated tests**, all passing on an unloaded host.

The unit tier runs with no network, no GStreamer, and no model, using injected
clocks, id generators, and fakes. The integration and browser tiers run **inside
the built image**, so a host PyGObject or Torch difference cannot mask a failure.

### 10.2 What the real-GStreamer suite proves

Against a private MediaMTX fixture and an ffmpeg publisher on an isolated
network — never the VMS:

1. all six required factories exist in the final image;
2. a real H.264/TCP stream produces ≥ 10 owned, writable, C-contiguous
   `(240, 320, 3)` `uint8` frames whose content spans black to white;
3. an H.264 **+ AAC** source exposes ≥ 2 pads, the audio pad is ignored, and
   video keeps flowing;
4. after a 2-second stall the sink hands back *current* frames, not a ~50-frame
   backlog, and `max-buffers=1 / drop=true / sync=false` are the live properties;
5. `stop()` reaches `NULL` from the owning thread in well under 2.5 s, and is
   safe on a pipeline that never started;
6. losing the publisher ends the attempt as error/EOS/stall, and republishing
   recovers on a rebuilt pipeline;
7. an **H.265** source is reported fatally as `unsupported_codec`, with the
   message naming H.264 as the supported encoding — no retry storm;
8. a credentialed URL that fails to connect leaks neither the password nor the
   username into the error, the code, or captured logs at DEBUG level.

### 10.3 What the real-model suite proves

* `torch 2.7.1+cpu`, `torchvision 0.22.1`, `ultralytics 8.4.49`,
  `torch.cuda.is_available() is False`;
* the official Ultralytics sample image yields both a `person` and a `bus` above
  0.5 confidence, with the original COCO ids (0 and 5) preserved and categories
  normalised to `person` / `vehicle`;
* every returned box lies inside the frame with `0 <= conf <= 1`;
* a blank frame yields zero detections;
* **measured warm inference at `imgsz=640`: 53.5 ms and 55.4 ms** across two runs
  on this host (the plan's spike recorded ~94 ms);
* three repeated inferences create and modify **no** file under `/opt/models`,
  `/opt/venv/lib`, or `/srv/analytics/app`, and `/opt/models` still contains
  exactly `yolo11n.pt` — no runtime download.

### 10.4 What the real-tracker suite proves

Against `trackers 2.6.0` + `supervision 0.30.2`:

* the frozen constructor parameter set is accepted;
* a track is confirmed after exactly `minimum_consecutive_frames` observations;
* the id is stable across motion and survives one missed observation;
* two objects get distinct ids, and class, category, confidence, and box all
  survive the round trip;
* **a track id that aged out is never reused within a session**;
* two adapters are independent, and `reset()` drops continuity on that instance
  only;
* the configured camera confidence really is the activation floor — detections
  at 0.35 never become tracks when the threshold is 0.6;
* aging behaves correctly at 1, 5, and 10 FPS.

### 10.5 Four-camera isolation

Two tests, run three consecutive times (76.6 s, 75.3 s, 76.1 s), all passing.
Over a 60-second steady interval with four live H.264 sources and one shared
detector, the suite asserts and observed:

* four live threads with four distinct worker instances and four distinct
  sessions;
* every camera received frames and completed **≥ 5 inferences**, with **no
  camera making zero progress for more than 15 s** while the detector was
  healthy;
* **maximum simultaneous inference = 1** — the process-wide lock serialises;
* each camera's latest-frame sequence advanced independently;
* real events were written and correctly attributed, each with a non-empty
  `frame.jpg` and `crop.jpg` under its own camera directory;
* stopping one publisher moved **only** that camera to `RECONNECTING` while the
  other three kept receiving frames; restoring it produced a fresh session;
* stopping one worker did not reset any peer's session;
* `reconcile_startup()` afterwards reported zero temp directories, zero orphans,
  zero rows with missing artifacts, and zero unknown paths;
* an independent RTSP consumer could still read a source after the analytics
  workers stopped.

A representative single-camera counter set captured mid-run:
`frames_received=933, inferences_run=99, inference_busy_drops=161,
events_recorded=6`. The busy-drop count is the design working as intended: the
shared detector refused admission and the worker dropped that opportunity rather
than queueing.

### 10.5.1 The starvation gate did fail once — under host oversubscription

This must not be glossed over, because `PLAN` §22.8 makes starvation a P0
acceptance failure that requires an explicit architecture revision rather than a
silently added scheduler.

**What happened.** During the final scripted validation pass, the four-camera
tier failed:

```
AssertionError: a camera made no inference progress for 42.5s while the
detector was healthy; the non-blocking admission policy is starving it
assert 42.52563239399933 < 15.0
```

**The conditions.** That run was not isolated. The **production analytics
service was simultaneously up with five cameras running the real YOLO11n model
at ~172–200% CPU** on a 10-CPU Docker VM, while the test started four more
workers with their own decoders. Nine concurrent H.264 decoders plus two
competing inference paths saturated the host.

**What was measured afterwards.** With the production service stopped, the same
tier passed again. Counting every execution of this tier on this machine:

| Condition | Runs | Result |
| --- | ---: | --- |
| Host not oversubscribed | 6 | **passed** (3 consecutive, then 3 more) |
| Production service also running 5 real-model cameras | 1 | **failed**, 42.5 s gap |

**Diagnosis, and why the test was made more precise.** A 42.5-second gap is
consistent with the affected camera having gone to `RECONNECTING` — a worker
that cannot decode within its 10-second stall deadline ends the attempt and
backs off 1, 2, 4, 8, 16, 30 s — which is a *host capacity* failure, not the
detector lock denying a running camera. The original assertion could not tell
those apart, so it now:

* accumulates a gap **only while the camera is `RUNNING`**, which is exactly the
  plan's "starved while the detector remains healthy";
* reports the states each camera was observed in and how long each spent outside
  `RUNNING`;
* fails separately, with a different message, when cameras spent most of the
  window not `RUNNING` — pointing at source or host capacity instead.

The 15-second threshold is unchanged and nothing was weakened to make the tier
pass. **No queue, executor, or scheduler was added.**

**What this means for acceptance.** The gate is met on an unloaded host. It is
**not** demonstrated under host oversubscription, and this handoff does not claim
it is. Running a second inference workload on the same host is outside P0's
stated four-camera envelope; if the target deployment must tolerate that, the
plan's escalation path applies — a documented architecture revision, not an
in-place fix. See §13 item 6.

### 10.6 Browser suite

26 Playwright scenarios against real Chromium and a deterministic in-process
server. Uncaught script errors fail every test. They prove: the empty state's
manual-mapping guidance; add/validate/enable/disable/delete with confirmation;
runtime badges tracking worker state; the credential `hunter2` appearing
**nowhere** in the DOM while `***:***@10.0.0.9:554/live` does; a camera name
containing `<img src=x onerror=alert(1)>` rendering as text with no element
created; polling pausing while the tab is hidden and stopping on navigation;
drag-to-draw plus save plus reload round-tripping normalised geometry;
rejection of a too-short line in the browser; the keyboard-only coordinate path;
**the direction arrow being drawn into the B half-plane**, measured from canvas
pixels; snapshot 409 and image 410 handling; deleted-camera history remaining
browsable; load-more never repeating an id across 30 events; all three recording
outcomes including opening the exact URL the VMS returned; a late response
failing to overwrite a newer selection; a network outage showing one banner and
recovering; and no horizontal overflow at 390 px.

### 10.7 Manual acceptance against the running VMS

Executed against the live Component 2/3 stack (`vms`, `vms-mediamtx`) and the
Component 1 simulator, all started independently. Four simulator sources were
registered in the VMS, which reported them `ONLINE` and `RECORDING`.

| # | Scenario | Result |
| --- | --- | --- |
| 3 | `/health` 200 with zero cameras | `{"status":"ok","database":"ok","event_storage":"ok","detector":"ok"}` |
| 4 | Four VMS cameras enabled and `ONLINE` | `cam_2e49db78 cam_6adb97ff cam_953f202d cam_dc039c4c` |
| 5 | Four analytics cameras `STARTING → RUNNING` | all four `RUNNING` |
| 6 | Line geometry and direction round-trip on each camera | exact round-trip for `A_TO_B`, `B_TO_A`, `BOTH` |
| 8a | Name-only patch keeps the session | `ws_e4ac20d5…` → `ws_e4ac20d5…` (unchanged) |
| 8b | FPS patch resets the session, RTSP uninterrupted | new `ws_bc169983…`, state stayed `RUNNING` |
| 8c | RTSP patch stops and rebuilds with a fresh instance | new instance, back to `RUNNING` |
| 9 | One source stopped | victim `RECONNECTING` at attempt 1; peers `['RUNNING','RUNNING','RUNNING']` |
| 9b | Source restored | back to `RUNNING` on new session `ws_57db1eb5…` |
| 10 | Analytics camera disabled | all VMS cameras still `ONLINE` + `RECORDING`; events retained |
| 11 | `docker compose restart analytics` | all five enabled cameras restored to `RUNNING`, **every session id new**, 34 events preserved (was 33) |
| 12 | Deleting an analytics camera | 204; the traffic camera's 45 events unchanged; VMS untouched |
| 13 | **Analytics stopped entirely** | all five VMS cameras still `ONLINE` + `RECORDING`; `/api/recordings` still listed items; WHEP `OPTIONS` still `204` |
| 14 | Event-store verifier | `schema_version=1 events=30 … problems=0 — event store is consistent`, exit 0 |

**10/10 scripted acceptance checks passed.**

#### The full event path with the real model

The simulator's stock sample is an SMPTE colour-bar pattern, in which YOLO
correctly detects nothing, so it cannot demonstrate event creation. A 24-second
640×480 H.264 clip was generated in which a real street scene (bus and
pedestrians) sweeps left to right, published through the simulator, registered
in the VMS, and consumed by analytics from the **real VMS MediaMTX path**
`rtsp://host.docker.internal:8555/vms_cam_640d286b` with a vertical `BOTH` line
at x = 0.5.

The real YOLO11n model produced real events:

```
2026-09-06T22:14:07.112Z  person   person   B_TO_A  conf=0.83 track=86
2026-09-06T22:14:06.177Z  person   person   B_TO_A  conf=0.83 track=87
2026-09-06T22:14:05.377Z  bus      vehicle  B_TO_A  conf=0.91 track=85
2026-09-06T22:13:53.913Z  person   person   B_TO_A  conf=0.83 track=80
…
```

`B_TO_A` is correct: for a line drawn top-to-bottom, a left-to-right sweep is a
movement from the positive half-plane to the negative one.

* **30 events, 30 unique dedupe keys, 0 duplicates** — one event per track per
  direction per session, as specified.
* `frame.jpg` decoded as a 640×480 JPEG (the exact processed frame, no overlay)
  and `crop.jpg` as a 77×200 JPEG of the clipped box, both served with
  `cache-control: private, max-age=31536000, immutable` and `nosniff`.
* `GET /api/events/{id}/recording` returned `AVAILABLE` with the VMS's own
  `playback_url`, and fetching that URL directly returned
  `HTTP/1.1 200 OK … Content-Type: video/mp4 … Server: mediamtx`.

#### Recording-lookup isolation, proven by stopping the VMS

| Step | Observed |
| --- | --- |
| VMS up | `AVAILABLE`, reason `None` |
| `docker stop vms` | `UNAVAILABLE`, reason `vms_unreachable` |
| Worker state during the outage | **unchanged** (identical id/state/session tuple before and after) |
| Analytics `/health` during the outage | `{"status":"ok", … "workers":{"enabled":3,"running":3,"reconnecting":0,"error":0}}` |
| `docker start vms` | `AVAILABLE` again |

A Component 3 outage is request-local: it never touches a worker, a tracker, a
camera row, or service health.

---

## 11. Deviations from the plan, with evidence

Each of these is the smallest change that preserves the plan's frozen invariant
while matching what the runtime actually does. None alters a service boundary,
P0 semantics, the schema, an external contract, or the security posture.

### 11.1 `GstApp` must be imported for `appsink.try_pull_sample`

**Plan:** §15.2 specifies pull polling via `try_pull_sample`.
**Observed:** with only `Gst` and `GstVideo` imported,

```
AttributeError: 'GstApp.AppSink' object has no attribute 'try_pull_sample'
```

PyGObject binds appsink's pull methods onto the instance only once the `GstApp`
typelib is loaded.
**Change:** `gi.require_version("GstApp", "1.0")` plus the import in
`app/analytics/gst_pipeline.py` and `scripts/validate_runtime.py`. The pull-based
design is unchanged, and the typelib ships in the already-pinned
`gir1.2-gst-plugins-base-1.0`.

### 11.2 The decoded frame must be copied, not just made contiguous

**Plan:** §15.4 step 5 — "call `np.ascontiguousarray(...).copy()`".
**Observed:** an early implementation that relied on `ascontiguousarray` alone
produced a read-only array:

```
ValueError: assignment destination is read-only
```

`np.frombuffer` returns a **read-only view of the mapped GStreamer buffer**, and
when the plane stride equals the visible row width the slice is already
contiguous, so `ascontiguousarray` returns that same view. Unmapping the buffer
would then leave a dangling reference.
**Change:** `_copy_rgb` now always copies, with a comment stating exactly why.
This is the plan's requirement; the interim code was the deviation.
**Evidence:** `test_the_latest_frame_is_an_owned_copy` and
`test_h264_over_tcp_produces_owned_rgb_frames` both mutate the returned array.

### 11.3 `frame` is not passed to `ByteTrackTracker.update`

**Plan:** §17.1 step 2 shows `tracker.update(detections, frame=rgb, timestamp=…)`;
§2.4's recorded spike shows `update(detections, timestamp=…)`.
**Observed:** the pinned library accepts `frame` but warns on every call:

```
UserWarning: ByteTrackTracker.update() received a frame argument but does not use it.
```

**Change:** the adapter passes only the parameters the signature actually
consumes. Monotonic `timestamp` forwarding — the behaviourally significant half —
is unchanged and covered by `test_tracker_real.py`.

### 11.4 `torchvision` is arch-conditional on the CPU index

**Observed:** `download.pytorch.org/whl/cpu` publishes
`torchvision-0.22.1-…manylinux_2_28_aarch64.whl` (no local version) but
`torchvision-0.22.1+cpu-…x86_64.whl`. A single pin cannot cover both.
**Change:** `requirements/torch-cpu.txt` pins both with
`platform_machine` markers and a real hash each, installed `--no-deps` before
`runtime.lock`. `torch==2.7.1+cpu` is available for both arches and needs no
marker. Torch's own dependencies are resolved and hashed in `runtime.lock`.

### 11.5 A bounded runtime heartbeat was added

**Plan:** §12 rule 3 — the worker publishes bounded immutable snapshots.
**Observed:** publishing only on state transitions froze the registry's
`last_frame_at` and counters at the values from the last transition, so the
Cameras view's frame age was wrong for a healthy camera.
**Change:** `RUNTIME_HEARTBEAT_SECONDS = 1.0`. Runtime is published on every
transition **and** at most once per second while running. Registry sections still
only swap a reference, so this does not put slow work under a lock.

### 11.6 Ultralytics needs a writable config directory

**Observed:** as the non-root runtime user,

```
ERROR ❌ Error writing to /opt/ultralytics/Ultralytics/persistent_cache.json:
[Errno 13] Permission denied
```

and, with a stale root-owned `/tmp` entry baked into an image layer, a silent
fallback that discarded the hardened settings.
**Change:** `scripts/seed_ultralytics.py` bakes a seed copy with telemetry
`sync` and `hub` **disabled**, the final stage clears `/tmp` after its build-time
validation, and `app/analytics/detector.py` seeds `YOLO_CONFIG_DIR` **at module
import** — Ultralytics resolves that directory once per process, so a lazy seed
inside `load()` is too late for any caller that imported the library first.
No application directory is writable; the service makes no telemetry call.

### 11.7 `app/services/presentation.py` was added

The planned tree has no home for record → response mapping, and putting it in
route modules would violate §9's rule that routes delegate all behaviour. One
module now owns that mapping — and therefore owns the single place a stored
`rtsp_url` becomes a masked one.

### 11.8 An explicitly empty `cursor` is rejected

**Plan:** §19.7 — reject a malformed cursor as `400 invalid_cursor`.
`?cursor=` (present but empty) is a client bug, not "start from the beginning";
only an **absent** cursor means that. Covered by
`test_api_events::test_a_malformed_cursor_is_400`.

### 11.9 The appsink backlog assertion measures the real invariant

**Plan:** §23.3 item 4 — "appsink retains at most one frame under an
intentionally slow consumer".
**Observed:** a naive "drain until `None`" loop never terminates, because a live
25 fps source always has a fresh frame within the 200 ms poll — 201 frames over
7.99 s, i.e. exactly live rate, not a backlog.
**Change:** the test asserts the configured properties (`max-buffers=1`,
`drop=true`, `sync=false`) **and** that after a 2-second stall the frames
available within a 150 ms window are far fewer than the ~50 produced during it,
and that the first frame returned is less than 0.5 s old. That is the invariant
that matters: the consumer is handed *current* frames, never a stall's history.

### 11.10 A test-infrastructure bug found and fixed

A conftest's `pytest_collection_modifyitems` receives the **whole session's**
item list, not only items beneath it. The per-tier conftests were therefore
tagging every test in the repository, which silently turned `-m unit` into "run
everything" — surfacing as `83 failed, 420 passed, 92 errors` when the
integration and browser tiers ran without their fixtures. Each conftest now
marks only items under its own directory, and
`tests/unit/test_marker_selection.py` is a regression test that collects each
tier in a subprocess and asserts it selects nothing outside its directory.

### 11.11 `tests/assets/bus.jpg` is downloaded, not committed

As the plan permits, redistribution terms for a standalone copy could not be
confirmed (the Ultralytics assets project is AGPL-3.0). The `real_model` job
downloads it and verifies SHA-256
`c02019c4979c191eb739ddd944445ef408dad5679acab6fd520ef9d434bfbc63`
(810×1080, 137,419 bytes) before use, caching it in a writable scratch directory
because the working tree is mounted read-only in the test image.

### 11.12 The moving-object content assertion is phase-independent

**Observed:** `test_h264_over_tcp_produces_owned_rgb_frames` asserted that the
first ten decoded frames spanned dark to bright. Against a freshly started
publisher those ten frames (0.67 s at 15 fps) can all precede the moving square
entering view, so the assertion failed with `max=129` on an otherwise healthy
stream.
**Change:** the shape and ownership assertions still use the first ten frames;
the content assertion now samples for up to 12 s until both a dark and a bright
pixel have been decoded, and fails with the extremes it did see. This proves the
decoded content genuinely tracks the source instead of depending on the object's
phase at connect time.

### 11.13 `410 worker_retired` is defined but never reached

`PLAN` §20.1 lists `410 worker_retired` for the snapshot route. In this design it
has no reachable trigger: the latest-frame store is owned by the worker instance
itself, so a retired instance's frames go away with it and cannot be served as
someone else's. A camera with no live worker is `409 snapshot_not_ready`, which
is the honest answer. The error class and its code are kept so the contract is
complete, but no code path raises it and no test asserts it.

### 11.14 An explicit Compose project name

The VMS clone lives in a directory with the same basename, so Compose derived the
same project name and reported the running VMS containers as **orphans of this
project** — one `docker compose down --remove-orphans` from removing them.
`name: genea-analytics` in both Compose files makes the separation explicit. This
is a VMS-isolation fix, not a cosmetic one.

---

## 12. Known limitations

Every one of these is observed behaviour, not a suspicion.

**Throughput and scheduling**

* CPU throughput is host, model, and stream dependent. **5 FPS is a per-camera
  desired sampling rate, not a service-level guarantee.** With four cameras and
  one shared detector, busy-drop counts are routinely comparable to inference
  counts (a mid-run sample: 99 inferences against 161 busy drops on one camera).
  That is the design working — dropping rather than queuing — but it means
  aggregate inference is bounded by one model on one CPU.
* The non-blocking lock is not a fair scheduler. The four-camera gate proves no
  camera is starved beyond 15 s over a 60-second window on an unloaded host; it
  does **not** prove fairness on a slower host, with more cameras, or under
  oversubscription — where it has been observed to fail (§10.5.1). If that gate
  fails, the correct response is an architecture review, not a queue.

**Media**

* Only H.264 over RTSP/TCP. H.265, MJPEG, UDP-only, and non-RTSP sources are
  unsupported; an H.265 source is fatal, by design.
* Nothing transcodes, and no clip is generated; event images are JPEGs of the
  processed frame.

**Analytics semantics**

* One active line per camera; the schema enforces it.
* Centroid-based sampled crossing can miss an object that appears on one side and
  disappears before a confirmed observation on the other.
* Tracker and dedupe continuity end on reconnect, restart, or a semantic
  configuration change, so the same physical object can produce another event in
  a new session. There is no cross-camera re-identification.
* A track produces at most one event per direction per session; intentional
  repeated same-direction laps are suppressed.
* `crossed_at` depends on the analytics host's UTC clock. There is no NTP
  verification and no source-clock correction, so a drifting host clock can
  place an event outside the VMS recording span that really contains it.

**Operations**

* SQLite is a single-host store with one writer process. Two replicas against one
  volume would corrupt the ownership model; there is no leader election.
* **No retention policy.** Nothing deletes an event or an image. When the volume
  fills, the affected worker stops in `ERROR` with `event_persistence_failed`,
  `/health` reports the store unwritable, and no partial event is recorded — but
  freeing space is entirely manual.
* No authentication and no metrics endpoint. Anyone who can reach port 8100 can
  add, edit, and delete cameras and read every event image.
* RTSP credentials are stored unencrypted in SQLite.
* The image is 2.93 GB, dominated by CPU Torch and its dependency closure.
* A worker that hangs inside a native call cannot be preempted; the 10-second
  bounded join is the last-resort containment, and a timeout surfaces as
  `503 worker_stop_timeout` with the desired state already persisted.
* Recording availability depends on the current Component 3 API **and** on the
  playback host being reachable from the operator's browser, which is a different
  network position from this container.

**Testing**

* The four-camera event assertions use a deterministic blob detector over
  genuinely decoded frames, because the synthetic fixture pattern is not a person
  or a vehicle. The real model is exercised separately, both by the `real_model`
  tier and by the end-to-end acceptance in §10.7.
* The browser suite runs Chromium only.
* No load, soak, or chaos testing beyond source loss, VMS outage, restart, and
  the three consecutive four-camera runs.

---

## 13. Runtime validations still open

These are the items from `PLAN` §25.4 that this work did **not** close. Nothing
below is claimed as verified.

1. **amd64.** Everything here was built and run on `linux/arm64`. The locks carry
   hashes for both architectures and `torch-cpu.txt` pins the x86_64 wheels, but
   **no amd64 build or test run was executed.**
2. **Linux `host-gateway`.** Verified on Docker Desktop for macOS only. The
   Compose file declares `extra_hosts: host.docker.internal:host-gateway`, which
   is the documented Linux path, but it was not exercised.
3. **Real IP cameras.** All sources were the Component 1 simulator or the test
   publisher. Camera-specific H.264 profiles, levels, and RTSP dialects are
   unverified.
4. **Credentialed RTSP end to end.** Credential *handling* is verified
   (validation, masking, scrubbing, a hostile-credential integration test), but
   no stream was consumed through a server that actually demanded authentication.
5. **Sustained load.** The longest continuous run was minutes, not hours. Memory
   was sampled, not profiled over a soak; no thermal or throttling behaviour was
   observed.
6. **Target-host capacity, and behaviour under oversubscription.** The
   53–55 ms warm inference and the four-camera progress figures are from this
   development machine with nothing else competing. They are not a capacity
   promise for any other host and must be re-measured there. The four-camera
   gate **failed once** when a second five-camera real-model workload ran on the
   same host (§10.5.1); behaviour under oversubscription is explicitly
   unproven, and the plan's escalation path — an approved architecture revision,
   not an in-place scheduler — applies if the target deployment requires it.
7. **Disk exhaustion on a real volume.** Simulated via injected `ENOSPC` and a
   broken-storage double. A genuinely full volume, a read-only remount, and
   SQLite corruption were not exercised against real hardware.
8. **UTC-midnight recording boundary.** Date derivation across midnight is
   covered by unit tests; it was not exercised against a real Component 3
   recording that spans midnight.
9. **Browsers other than Chromium**, and the browser-network reachability of the
   returned `playback_url` from a machine other than the Docker host.
10. **A stopped-service backup and restore drill.** The procedure is documented
    in `README.md` §11 but was not performed.

---

## 14. Files added

```
.dockerignore  .env.example  .gitignore
Dockerfile                     4 stages: runtime-base, python-deps, final, tests
docker-compose.yml             analytics only; named project genea-analytics
docker-compose.test.yml        private MediaMTX + publisher + test runner
pyproject.toml                 pytest markers: unit integration real_model
                               four_camera e2e
requirements/torch-cpu.txt     CPU Torch/Torchvision, arch-marked, hashed
requirements/runtime.lock      fully resolved, every wheel hashed
requirements/test.lock         pytest, pytest-asyncio, Playwright, hashed
README.md                      rewritten for this component
THIRD_PARTY_NOTICES.md         incl. the Ultralytics AGPL-3.0 obligation
HANDOFF_component_4.md         this document
app/                           28 modules (see §3)
scripts/                       validate_runtime.py verify_event_store.py
                               seed_ultralytics.py
tests/                         unit (16 files), integration (5), e2e (1),
                               fakes (6), fixtures (mediamtx.yml + publisher)
```

`git status --short` shows only these paths plus the pre-existing
`PLAN_component_4.md`; `git diff --check` is clean. No file outside this
repository was modified, and the VMS and simulator stacks were used only through
their public HTTP APIs.

---

---

## 15. Environment state created for acceptance

The acceptance in §10.7 needed real VMS camera paths, so the following were
created **through the public HTTP APIs only** — no file in the simulator or VMS
clone was modified, and nothing was deleted.

**RTSP simulator (Component 1), `http://localhost:8080`**

| id | name | source |
| --- | --- | --- |
| `cam_1ca85469` | `acc-cam-1` | bundled `sample-320x240.mp4` |
| `cam_5d8c9be5` | `acc-cam-2` | bundled `sample-320x240.mp4` |
| `cam_b4e28fe5` | `acc-cam-3` | bundled `sample-320x240.mp4` |
| `cam_346f2be1` | `acc-cam-4` | bundled `sample-320x240.mp4` |
| `cam_b4bc0c9c` | `acc-traffic` | a generated 24 s clip of a real street scene |

**VMS (Component 2/3), `http://localhost:8090`**

`cam_2e49db78`, `cam_6adb97ff`, `cam_953f202d`, `cam_dc039c4c`, `cam_640d286b`,
all pointing at the simulator paths above. Recording was enabled on them to
exercise the Component 3 lookup, then **disabled again** at the end of this work
so they stop consuming disk.

The pre-existing VMS camera `cam_f272c569` (`car_stream`) was not touched.

Remove them with, for each id:

```bash
curl -X DELETE http://localhost:8090/api/cameras/<vms-camera-id>
curl -X DELETE http://localhost:8080/api/cameras/<simulator-camera-id>
```

The analytics-side cameras and their events live in the `analytics-data` volume
and are removed with `docker compose down --volumes` **in this repository only**
(the explicit project name makes that safe for the VMS stack).

---

*The plan this implements is `PLAN_component_4.md`; the user-facing guide is
[README.md](../../services/video-analytics/README.md). The external contracts consumed here are described by
`HANDOFF_component_2.md` and `HANDOFF_component_3.md` in the VMS clone.*
