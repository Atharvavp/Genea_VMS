# Genea Analytics — Standalone Video Analytics & Event Detection (Component 4)

A single-process FastAPI service with a dependency-free dashboard that reads
H.264 RTSP paths already published by the VMS MediaMTX instance, runs CPU-only
YOLO11n detection and ByteTrack tracking per camera, and records an event every
time a confirmed track crosses a configured line in an allowed direction.

It is **completely downstream of the VMS**. It never imports VMS code, never
reads the VMS database or recording filesystem, never calls the MediaMTX Control
API, and never changes a MediaMTX path. Stopping or crashing this service leaves
VMS live view and recording untouched.

```
camera / simulator RTSP
        │
        ▼
VMS-owned MediaMTX path  vms_cam_<id>
        ├── WebRTC / WHEP  ──────────►  VMS browser live view      (Component 2)
        ├── native recording ────────►  VMS storage and playback   (Component 3)
        └── RTSP :8555  ─────────────►  Genea Analytics reader     (Component 4)
```

---

## Contents

1. [Quick start](#1-quick-start)
2. [What it does](#2-what-it-does)
3. [Configuring a camera](#3-configuring-a-camera)
4. [Drawing a line, and what a direction means](#4-drawing-a-line-and-what-a-direction-means)
5. [Events, images, and recording lookup](#5-events-images-and-recording-lookup)
6. [Runtime states](#6-runtime-states)
7. [HTTP API](#7-http-api)
8. [Configuration](#8-configuration)
9. [Validated runtime](#9-validated-runtime)
10. [Tests](#10-tests)
11. [Backup, restore, and disk](#11-backup-restore-and-disk)
12. [Troubleshooting](#12-troubleshooting)
13. [Security posture](#13-security-posture)
14. [Known limitations](#14-known-limitations)

---

## 1. Quick start

The VMS stack (Components 2 and 3) runs independently and does not need to know
this service exists.

```bash
cd services/video-analytics   # every command below runs from this directory
cp .env.example .env          # then change CURSOR_SIGNING_KEY for anything non-local
docker compose build analytics
docker compose up -d analytics
curl --fail --silent --show-error http://localhost:8100/health
open http://localhost:8100
```

Stop it again with `docker compose down`. **Do not add `--volumes`** during
normal teardown: that destroys every recorded event and its images.

This repository runs as its own Compose project (`name: genea-analytics`), so
Compose commands here never touch the VMS containers even though the VMS clone
lives in a directory with the same name.

---

## 2. What it does

For each enabled camera, one non-daemon thread owns everything on that camera's
media path:

```
rtspsrc(TCP) → rtph264depay → h264parse → avdec_h264
             → videoconvert → video/x-raw,format=RGB → appsink(max-buffers=1, drop)
             → cadence gate → detector → ByteTrack → crossing → event writer
```

* **Latest-frame only.** The appsink keeps at most one buffer and drops the
  rest, and the sampler never fires a catch-up burst. Source frames and
  inference work cannot accumulate in an unbounded queue.
* **One shared detector.** YOLO11n is loaded once for the whole process and
  guarded by a *non-queuing* lock: a worker that loses admission drops that
  analytics opportunity and moves on rather than queuing behind another camera.
  This bounds memory at four cameras instead of loading four models.
* **One tracker per session.** `trackers.ByteTrackTracker` is instantiated
  directly, one instance per worker session. `YOLO.track` is deliberately not
  used — it attaches a tracker to shared `model.predictor` state.
* **Events are durable before they exist.** Both JPEGs are written and fsynced
  into a hidden temporary directory, atomically renamed into place, and only
  then is the database row inserted.

### What is an event

All of these must hold. Detection alone is never an event.

1. A **confirmed** ByteTrack track (not a bare detection).
2. Its normalised centroid moved from one **stable** side of the line to the
   other, where "stable" means at least 0.01 normalised units away from the line
   — points inside that deadband neither fire nor move the anchor.
3. The movement segment intersects the **finite** segment A–B, not its infinite
   extension.
4. The actual direction is allowed by the line's policy.
5. The row and both images were persisted successfully.

A track fires at most once per direction per session. A new session — from a
reconnect, a restart, or a semantic configuration change — starts a fresh
deduplication scope.

---

## 3. Configuring a camera

There is no VMS discovery, by design. Copy the camera id from the VMS and give
this service an RTSP URL that is reachable **from the analytics container**:

```bash
curl -sX POST http://localhost:8100/api/cameras \
  -H 'content-type: application/json' \
  -d '{"vms_camera_id":"cam_f272c569",
       "name":"Loading Bay",
       "rtsp_url":"rtsp://host.docker.internal:8555/vms_cam_f272c569"}'
```

| From | Use |
| --- | --- |
| Docker Desktop (macOS, Windows) | `rtsp://host.docker.internal:8555/vms_<camera-id>` |
| Linux Docker | the same host alias, provided by `extra_hosts: host-gateway` |
| Same Docker network as the VMS | the VMS MediaMTX service name and port `8554` |

`localhost` inside the container is the container, not your machine. Creating an
unreachable camera is valid desired state: it is accepted and visibly reconnects.

Per-camera settings are database state, not environment variables:
inference FPS (1–10, default 5), confidence (0.10–0.95, default 0.25), and
object categories (`person`, `vehicle`). Editing FPS, confidence, categories,
the VMS id, or the line resets that camera's tracking session but never
interrupts its RTSP pipeline. Editing the RTSP URL stops the worker and starts a
fresh one. Renaming changes nothing at runtime.

Credentials in an RTSP URL are accepted, stored, and passed to GStreamer. They
are never returned by the API, rendered in the UI, or written to a log; the edit
form leaves the URL field blank and treats it as "keep the stored source".

---

## 4. Drawing a line, and what a direction means

Open **Configure** on a camera. The page shows the latest decoded frame with a
canvas over it. Drag to place **A** then **B**; drag either endpoint to adjust;
or type the four normalised coordinates directly, which is the keyboard-only
path. Coordinates are always normalised `0..1`, so they survive any resize.

Both the browser and the backend use the same screen coordinate system: origin
top-left, x grows right, **y grows down**. For the directed line `A → B`:

```
signed_distance(P) = cross(B - A, P - A) / |B - A|
A_SIDE = negative half-plane      B_SIDE = positive half-plane
```

> **`A_TO_B` means "from the A-side half-plane to the B-side half-plane".**
> It does **not** mean "from endpoint A towards endpoint B."

For a horizontal line drawn A-left to B-right, points visually **above** it are
the A side and points **below** it are the B side, because screen y increases
downward. The green arrow on the overlay is drawn along the same positive normal
the backend uses, so what you see is what is enforced.

`BOTH` accepts either actual direction; an event always stores the concrete
direction it observed, never `BOTH`.

---

## 5. Events, images, and recording lookup

Each event stores denormalised metadata plus `frame.jpg` (the exact processed
frame, no overlays) and `crop.jpg` (the clipped bounding box from that frame):

```
/data/events/<camera_id>/<UTC YYYY-MM-DD>/<event_id>/{frame.jpg,crop.jpg}
```

`crossed_at` is the **UTC wall-clock receipt time of the processed frame that
first proved the crossing**. Source RTP/PTS timestamps have an arbitrary clock
origin and are stored only as opaque diagnostics; they are never used for
ordering, deduplication, or recording lookup.

**Deleting a camera does not delete its history.** The camera and its line are
removed; every event row and image stays, and the Events view keeps showing the
saved camera and line names.

**View recording** calls Component 3 once, read-only:

```http
GET {VMS_API_BASE_URL}/api/recordings?camera_id=<vms id>&date=<UTC YYYY-MM-DD>
```

and matches the half-open span `start <= crossed_at < start + duration`. The
route always returns HTTP 200 when the event exists:

| Status | Meaning |
| --- | --- |
| `AVAILABLE` | a span contains the timestamp; the browser opens the **VMS's own** `playback_url` |
| `NOT_FOUND` | the VMS answered, and nothing recorded covers that moment |
| `UNAVAILABLE` | timeout, unreachable, rejected, malformed, or an unsafe URL |

A lookup failure never changes a worker, a tracker, a camera, or an event.
Component 4 never proxies, rewrites, or stores the playback URL.

---

## 6. Runtime states

Analytics runtime state means only what this service can observe about its own
worker. **VMS camera health remains owned by the VMS.**

| State | Meaning |
| --- | --- |
| `DISABLED` | desired state is off; no worker exists |
| `STARTING` | the thread started; no valid frame yet |
| `RUNNING` | at least one RGB frame has been decoded and admitted |
| `RECONNECTING` | a recoverable source failure; retrying with backoff, forever |
| `ERROR` | a fatal failure; the thread exited until configuration changes |

`RUNNING` is about frames, not events: no line, no detections, a cadence skip,
or a detector-busy drop all leave a live camera `RUNNING`.

Recoverable failures (source error, EOS, stall, refused, unauthenticated) retry
with `1, 2, 4, 8, 16, 30, 30…` seconds, each multiplied by a random `0.8–1.2`.
Fatal failures (unsupported codec, detector unavailable, tracker invariant,
event persistence failure) publish `ERROR` and end that worker only.

---

## 7. HTTP API

Interactive docs at `/docs`. Every JSON error uses one envelope:

```json
{"error": {"code": "camera_not_found", "message": "...",
           "details": {}, "request_id": "req_<32hex>"}}
```

| Method / path | Success | Notable failures |
| --- | --- | --- |
| `GET /health` | 200 / 503 | — |
| `GET /api/cameras` | 200 | — |
| `POST /api/cameras` | 201 + `Location` | 409 duplicate VMS id, 422 |
| `GET /api/cameras/{id}` | 200 | 404 |
| `PATCH /api/cameras/{id}` | 200 | 404, 409, 422, 503 stop timeout |
| `DELETE /api/cameras/{id}` | 204 | 404, 503 stop timeout |
| `GET /api/cameras/{id}/snapshot` | 200 JPEG | 404, 409 not ready |
| `GET /api/cameras/{id}/line` | 200 | 404 camera, 404 `line_not_configured` |
| `PUT /api/cameras/{id}/line` | 200 / 201 | 404, 422 |
| `DELETE /api/cameras/{id}/line` | 204 | 404 camera |
| `GET /api/events` | 200 | 400 cursor/range, 422 |
| `GET /api/events/{id}` | 200 | 404 |
| `GET /api/events/{id}/frame` \| `/crop` | 200 JPEG | 404, 410 artifact gone |
| `GET /api/events/{id}/recording` | 200 | 404 event only |

`GET /api/events` accepts `camera_id`, `object_category`, `object_class`,
`direction`, `from`, `to` (half-open, at most 31 days), `limit` (1–100), and an
HMAC-signed opaque `cursor`. No total count is returned; that would force a full
scan for no benefit.

`/health` is 200 only when the database, the event-store writability probe, and
the detector are all usable. A single reconnecting or failed camera is isolated
and does **not** make the service unhealthy.

---

## 8. Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `ANALYTICS_HTTP_PORT` | `8100` | Compose host side only |
| `ANALYTICS_DATA_DIR` | `/data` | absolute container path |
| `ANALYTICS_DB_PATH` | `/data/analytics.db` | must live beneath the data dir |
| `ANALYTICS_EVENT_DIR` | `/data/events` | must live beneath the data dir |
| `ANALYTICS_MODEL_PATH` | `/opt/models/yolo11n.pt` | baked into the image |
| `ANALYTICS_MODEL_SHA256` | `0ebbc80d…44ee1` | verified before the model loads |
| `ANALYTICS_TORCH_THREADS` | `2` | 1–8; also sets `OMP`/`MKL` threads |
| `ANALYTICS_JPEG_QUALITY` | `90` | 70–95 |
| `ANALYTICS_STALL_SECONDS` | `10` | 5–60; no valid frame for this long ends the attempt |
| `ANALYTICS_STOP_TIMEOUT_SECONDS` | `10` | 2–30; bounded worker join |
| `ANALYTICS_LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR |
| `VMS_API_BASE_URL` | `http://host.docker.internal:8090` | http/https, no credentials, query, or fragment |
| `CURSOR_SIGNING_KEY` | **no production default** | ≥ 32 bytes; signs event page cursors |

`.env.example` ships a well-known development value for `CURSOR_SIGNING_KEY`.
**Replace it before any non-local deployment.**

---

## 9. Validated runtime

Measured on the image this repository builds, on `linux/arm64`:

| Component | Version |
| --- | --- |
| Base image | `ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517` |
| Python | 3.12.3 (Ubuntu system interpreter, `--system-site-packages` venv) |
| GStreamer | core 1.24.2, `gstreamer1.0-libav` 1.24.1, PyGObject 3.48.2 |
| PyTorch | `torch 2.7.1+cpu`, `torchvision 0.22.1`, `torch.cuda.is_available() == False` |
| Ultralytics | 8.4.49, YOLO11n `sha256:0ebbc80d…44ee1` |
| Tracking | `trackers 2.6.0` with `supervision 0.30.2` |
| Web | FastAPI 0.116.1, Uvicorn 0.35.0 (one worker), Pydantic 2.13.5 |

Verify any image yourself:

```bash
docker compose run --rm analytics python scripts/validate_runtime.py --all
```

Every Python dependency is hash-locked (`requirements/*.lock`, installed with
`--require-hashes`), the model is checksum-verified at build and load time, and
the runtime image contains no compiler, no `curl`, and no download path.

---

## 10. Tests

```bash
# unit — no network, no GStreamer, no model
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm --no-deps tests \
  pytest -m unit -q

# real GStreamer, real tracker (starts private MediaMTX + publisher fixtures)
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm tests \
  pytest -m 'integration and not real_model and not four_camera' -q

# real YOLO11n weights on CPU
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm tests \
  pytest -m real_model -q

# four enabled cameras against one detector
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm tests \
  pytest -m four_camera -q

# real Chromium against a deterministic in-process server
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm tests \
  pytest -m e2e -q
```

The test overlay creates its **own** MediaMTX and publisher on a private
network. It never joins, reads, or controls the real VMS stack.

Run the `four_camera` tier on an **otherwise idle host**. It starts four decoders
and asserts that no camera goes 15 seconds without an inference; another
inference workload on the same machine (including a running `analytics` service)
will saturate the CPU and fail that gate for reasons that have nothing to do with
the code under test. `docker compose stop analytics` first.

Read-only store verification, safe to run at any time:

```bash
docker compose exec analytics python scripts/verify_event_store.py
```

---

## 11. Backup, restore, and disk

One named volume, `analytics-data` (physically `genea-analytics-data`), holds
both `/data/analytics.db` and `/data/events`. **They are a single consistency
unit.** Copying the database while the service is running, without SQLite-safe backup semantics, can produce
a database that disagrees with the images beside it.

```bash
docker compose stop analytics
docker run --rm -v genea-analytics-data:/data -v "$PWD":/backup ubuntu:24.04 \
  tar czf /backup/analytics-backup.tgz -C /data .
docker compose start analytics
```

**There is no retention policy.** Nothing deletes an event or an image
automatically; disk capacity is an operator responsibility. When the volume
fills, the affected worker stops in `ERROR` with `event_persistence_failed`,
`/health` reports the store unwritable, and no partial event is ever recorded.

---

## 12. Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Camera stuck `RECONNECTING` | The URL is not reachable *from the container*. Check with `docker compose exec analytics python -c "import socket;socket.create_connection(('host.docker.internal',8555),5)"` |
| `ERROR / unsupported_codec` | The source is not H.264. Only H.264 over RTSP/TCP is supported; nothing transcodes |
| `ERROR / model_unavailable` | The model file is missing or its checksum does not match. `/health` reports the detector unavailable and no worker starts |
| `ERROR / event_persistence_failed` | The event volume is full, read-only, or the database is unusable |
| `503 worker_stop_timeout` | A worker did not stop within the bound. Desired state is already persisted; retry the same operation |
| `RUNNING` but no events | Check that a line exists **and is enabled**, that the direction policy matches the real movement, and that the object's category is enabled |
| Snapshot shows `409` | No frame has been decoded yet; the view retries every second |
| `View recording` says `UNAVAILABLE` | The VMS API is unreachable from the container or returned something unexpected. The event itself is unaffected |
| Events exist but images 410 | The image files were removed underneath the service. The row is deliberately kept; run `verify_event_store.py` |

Useful log events: `worker_started`, `worker_session_started`, `worker_exit`,
`worker_source_failed`, `worker_fatal`, `event_recorded`, `event_persist_retry`,
`event_store_reconciled`, `recording_lookup`, `camera_created`,
`camera_updated`, `camera_deleted`, `line_configured`, `startup_complete`.
Repetitive source failures are rate-limited to once per 30 seconds per
`(camera, error code)` with a suppressed count.

---

## 13. Security posture

P0 has **no authentication** and is intended for a trusted local network only.
Within that:

* the container runs as a non-root user, drops all Linux capabilities, and sets
  `no-new-privileges`;
* no Docker socket, no VMS volume, no MediaMTX Control API port;
* raw RTSP credentials live only in SQLite and the `rtspsrc location` property —
  never in a response, the DOM, a log line, a traceback, or a validation error;
* every stored image path is built from validated ids, resolved beneath the data
  root, and refused if any component is a symlink; no endpoint accepts a path;
* responses carry `X-Content-Type-Options`, `Referrer-Policy`, and a
  restrictive `Content-Security-Policy` with `frame-ancestors 'none'`;
* event page cursors are HMAC-signed and bound to their filters.

RTSP credentials are **not** encrypted at rest in SQLite.

---

## 14. Known limitations

* CPU throughput is host, model, and stream dependent. 5 FPS is a per-camera
  *desired sampling rate*, not a service-level guarantee; a shared detector
  serialises inference by design.
* Only H.264 over RTSP/TCP. H.265, MJPEG, and UDP-only sources are unsupported.
* One active line per camera; the schema enforces it.
* Centroid-based sampled crossing can miss an object that appears on one side
  and disappears before a confirmed observation on the other.
* Tracking and deduplication continuity end on reconnect, restart, or a semantic
  configuration change, so the same physical object can produce another event in
  a new session. There is no cross-camera re-identification.
* A track produces at most one event per direction per session; intentional
  repeated same-direction laps are suppressed.
* `crossed_at` depends on the analytics host's UTC clock; there is no NTP
  verification or source-clock correction.
* Snapshots are JPEGs from the processed frame; there is no clip generation.
* SQLite is a single-host store with one writer process; there is no HA.
* No authentication, no retention policy, no metrics endpoint.
* Recording availability depends on the current Component 3 API and on the
  playback host being reachable from the **browser**, not from this container.

See `HANDOFF_component_4.md` for the verification record, exact measurements,
and the runtime validations that remain open.
