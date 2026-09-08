# RTSP Camera Simulator

Turn ordinary video files into **virtual RTSP cameras**. Upload a clip (or point at a
file you mounted into the container), and the simulator publishes it as a continuously
looping RTSP stream that any player — VLC, FFplay, a VMS, an ONVIF-less test harness —
can pull, exactly like a real IP camera.

Everything runs in Docker. No cameras, no capture hardware, no cloud services.

```
Browser single-page UI
        │  HTTP / REST (multipart upload)
        ▼
FastAPI  ──►  CameraManager  ──►  one FFmpeg publisher process per camera
                    │                        │
              SQLite (/data)                 │  RTSP publish (TCP)
                                             ▼
                                          MediaMTX
                                             │
                                             ▼
                                RTSP clients (VLC, FFplay, VMS)
```

---

## Table of contents

- [Quick start](#quick-start)
- [Using the UI](#using-the-ui)
- [Viewing a stream](#viewing-a-stream)
- [Camera settings](#camera-settings)
- [Camera lifecycle](#camera-lifecycle)
- [Mounted source videos](#mounted-source-videos)
- [Persistence and Docker volumes](#persistence-and-docker-volumes)
- [REST API](#rest-api)
- [Configuration](#configuration)
- [Running the tests](#running-the-tests)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [V1 scope and limitations](#v1-scope-and-limitations)

---

## Quick start

**Prerequisites:** Docker with Compose v2, a browser, and an RTSP player
(VLC, or `ffplay`/`ffprobe` from FFmpeg) to watch the result.

```bash
git clone <this-repo>
cd Genea_VMS/services/rtsp-simulator
docker compose up --build
```

Then:

1. Open **<http://localhost:8080>**
2. Click **Add Camera**, give it a name, and drag a video file onto the drop zone
   (or pick the bundled `sample-320x240.mp4` under *Use a mounted file*).
3. Click **Save camera**, then **Start**.
4. Copy the RTSP URL from the card and open it in VLC or FFplay.

```bash
ffplay -rtsp_transport tcp rtsp://localhost:8554/simulator/parking-entrance
```

Two services come up:

| Service     | Port   | Purpose                                             |
| ----------- | ------ | --------------------------------------------------- |
| `simulator` | `8080` | REST API + single-page UI (FastAPI/Uvicorn, FFmpeg) |
| `mediamtx`  | `8554` | RTSP server that clients connect to                 |

Stop with `Ctrl+C`, or `docker compose down` (cameras survive; see
[Persistence](#persistence-and-docker-volumes)).

---

## Using the UI

The whole app is one page at <http://localhost:8080>.

- **Add Camera** opens the create dialog: name, optional stream path, the source
  video, and the virtual camera's output settings.
- Each camera is a card showing its **status badge**, its **RTSP URL** with a copy
  button, and two clearly separated blocks:
  - **Source video (input file)** — what ffprobe found in the file you supplied:
    codec, resolution, frame rate, duration, bitrate, size, audio.
  - **Virtual camera output (RTSP)** — what the published stream is *encoded to*:
    codec, resolution, frame rate, bitrate, loop and auto-start flags, live PID.

  These two are deliberately independent: a 1080p/30fps source can be published as a
  640×480/10fps H.265 camera.
- **Start / Stop / Restart / Edit / Delete** act on one camera and never affect the
  others. Buttons are disabled while a camera is mid-transition.
- The list polls every 2 seconds (paused while a dialog is open), so status changes —
  including a publisher that dies on its own — appear without a refresh.
- The badge in the header reports simulator health (ffmpeg, ffprobe, database).

Uploads are streamed to disk, validated with `ffprobe`, and only then attached to a
camera. A file that is not decodable video is rejected with a clear message and
nothing is persisted.

---

## Viewing a stream

Every camera is published at:

```
rtsp://localhost:8554/simulator/<stream_path>
```

```bash
# FFplay
ffplay -rtsp_transport tcp rtsp://localhost:8554/simulator/parking-entrance

# ffprobe (prints the stream's real properties)
ffprobe -rtsp_transport tcp -show_streams rtsp://localhost:8554/simulator/parking-entrance

# VLC
vlc rtsp://localhost:8554/simulator/parking-entrance
```

RTSP is served over **TCP only**. UDP RTP does not survive ordinary Docker port
mapping, so both MediaMTX and the FFmpeg publishers are pinned to TCP. Pass
`-rtsp_transport tcp` to FFmpeg-based players; VLC negotiates TCP automatically.

Streams exist only while a camera is running: stopping a camera makes the RTSP path
disappear (clients get `404 Not Found`), which is the V1 way to simulate a camera
going offline.

---

## Camera settings

| Setting          | Values                                | Notes                                                                 |
| ---------------- | ------------------------------------- | --------------------------------------------------------------------- |
| **Name**         | 1–100 characters                       | Free text.                                                            |
| **Stream path**  | `^[a-z0-9][a-z0-9_-]{0,63}$`           | Optional — generated from the name (`Parking Entrance` → `parking-entrance`), de-duplicated with `-2`, `-3`, … Must be unique. |
| **Codec**        | `h264` (default), `h265`               | H.264 via `libx264`, H.265 via `libx265`.                             |
| **Resolution**   | *same as source*, or fixed W×H         | Even numbers, 2–7680. Fixed scales with aspect ratio preserved and pads to the exact size. |
| **Frame rate**   | *same as source*, or fixed FPS         | `0 < fps ≤ 120`.                                                      |
| **Bitrate**      | *auto* (CRF 23), or fixed kbps         | Fixed sets `-b:v/-maxrate/-bufsize`, 64–50000 kbps.                   |
| **Loop**         | on (default) / off                     | On: the file repeats forever. Off: the camera stops when the clip ends. |
| **Auto start**   | off (default) / on                     | On: the camera starts with the simulator, including after a container restart. |

Audio is always dropped — this simulates a video-only IP camera and keeps the
publisher simple.

**H.265 caveat:** H.265/HEVC over RTSP is supported by MediaMTX and FFmpeg-based
players, but some clients (older VLC builds, some browsers and VMS decoders) will not
play it. H.264 is the default and the safe choice for interoperability testing.

---

## Camera lifecycle

```
CREATED ──► STOPPED ◄──────────────► RUNNING ──► ERROR
              ▲   (start / stop / restart)          │
              └──────────── stop ───────────────────┘
```

| Status     | Meaning                                                                    |
| ---------- | -------------------------------------------------------------------------- |
| `CREATED`  | Just created, never started.                                                |
| `STARTING` | FFmpeg spawned, inside the startup grace period.                            |
| `RUNNING`  | The FFmpeg publisher survived startup validation and is publishing.         |
| `STOPPING` | A stop was requested; the process is being terminated.                      |
| `STOPPED`  | No publisher. Also where a non-looping camera lands when its clip ends.     |
| `ERROR`    | FFmpeg failed to start or exited unexpectedly. `last_error` holds the reason. |

Rules worth knowing:

- **Start** is idempotent on a running camera and never spawns a second publisher.
- **Stop** is idempotent on a stopped camera and clears `last_error`.
- **Restart** stops any owned process, then starts a fresh one.
- **Editing a running camera** that changes anything runtime-affecting — source,
  stream path, loop, codec, resolution, frame rate, bitrate — persists the change and
  then **restarts the publisher automatically**. Changing only the name or the
  auto-start flag does not interrupt the stream.
- If a publisher dies on its own, the camera leaves `RUNNING` by itself: a non-looping
  camera whose FFmpeg exited `0` becomes `STOPPED`; anything else becomes `ERROR` with
  the tail of FFmpeg's stderr in `last_error`.
- On shutdown every publisher is terminated gracefully (then killed after a timeout),
  so `docker compose down` leaves no orphaned FFmpeg processes.
- On startup, statuses left over from the previous run (`RUNNING`, `STARTING`,
  `STOPPING`) are reconciled to `STOPPED`, and cameras with auto-start are started
  again — independently, so one failure does not block the others.

`RUNNING` means *the managed FFmpeg publisher started cleanly and is still alive*.
V1 deliberately does not continuously re-probe the RTSP endpoint.

---

## Mounted source videos

Anything you put in [`sample-media/`](sample-media/) is mounted **read-only** into the
container at `/data/local-sources` and can be used as a camera source without
uploading it. In the dialog choose *Use a mounted file*; over the API:

```json
{"source": {"kind": "local", "local_path": "sample-320x240.mp4"}}
```

`local_path` is always interpreted **relative to that mounted directory**, never as a
host path. Absolute paths, `../` traversal and symlinks pointing outside the directory
are rejected. The simulator never deletes files there — deleting a camera only removes
a file the camera itself owns (an upload).

A 10-second test pattern (`sample-320x240.mp4`) ships with the repo so you can try the
simulator without finding a video first.

---

## Persistence and Docker volumes

| Location                       | Contents                                             |
| ------------------------------ | ---------------------------------------------------- |
| `simulator-data` volume → `/data` | SQLite database, uploaded videos, staging directory |
| `./sample-media` → `/data/local-sources` (read-only) | Your own source clips           |

- Camera definitions live in SQLite at `/data/simulator.db`; uploads live in
  `/data/videos/<camera_id>_<uuid>.<ext>` (the client's filename is never used as a
  path). Running processes are **not** persisted — only the configuration is.
- `docker compose down` then `docker compose up` keeps every camera; auto-start
  cameras come back publishing on their own.
- `docker compose down -v` deletes the volume and therefore all cameras and uploads.

---

## REST API

Interactive docs: **<http://localhost:8080/docs>** · OpenAPI: `/openapi.json`

| Method   | Path                             | Purpose                                     |
| -------- | -------------------------------- | ------------------------------------------- |
| `GET`    | `/health`                        | Dependency check (`200` ok / `503` degraded) |
| `GET`    | `/api/cameras`                   | List cameras                                 |
| `POST`   | `/api/cameras`                   | Create a camera (`201`)                      |
| `GET`    | `/api/cameras/{id}`              | Fetch one camera                             |
| `PATCH`  | `/api/cameras/{id}`              | Update (restarts a running camera if needed) |
| `DELETE` | `/api/cameras/{id}`              | Stop, delete, and clean up its upload (`204`)|
| `POST`   | `/api/cameras/{id}/start`        | Start the publisher                          |
| `POST`   | `/api/cameras/{id}/stop`         | Stop the publisher                           |
| `POST`   | `/api/cameras/{id}/restart`      | Stop then start                              |
| `GET`    | `/api/local-sources`             | Files available in the mounted directory     |

Create/update accept **`multipart/form-data`** (a JSON `payload` part plus an optional
`file` part) or plain **`application/json`** when the source is a mounted file.

```bash
# Create from an upload and start it immediately
curl -X POST http://localhost:8080/api/cameras \
  -F 'payload={"name":"Parking Entrance","auto_start":true,"loop":true,
                "source":{"kind":"upload"},
                "video":{"codec":"h264",
                         "resolution":{"mode":"fixed","width":1280,"height":720},
                         "fps":{"mode":"fixed","value":15},
                         "bitrate":{"mode":"fixed","kbps":2500}}}' \
  -F 'file=@/path/to/clip.mp4'

# Create from a mounted file
curl -X POST http://localhost:8080/api/cameras \
  -H 'content-type: application/json' \
  -d '{"name":"Lobby","source":{"kind":"local","local_path":"sample-320x240.mp4"}}'

# Change the output resolution (a running camera restarts automatically)
curl -X PATCH http://localhost:8080/api/cameras/cam_ab12cd34 \
  -H 'content-type: application/json' \
  -d '{"video":{"resolution":{"mode":"fixed","width":640,"height":480}}}'

curl -X POST   http://localhost:8080/api/cameras/cam_ab12cd34/start
curl -X POST   http://localhost:8080/api/cameras/cam_ab12cd34/stop
curl -X DELETE http://localhost:8080/api/cameras/cam_ab12cd34
```

<details>
<summary>Camera response shape</summary>

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
</details>

**Errors** always use the same envelope:

```json
{"error": {"code": "duplicate_stream_path",
           "message": "Stream path 'lobby' is already in use.",
           "details": {"stream_path": "lobby"}}}
```

| Status | When                                                                      |
| ------ | ------------------------------------------------------------------------- |
| `400`  | `bad_request` — malformed multipart/JSON, missing source, or two sources    |
| `404`  | `camera_not_found`                                                         |
| `409`  | `duplicate_stream_path`, `operation_in_progress`                           |
| `422`  | `validation_error` (bad config), `invalid_source` (unreadable/rejected file)|
| `503`  | `/health` only, when FFmpeg or the database is unavailable                 |

A **failed start is not an HTTP error**: `POST /start` returns `200` (and create with
auto-start returns `201`) with `status: "ERROR"` and a populated `last_error`, so the
failure is visible in exactly the same place whether it happened at start time or
later.

---

## Configuration

Compose passes sensible defaults; override them in a `.env` file next to
`docker-compose.yml`. See [`.env.example`](.env.example).

| Variable                       | Default            | Meaning                                        |
| ------------------------------ | ------------------ | ---------------------------------------------- |
| `SIMULATOR_HTTP_PORT`          | `8080`             | Host port for the UI/API                       |
| `PUBLIC_RTSP_HOST` / `PUBLIC_RTSP_PORT` | `localhost` / `8554` | What RTSP URLs advertise to clients   |
| `MEDIAMTX_HOST` / `MEDIAMTX_RTSP_PORT` | `mediamtx` / `8554`  | Where FFmpeg publishes                |
| `RTSP_PATH_PREFIX`             | `simulator`        | Path prefix in the RTSP URL                    |
| `MAX_UPLOAD_BYTES`             | `2147483648` (2 GiB) | Upload size limit                            |
| `FFMPEG_STARTUP_GRACE_SECONDS` | `2.0`              | How long a publisher must survive to be `RUNNING` |
| `FFMPEG_STOP_TIMEOUT_SECONDS`  | `5.0`              | Grace before a publisher is killed             |
| `FFMPEG_STDERR_TAIL_LINES`     | `50`               | stderr lines kept for `last_error`             |
| `LOG_LEVEL`                    | `INFO`             | Simulator log level                            |

If port 8080 or 8554 is already taken:

```bash
SIMULATOR_HTTP_PORT=9080 PUBLIC_RTSP_PORT=9554 docker compose up --build
```

---

## Running the tests

Unit, API and static-UI tests (no MediaMTX needed) — they use stub FFmpeg binaries, so
they are fast and deterministic:

```bash
docker compose run --rm --no-deps simulator pytest -q
```

End-to-end tests that publish real video through MediaMTX and read it back with
`ffprobe` (require the stack to be up):

```bash
docker compose up -d
docker compose exec simulator pytest -m integration -v
```

Without Docker, from `simulator/` with Python 3.12+ and FFmpeg on `PATH`:

```bash
pip install -e ".[dev]"
pytest -q
```

What is covered: stream-path slugging and validation, video-config validation, source
path containment (traversal, symlink escape, extension checks), ffprobe JSON parsing
(plus real probes of an FFmpeg-generated clip), FFmpeg argv construction for every
codec/resolution/FPS/bitrate/loop combination, process supervision (startup grace,
early exit, requested stop, force kill, unexpected exit), the full camera lifecycle
including automatic restart on edit and auto-start reconciliation after a restart, and
the HTTP API including every error path, and the static-UI contract (dialog starts
closed, conditional fields stay hidden, UI assets are served uncached).

Manual smoke test:

```bash
docker compose up --build
curl http://localhost:8080/health
# create a camera in the UI, then:
ffplay -rtsp_transport tcp rtsp://localhost:8554/simulator/<stream_path>
```

---

## Troubleshooting

**The camera is `ERROR` right after Start.**
Read `last_error` on the card — it is the tail of FFmpeg's stderr. The usual causes
are MediaMTX not running (`Connection refused`) and a source file that disappeared.
`docker compose logs simulator` shows the full history.

**"Not a readable video file" when uploading.**
`ffprobe` could not decode the file, or it contains no video stream (an audio-only
file, or an image with cover art). Re-encode it:
`ffmpeg -i input.ext -c:v libx264 -pix_fmt yuv420p output.mp4`.

**"Unsupported file type".**
Allowed extensions are `.mp4 .mkv .mov .avi .m4v .webm`. The extension is only a first
gate; the file still has to survive `ffprobe`.

**All cameras suddenly went `ERROR`.**
MediaMTX probably restarted or stopped. The publishers lose their connection and exit;
the simulator itself stays up. Start MediaMTX (`docker compose up -d mediamtx`) and hit
Restart on the cameras.

**`Server returned 404 Not Found` from the player.**
The camera is not running (the path only exists while a publisher is connected), or the
stream path is spelled differently. Check the card's status and copy the URL from it.

**The player connects but shows nothing / cannot decode.**
Likely H.265 in a client that cannot decode it. Edit the camera to H.264 — a running
camera restarts automatically.

**`Bind for 0.0.0.0:8080 failed: port is already allocated`.**
Something else uses 8080 or 8554; override the ports (see
[Configuration](#configuration)).

**Cameras disappeared after a restart.**
Only `docker compose down -v` deletes them — that removes the `simulator-data` volume.
A plain `down`/`up` preserves everything.

**The UI looks wrong or stale after an upgrade.**
`index.html`, `app.js` and `styles.css` are served with `Cache-Control: no-store`, and
their URLs carry a version query string, so a reload picks up the current build. If a
tab was open across the upgrade, reload it once. To confirm what the server is sending:
`curl -I http://localhost:8080/static/styles.css`.

**Permission errors writing to `/data`.**
The named volume is managed by Docker and normally just works. If you replaced it with
a bind mount, make sure the host directory is writable by the container user.

**Upload rejected as too large.**
Raise `MAX_UPLOAD_BYTES` (bytes) in `.env` and restart the stack.

---

## How it works

```
simulator/app
├── main.py                     FastAPI app, lifespan (startup reconcile / shutdown stop-all)
├── config.py                   Settings from the environment; RTSP URL construction
├── api/
│   ├── cameras.py              Routes; multipart + JSON payload parsing
│   └── errors.py               ApiError types and the error envelope
├── domain/models.py            Pydantic models, validation, lifecycle enum, slugging
├── persistence/
│   ├── database.py             SQLite connections (WAL, busy timeout) + schema
│   └── camera_repository.py    Camera CRUD
└── services/
    ├── camera_manager.py       Orchestration: CRUD, lifecycle, per-camera locking
    ├── source_storage.py       Upload staging/promotion, mounted-path containment
    ├── probe.py                ffprobe wrapper and metadata parsing
    ├── ffmpeg_command.py       Pure argv builder for the publish command
    └── process_manager.py      Spawn, startup grace, stderr tail, stop/kill, supervision
```

Design decisions that matter:

- **One FFmpeg process per camera**, spawned with `asyncio.create_subprocess_exec` and
  an argv list. No shell, ever — user input (names, paths, sizes) can never be
  interpreted as a command.
- **A publisher is confirmed, not assumed.** After spawning, the manager watches the
  process for a grace period; a publisher that dies inside it becomes `ERROR` with
  FFmpeg's own stderr instead of a silent "running" camera.
- **Per-camera `asyncio.Lock`.** Every lifecycle or update operation for a camera runs
  under its own lock, so two requests can never spawn two publishers for the same
  camera, while operations on *different* cameras remain fully parallel. The process
  registry has its own lock and refuses a duplicate registration as a second guard.
- **The process table is memory-only.** SQLite stores configuration and last-known
  status; PIDs are never trusted across a restart. Startup reconciles stale statuses
  and re-launches auto-start cameras.
- **SQLite is used safely from async code.** Short-lived connections in WAL mode with a
  busy timeout, every call dispatched to a worker thread so the event loop never blocks
  and a connection is never shared between threads.
- **Uploads are never trusted.** Streamed to a staging file under a size cap, validated
  with `ffprobe`, then moved under a generated name; the client's filename is only ever
  used for display. Mounted paths are resolved and must remain inside the mounted
  directory. Staged files are removed on every failure path.
- **Dynamic MediaMTX paths** (`all_others:` with `source: publisher`) mean adding a
  camera needs no MediaMTX reconfiguration.

---

## V1 scope and limitations

In scope and working: creating cameras from uploaded or mounted videos, configurable
H.264/H.265 output (resolution, frame rate, bitrate), looping and non-looping playback,
start/stop/restart/edit/delete, automatic restart on runtime-affecting edits,
auto-start, persistence across restarts, multiple independent cameras, and a
single-page UI.

Deliberately **not** in V1:

- No VMS, recording, playback UI, preview or WebRTC — this is a source of RTSP streams.
- No authentication on the UI/API or on RTSP. Run it on a trusted network.
- No ONVIF, no camera discovery, no PTZ.
- No audio: publishers strip it.
- No advanced outage simulation (packet loss, jitter, latency injection, credential
  failures). Stopping a camera is the V1 way to take one offline; richer failure
  injection is the natural next step.
- No horizontal scaling or clustering: publishers are children of one container, so
  camera capacity is bounded by that container's CPU. Encoding many high-resolution
  cameras at once is CPU-bound.
- Uploaded files belong to exactly one camera and are not shared between cameras.
- `RUNNING` reflects a healthy publisher process, not a continuously verified RTSP
  endpoint.
