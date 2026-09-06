# Genea VMS — Live View (Component 2)

Register generic RTSP cameras, have MediaMTX ingest them, and watch them in a
browser over WebRTC.

```text
Any RTSP camera ──RTSP──▶ VMS MediaMTX ──WebRTC/WHEP──▶ Browser
                              ▲
                              │ Control API (create/remove paths)
                         VMS backend ──▶ SQLite (desired state)
```

No media passes through the VMS process. It stores what you asked for, tells
MediaMTX to pull it, and reports what MediaMTX says. There is no FFmpeg or
GStreamer anywhere in the live path, and no transcoding.

## Quick start

```bash
cp .env.example .env          # optional; the defaults suit Docker Desktop
docker compose up --build     # UI at http://localhost:8090, API docs at /docs
```

Add a camera in the UI, or:

```bash
curl -X POST http://localhost:8090/api/cameras \
  -H 'content-type: application/json' \
  -d '{"name":"Parking Entrance",
       "rtsp_url":"rtsp://10.0.0.9:554/Streaming/Channels/101",
       "recording_enabled":true}'

# What has it recorded today?
curl "http://localhost:8090/api/recordings?camera_id=cam_ab12cd34&date=$(date -u +%F)"
```

`recording_enabled` defaults to `false`: registering a camera never starts
writing to disk unless you ask it to. Turn it on later from the tile's
**Recording** button, and browse what it captured from **Recordings**.

A syntactically valid URL is accepted even if the camera is unreachable; it
then shows as `OFFLINE` and goes `ONLINE` by itself when the source appears.

Stop with `docker compose down` (registrations *and recordings* persist in named
volumes) or `docker compose down -v` (wipes both).

## Ports

| Service | Purpose | Host port |
| --- | --- | ---: |
| `vms` | UI + REST API | `8090` |
| `vms-mediamtx` | RTSP | `8555` |
| `vms-mediamtx` | WebRTC HTTP / WHEP signalling | `8889` |
| `vms-mediamtx` | WebRTC ICE (UDP) | `8189/udp` |
| `vms-mediamtx` | Recorded playback (`/list`, `/get`) | `9996` |
| `vms-mediamtx` | Control API | **not published** — compose network only |

`8555` avoids the RTSP Camera Simulator's `8554`, so both stacks run side by
side. The ICE UDP port is published on the same number it listens on, because
an ICE candidate advertises the port it was gathered from.

## Architecture

### Desired state vs. runtime state

| | Where it lives | Who owns it |
| --- | --- | --- |
| Name, source URL, enabled, recording preference | SQLite (`/data/vms.db`) | the operator |
| MediaMTX paths and their recording config | MediaMTX, at runtime | derived from SQLite |
| Camera health | this process's memory | derived from MediaMTX |
| Recording state | this process's memory | derived from MediaMTX |
| Which recordings exist | the files, via MediaMTX's playback server | MediaMTX |
| Player state | the browser | that browser alone |

SQLite is the source of truth; MediaMTX is treated as a cache of it and rebuilt
whenever the two disagree. **Health is not persisted**: a stored `ONLINE` is
stale the moment the process stops and would have to be distrusted at startup
anyway, so it is recomputed from MediaMTX on every poll. `cameras` therefore
has no `health_state`/`last_error` columns.

**There is no recordings table either.** MediaMTX's playback server already
answers "which recordings exist for this camera on this day?", and a SQLite copy
would be a second source of truth that could disagree with the files on disk.
The only recording column is `recording_enabled` — the preference, not the
state.

### One MediaMTX path per enabled camera

Camera `cam_ab12cd34` owns the path `vms_cam_ab12cd34`. The name is derived
from the id, so it never changes — editing a camera's URL does not invalidate
its WebRTC URL, and the player only has to reconnect. Users cannot choose
paths; the `vms_` prefix is what tells reconciliation "this one is mine".

Each path is configured as:

```json
{"source": "<the RTSP URL>", "sourceOnDemand": false, "rtspTransport": "tcp",
 "record": <enabled && recording_enabled>,
 "recordPath": "/recordings/%path/%Y-%m-%d/%H-%M-%S-%f",
 "recordFormat": "fmp4", "recordPartDuration": "1s", "recordMaxPartSize": "50M",
 "recordSegmentDuration": "5m", "recordDeleteAfter": "24h"}
```

- `sourceOnDemand: false` (pull always) so health answers "can the VMS obtain
  this source?" independently of whether anyone is watching. MediaMTX retries an
  offline source on its own and recovers when it returns.
- `rtspTransport: tcp` because UDP RTP does not survive Docker port mapping.
- `record` is `enabled && recording_enabled` — recording only runs on a camera
  the VMS is ingesting, and only when it was asked for.
- **The whole recording block is sent every time, including when `record` is
  false.** This is not cosmetic: a payload without those keys resets `recordPath`
  to MediaMTX's default, and the playback server then finds nothing under the
  configured root — so a partial update would make a camera's existing history
  vanish from the UI while the files sat untouched on disk. Verified against
  1.20.1, and locked down by both a unit and an integration test.

### When reconciliation runs

The 2-second poll is a **read**, not a reconcile. A full reconcile runs only:

| Trigger | Why |
| --- | --- |
| Startup | SQLite and MediaMTX may have diverged while the process was down |
| A camera mutation | Targeted: only the camera that changed is applied |
| Drift found by the poll | An enabled camera's path is missing, or a `vms_` path has no enabled owner |
| MediaMTX recovery | It may have restarted empty |

Drift detection is free: the same `/v3/paths/list` response that produces
health also shows which paths exist, so a MediaMTX restart is noticed without a
second API call. Drift-triggered reconciles are rate-limited
(`RECONCILE_MIN_INTERVAL_SECONDS`, default 10 s) so an unfixable camera cannot
turn the poll into a reconcile loop; a recovery reconcile ignores that limit.

### MediaMTX contract (verified against 1.20.1)

| Need | Endpoint | Note |
| --- | --- | --- |
| Create **or** update a path | `POST /v3/config/paths/replace/{name}` | Upserts, so no separate `add` call is needed and no server restart happens |
| Remove a path | `DELETE /v3/config/paths/delete/{name}` | `404` when already gone |
| Which paths exist | `GET /v3/config/paths/list` | **Echoes each source URL including its password** — only names are read from it |
| Health of every path | `GET /v3/paths/list` | `available` is the signal |
| Version | `GET /v3/info` | |

Two things that do **not** work in 1.20.1 and shaped the design:

- `GET /v3/paths/static-sources/get/{name}` does not exist, so there is no
  per-source `lastError` to show. Health is `available`, plus a generic message.
- `online` stays `true` for a configured-but-unreachable static source, so it
  is unusable as a health signal. `available` is the honest one.

`mediamtx/mediamtx.yml` also widens `authInternalUsers`: MediaMTX's default
grants the `api` action to `127.0.0.1` only, which would lock out the VMS
container. That is safe **only** because 9997 is not published. If you ever
publish it, add real credentials first.

## Recording

Recording is per camera and off by default: registering a camera never starts
writing to disk. The preference is independent of `enabled`:

```text
enabled=true,  recording_enabled=false  ->  live only
enabled=true,  recording_enabled=true   ->  live + continuous recording
enabled=false                           ->  no ingest, no recording
```

A disabled camera **keeps** its recording preference, so enabling it again
resumes recording with no operator retoggle.

### Storage layout

MediaMTX writes the files; the VMS never reads them and the VMS container does
not even mount the volume:

```text
/recordings/vms_cam_ab12cd34/2026-09-05/23-17-38-002549.mp4
            └─ camera's path ┘└─ UTC day ┘└─ start time ─┘
```

`RECORDING_SEGMENT_DURATION` (default `5m`) is a **minimum**, not a guarantee:
real boundaries follow keyframes and stream conditions.

### Retention

`recordDeleteAfter`, MediaMTX's own age-based deletion, derived from
`RECORDING_RETENTION_HOURS` (default 24). The VMS runs no cleanup job and never
deletes a file — there is no code path in this repo that unlinks media.

### Historical playback

The recordings dialog asks the VMS which recordings exist, and the VMS asks
MediaMTX's playback server:

```text
Browser ──GET /api/recordings?camera_id=&date=──▶ VMS ──/list?path=&start=&end=──▶ MediaMTX
Browser ◀──────────────── playback_url ─────────────┘
Browser ──────────────GET /get?...&format=mp4──────────────────────────────────▶ MediaMTX
```

The client sends a **camera id**, never a path and never a filename. The VMS
resolves it to the path that camera owns and builds the playback URL from its own
settings, so no request can be pointed at another camera's recordings or at the
host filesystem.

Two details verified against 1.20.1 that shape what you see:

- **`/list` returns timespans, not files.** MediaMTX merges consecutive segments
  into one entry, so a row in the list is everything recorded between two
  interruptions of the source — not one five-minute file.
- **`format=mp4` is deliberate.** It puts the `moov` box at the front of the
  response, so a native `<video>` element can start playing and seek within what
  it has buffered. (MediaMTX sends `Accept-Ranges: none`, so there is no
  server-side byte-range seeking — see Known limitations.)

### Status model

Four states, deliberately never conflated:

**Camera health** — "can the VMS obtain this RTSP source?", from MediaMTX:

| State | Means |
| --- | --- |
| `ONLINE` | MediaMTX has the stream (`available: true`) |
| `OFFLINE` | The path is configured but MediaMTX cannot read the source |
| `UNKNOWN` | MediaMTX is unreachable, the path is not configured yet, or the camera is disabled |

**Player state** — this browser's WebRTC session for one tile: `CONNECTING`,
`LIVE`, `RECONNECTING`, `ERROR`. It never reaches the server.

**Recording state** — is this camera being written to disk?

| State | Means |
| --- | --- |
| `DISABLED` | Not requested, or the camera is disabled |
| `WAITING` | Requested, but MediaMTX has no source to write yet |
| `RECORDING` | Requested, configured, and MediaMTX has the source |
| `ERROR` | The recording configuration could not be applied |

**Historical playback state** — could the recordings dialog list past
recordings? `LOADING`, `AVAILABLE`, `EMPTY`, `UNAVAILABLE`. It belongs to the
dialog, not to the camera.

The last two are the pair most worth keeping apart: **the playback server being
unreachable does not mean the camera stopped recording.** A `503` from
`/api/recordings` leaves the camera reading `RECORDING`, and the tile badge never
turns into `REC ERROR` because history could not be listed. There is a test for
this at every layer, including in the browser.

`RECORDING` is **inferred**. MediaMTX 1.20.1 exposes no recorder-writer health,
so it means "recording is configured and MediaMTX has the source", not "bytes
are provably reaching the disk right now". Log scraping was rejected as a
substitute, the same way it was for per-source health in Component 2.

A tile shows all three server-side badges, so "the camera is down", "my
connection is down" and "it is not being recorded" are distinguishable at a
glance.

### WebRTC playback

The browser talks to MediaMTX's own WHEP endpoint using the reader MediaMTX
ships, vendored at `app/static/vendor/mediamtx-reader.js` (see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)). No custom signalling is
invented. Each tile owns one isolated reader, so one camera failing, being
edited or being deleted cannot disturb the others. The reader retries by
itself, which is why a lost source maps to `RECONNECTING` rather than a
teardown — a source coming back never requires recreating the camera.

The focused view opens its own player and pauses that camera's tile player, so
one camera is never pulled twice; every other tile keeps running.

## API

`GET /docs` for the interactive schema. All camera routes are under `/api`.

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/health` | `200`, or `503` when MediaMTX is unreachable or the DB fails |
| `GET` | `/api/cameras` | list, in creation order |
| `POST` | `/api/cameras` | `201` |
| `GET` | `/api/cameras/{id}` | one camera |
| `PATCH` | `/api/cameras/{id}` | partial update |
| `DELETE` | `/api/cameras/{id}` | `204` |
| `POST` | `/api/cameras/{id}/enable` | `200` |
| `POST` | `/api/cameras/{id}/disable` | `200` |
| `GET` | `/api/cameras/{id}/status` | health only |
| `GET` | `/api/recordings?camera_id=&date=` | finalised recordings for one UTC day |

A camera response:

```json
{
  "id": "cam_ab12cd34",
  "name": "Parking Entrance",
  "rtsp_url_display": "rtsp://admin:***@10.0.0.9:554/Streaming/Channels/101",
  "has_credentials": true,
  "enabled": true,
  "recording_enabled": true,
  "mediamtx_path": "vms_cam_ab12cd34",
  "webrtc_url": "http://localhost:8889/vms_cam_ab12cd34/whep",
  "health": {
    "state": "ONLINE", "last_error": null,
    "checked_at": "2026-09-06T00:00:00.000Z", "mediamtx_available": true
  },
  "recording": {"state": "RECORDING", "last_error": null},
  "created_at": "2026-09-06T00:00:00.000Z",
  "updated_at": "2026-09-06T00:00:00.000Z"
}
```

A recordings response:

```json
{
  "camera_id": "cam_ab12cd34",
  "camera_name": "Parking Entrance",
  "date": "2026-09-05",
  "items": [
    {
      "id": "rec_a7c5733ac220bb20",
      "start_time": "2026-09-05T23:17:38.002549Z",
      "end_time": "2026-09-05T23:17:45.069Z",
      "duration_seconds": 7.066655555,
      "playback_url": "http://localhost:9996/get?path=vms_cam_ab12cd34&start=...&format=mp4",
      "source": "mediamtx"
    }
  ]
}
```

`id` is a hash of the camera id, start and duration — an opaque key for the
frontend to render a list with. It encodes no filesystem path, so it cannot be
turned into one. There is no `GET`/`DELETE /api/recordings/{id}`: nothing in the
UI needs to address a single recording, and manual deletion was left out rather
than approximating "delete a timespan" over MediaMTX's per-segment API.

There is deliberately **no field carrying the real URL**. `PATCH` therefore
treats an omitted `rtsp_url` as "keep the stored source" — which is what the
edit form does for a camera whose URL has credentials. A blank string is
rejected rather than guessed at.

There is no playback/session endpoint: `webrtc_url` points straight at
MediaMTX's WHEP endpoint, which is all the browser needs.

### Errors

```json
{"error": {"code": "validation_error", "message": "...", "details": {"fields": []}}}
```

| Code | HTTP | Cause |
| --- | --- | --- |
| `validation_error` | 422 | bad payload, or a `date` that is not `YYYY-MM-DD` |
| `camera_not_found` | 404 | unknown id |
| `disabled_camera_history_unavailable` | 409 | history asked for on a disabled camera |
| `recording_unavailable` | 503 | the playback server could not answer |
| `internal_error` | 500 | unhandled |

`recording_unavailable` is a **historical-playback** failure. It does not mean
recording stopped, and it never changes a camera's recording state. An enabled
camera with nothing recorded that day is a `200` with an empty `items` list, not
an error — including a camera whose recording was only just switched on, which
MediaMTX reports as a missing directory rather than an empty one.

Two failures that deliberately are **not** request errors:

- **MediaMTX unavailable.** A registration is desired state; an outage does not
  reject it. The change is stored, the camera reports `UNKNOWN`, and
  reconciliation applies it when MediaMTX returns. The outage shows up in
  `/health` (503), not as a 503 on the write.
- **A source MediaMTX refuses.** The camera is kept and reported `OFFLINE`,
  which is the PRD's semantics for a source the VMS cannot obtain.

## Configuration

Every setting has a Docker Desktop default; `.env` overrides them.

| Variable | Default | Purpose |
| --- | --- | --- |
| `VMS_HTTP_PORT` | `8090` | host port for the UI/API |
| `VMS_RTSP_PORT` | `8555` | host port for MediaMTX RTSP |
| `VMS_WEBRTC_HTTP_PORT` | `8889` | WHEP signalling |
| `VMS_WEBRTC_ICE_UDP_PORT` | `8189` | ICE, published on the port it listens on |
| `VMS_PLAYBACK_HTTP_PORT` | `9996` | recorded playback, fetched by the browser |
| `PUBLIC_WEBRTC_HOST` | `localhost` | what the browser is told to connect to for live |
| `PUBLIC_PLAYBACK_HOST` | follows `PUBLIC_WEBRTC_HOST` | …and for recordings |
| `MEDIAMTX_API_URL` | `http://vms-mediamtx:9997` | Control API, compose network only |
| `MEDIAMTX_PLAYBACK_URL` | `http://vms-mediamtx:9996` | playback server, compose network only |
| `RECORDING_STORAGE_PATH` | `/recordings` | recording root **inside the MediaMTX container** |
| `RECORDING_STORAGE_HOST_PATH` | *(empty)* | host bind mount; empty means the `vms-recordings` volume |
| `RECORDING_SEGMENT_DURATION` | `5m` | minimum segment length (`ms`/`s`/`m`/`h`) |
| `RECORDING_RETENTION_HOURS` | `24` | age after which MediaMTX deletes a recording |
| `DATABASE_PATH` | `/data/vms.db` | inside the `vms-data` volume |
| `CAMERA_HEALTH_POLL_SECONDS` | `2.0` | health refresh interval |
| `LOG_LEVEL` | `INFO` | |

**Serving other machines:** set `PUBLIC_WEBRTC_HOST` to the host's address.
It reaches the `webrtc_url` the API hands out, MediaMTX's
`webrtcAdditionalHosts`, and (unless you override `PUBLIC_PLAYBACK_HOST`) the
playback URLs too, so live and recorded video follow the same address. Leaving it
at `localhost` means only the Docker host's own browser can play video.

**Short segments for a demo.** The 5-minute default means the first recording
takes five minutes to appear. For a demo or an E2E run, start the stack with
something like `RECORDING_SEGMENT_DURATION=5s`. `RECORDING_RETENTION_HOURS`
accepts fractions, so `0.01` (36 s) makes retention observable in a minute.

**Recording storage.** By default recordings live in the `vms-recordings` named
volume, which survives `docker compose down` and is wiped by `down -v`. Set
`RECORDING_STORAGE_HOST_PATH=./recordings` to bind a host directory and inspect
the files directly. Only the MediaMTX container mounts it; the VMS container
deliberately does not.

## Security

This is a lab tool with **no authentication** — anyone who can reach port 8090
can add, edit and delete cameras. Run it on a trusted network.

Within that, RTSP credentials are handled deliberately:

| Risk | Mitigation |
| --- | --- |
| Credentials in an API response | The raw URL is never serialised. `rtsp_url_display` masks the password as `***`; there is no endpoint that returns the real value |
| Credentials in the UI | The edit form leaves a credentialed URL blank and keeps the stored source when the field is empty |
| Credentials in logs | Every log line that mentions a URL passes through `sanitize_rtsp_url`; request bodies are never logged |
| Credentials echoed back by MediaMTX | `GET /v3/config/paths/*` returns the source URL in clear. The client extracts nothing but path names, and any MediaMTX error text is scrubbed of the URL and the password before it goes anywhere |
| Log/response injection via a URL | Control characters and whitespace are rejected at validation; anything rendered is control-character stripped |
| SQL injection | Parameterised statements only |
| Unmanaged MediaMTX paths | `paths: {}` with no `all_others`: nothing can publish to the VMS MediaMTX, and only VMS-created paths exist |
| Unauthenticated Control API | Port 9997 is not published; only the compose network reaches it |
| A client reaching another camera's recordings | The API takes a camera id, never a path or filename. The VMS resolves it to the path that camera owns and builds the playback URL itself |
| Path traversal / arbitrary file access | No API accepts a filesystem path, none returns one, and no code in this repo opens, serves or deletes a media file. `date` must match `YYYY-MM-DD` exactly |
| Credentials leaking through recording errors | Playback and recording-config errors go through the same `sanitize_text`, which now also masks the password of any credentialed URL embedded in free text, even when the caller does not know which URL is involved |

**Credentials are stored in plain SQLite.** That is a deliberate V1 limit for a
trusted local/lab deployment; encryption or a secret store belongs to a later
security component.

**The playback port is unauthenticated too.** Anyone who can reach `9996` and
knows a path name can fetch that camera's recordings, exactly as anyone who can
reach `8889` can watch it live. Same trusted-network posture, now covering
recorded video as well as live.

## Running with the RTSP Camera Simulator

The simulator (Component 1) lives in its own clone and is used here purely as
an external RTSP source — the same way a real camera would be. **No VMS code
imports it, calls its API, reads its database or assumes its path layout.**

```bash
# simulator clone
docker compose up -d --build                     # http://localhost:8080
# create a camera in its UI, then check the stream exists
ffprobe -rtsp_transport tcp rtsp://localhost:8554/simulator/parking-entrance

# this clone
docker compose up -d --build                     # http://localhost:8090
```

Register `rtsp://host.docker.internal:8554/simulator/parking-entrance` in the
VMS. `host.docker.internal` is how the VMS container reaches the simulator's
port on the host; from a real camera you would use its own address.

## Tests

```bash
docker compose run --rm --no-deps vms pytest -q          # unit / API / static UI
.venv/bin/python -m pytest -q                            # same, from the host
```

Integration and browser tests are opt-in:

```bash
# Real MediaMTX 1.20.1 in Docker, started from the config this repo ships.
.venv/bin/python -m pytest -m integration

# Full chain in Chromium. Both stacks must be up (see above).
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -m e2e
```

| Suite | Covers |
| --- | --- |
| `test_rtsp_url_security` | scheme/host/port validation, masking, control-character rejection, error scrubbing |
| `test_validation` | request models, settings-derived URLs, id/timestamp shape |
| `test_repository` | schema (and the absence of health columns), WAL, CRUD, insertion order |
| `test_mediamtx_client` | the exact 1.20.1 wire format, pagination, error mapping, secret handling |
| `test_camera_manager` | mutations, targeted vs. full reconciliation, drift and recovery, rate limiting, health mapping, failure isolation |
| `test_api_cameras` | every route, the error envelope, "no response ever carries the password" |
| `test_static_ui` | dialogs ship closed, assets versioned, player lifecycle rules, health/player/recording separation, playback URLs never built in the frontend |
| `test_api_recordings` | listing, empty days, unknown/disabled cameras, bad dates, playback outages, and that none of it changes recording state |
| `test_integration_mediamtx` | the shipped config boots; dynamic path CRUD; a live FFmpeg stream flipping `available`; the Control API really does echo passwords; **recording, listing, MP4 playback, native retention, the partial-replace regression, disabled-path history, source loss/return, four concurrent recordings** |
| `test_e2e_live_view` | video file → simulator → RTSP → VMS MediaMTX → WebRTC → Chromium with real pixels, **and → recording → playback server → `<video controls>`** |

### What the recording tests prove against the real server

Against `bluenviron/mediamtx:1.20.1`, not documentation:

- a full recording config is accepted on a dynamic path, and comparison survives
  MediaMTX normalising `5m` to `5m0s` and `24h` to `1d`;
- a real H.264 stream produces files at `<root>/<path>/<date>/<time>.mp4`,
  `/list` reports them as timespans, and `/get?format=mp4` returns a `video/mp4`
  body with `moov` before `mdat`;
- turning recording off leaves the history listable;
- **a Component 2-shaped partial replacement makes the history invisible, and the
  full payload restores it** — the regression the client is written to avoid;
- deleting a path (what disabling a camera does) makes both `/list` and
  `/v3/recordings/get` refuse with "not configured", and recreating it brings the
  same files back;
- `recordDeleteAfter` deletes expired recordings on its own;
- a source that disappears and returns resumes recording on the same path;
- four cameras record at once, and stopping one leaves the other three recording.

### What the E2E suite proves

Registration plays in a real browser (`readyState ≥ 2`, `videoWidth > 0`, and
the frame hash changes); WebRTC comes from port 8889 and the browser never
touches the simulator; four cameras play independently and deleting one leaves
the others live; renaming does not interrupt playback; changing a URL keeps the
same path and WebRTC URL; disable/enable; source stops → `OFFLINE` → source
returns → live again without recreating the camera; the focused view leaves
other tiles alone and hands the session back on close; browser refresh
recovers; add and delete through the UI; a credentialed URL never appears in
the page.

For recording: a new camera is not recording until asked; the tile toggle turns
recording on and the badge converges to `RECORDING` **without interrupting the
live session**; a recorded MP4 loads from the playback server into a native
`<video controls>` and its `currentTime` actually advances; a day with nothing
recorded shows the empty state rather than an error; **a failing
`/api/recordings` shows history `UNAVAILABLE` while the tile still reads
`RECORDING` and `LIVE`**; a disabled camera's Recordings button is disabled and
its preference survives; a source outage moves recording to `WAITING` and back to
`RECORDING` by itself; and one camera recording leaves another live-only camera
alone.

### Verified by hand

- MediaMTX restart: paths vanish, the poll spots the drift, a reconcile
  restores all four, players recover.
- VMS backend restart: registrations survive, the startup reconcile reapplies
  the enabled cameras.
- `docker compose down` then `up`: same camera ids, all `ONLINE` again.
- MediaMTX down at VMS startup: the UI and API serve normally, cameras list as
  `UNKNOWN`, a camera added during the outage is kept and applied when MediaMTX
  returns.

For recording, the full demo flow of §11 of the PRD was run against both stacks:

- an existing Component 2 database upgraded in place on first start
  (`schema_upgraded added=recording_enabled`), keeping its three cameras and
  defaulting every one of them to recording off;
- camera A recording while camera B stayed live-only, each listing only its own
  history;
- the returned `playback_url` fetched with `curl` gave `Content-Type: video/mp4`
  and `ffprobe` confirmed H.264 320x240 of the expected duration;
- recording off, then on again: live never dropped and the old history stayed
  listable;
- MediaMTX restarted: `reconciled reason=mediamtx-recovered enabled=2`, recording
  resumed and history came back;
- source stopped and restarted: `RECORDING` → `WAITING` → `RECORDING`, no
  recreation and no retoggle;
- backend rebuilt and restarted: preference and history survived, and the startup
  reconcile issued **no** path replacement for the already-correct camera, so the
  recorder was never interrupted;
- `docker compose down` then `up` with volumes retained: same camera id, still
  recording, history intact.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Camera stuck `OFFLINE` | Check the source itself: `ffprobe -rtsp_transport tcp <url>`. From Docker, the host is `host.docker.internal`, not `localhost` |
| Health `UNKNOWN` on everything | MediaMTX is unreachable; `docker compose logs vms-mediamtx` and check `/health` |
| Tile stays `CONNECTING`, health `ONLINE` | ICE cannot get through. Confirm `8189/udp` is published and `PUBLIC_WEBRTC_HOST` matches the address the browser used |
| Black tile from another machine | `PUBLIC_WEBRTC_HOST` is still `localhost` |
| Video never appears, no errors | The stream's codec may not be WebRTC-compatible. MediaMTX does not transcode; H.264 is the practical baseline |
| UI looks stale after a rebuild | Assets are version-stamped and sent `no-store`; a hard refresh clears anything older |

Useful log events: `camera_created`, `camera_updated`, `camera_deleted`,
`mediamtx_path_ensured`, `mediamtx_path_deleted`, `reconcile_requested`,
`reconciled`, `mediamtx_unreachable`, `mediamtx_recovered`,
`camera_source_rejected`.

## Known limitations

Live view:

- A WebRTC session that stalls without the peer connection failing can read
  `LIVE` until ICE consent checks time out (~30 s). The health badge, which
  comes from MediaMTX, is the authoritative signal in that window.
- MediaMTX 1.20.1 exposes no per-source error, so an `OFFLINE` camera gets a
  generic message rather than "authentication failed" or "no route to host".
  `docker compose logs vms-mediamtx` has the detail.
- RTSP credentials are stored unencrypted (see Security).
- No authentication, no multi-user support, no metrics endpoint.
- Capacity is whatever the host can decode; there is no admission control.
  Four cameras is a functional demonstration, not a capacity claim.

Recording and playback — all verified, none worked around:

- **`RECORDING` is inferred, not measured.** MediaMTX 1.20.1 exposes no
  recorder-writer health, so the badge means "configured, and MediaMTX has the
  source". A disk that is full or unwritable is not reliably visible through its
  APIs, and the badge could read `RECORDING` while writes are failing.
- **A disabled camera's history cannot be browsed.** Disabling removes the
  camera's MediaMTX path (Component 2 semantics), and the playback server refuses
  any path it does not have configured — verified: both `/list` and
  `/v3/recordings/get` answer `400 not configured`. The files are untouched on
  disk and reappear the moment the camera is enabled again. The API says so
  explicitly with `409 disabled_camera_history_unavailable` rather than
  pretending the recordings are gone.
- **A row in the recordings list is a timespan, not a file.** MediaMTX merges
  consecutive segments, so one row covers everything recorded between two
  interruptions of the source, and it includes the segment currently being
  written. `RECORDING_SEGMENT_DURATION` is a floor on file length, not on what
  the list shows.
- **No server-side seeking.** The playback server sends `Accept-Ranges: none`,
  so seeking works only within what the browser has already buffered. `format=mp4`
  puts `moov` first so playback starts immediately, but a long recording must
  download before its end is reachable.
- **Audio is not stripped.** No native way to exclude audio from a recording
  was found in 1.20.1, and adding FFmpeg purely to strip it was out of scope. A
  camera that publishes audio will have it recorded. Test and demo sources here
  are video-only.
- **Codec pass-through.** MediaMTX records what the camera sends. H.264 is the
  practical browser baseline; H.265 will record but not play in most browsers,
  and nothing transcodes.
- **Orphaned recording directories are not cleaned up.** Retention is
  MediaMTX's own `recordDeleteAfter`, which applies to configured paths. Deleting
  a camera removes its path, so whatever it had recorded stops ageing out and
  stays on disk until removed by hand or by `docker compose down -v`.
- No migration framework. The Component 2 → 3 column was added by a small
  idempotent upgrade at startup; a larger future change would need more.

## Not in scope

A visual scrub timeline, per-camera retention or segment profiles, manual
deletion of individual recordings, disk quotas and storage tiers, cloud/object
storage, snapshots, thumbnails, motion or event recording, AI/analytics, event
search, ONVIF, discovery, PTZ, authentication, multi-user, Kubernetes, and any
simulator integration.

Deliberately *not* built, with reasons rather than omissions:

- **No filesystem playback fallback and no FastAPI media proxy.** MediaMTX's
  playback server serves recordings directly and correctly; putting Python in
  the media path would add a failure mode and a copy for no gain.
- **No SQLite recordings index.** MediaMTX already answers the discovery
  question authoritatively. A materialised copy would be a second source of
  truth that could disagree with the files.
- **No orphan cleanup and no manual delete.** Both are real gaps (see Known
  limitations), but deleting media is the one irreversible thing this system
  could do, and neither was needed for the recording, playback and recovery
  behaviour above.

### The Component 4 boundary

AI stays independent. MediaMTX holds every camera as a named path with both a
live and a recorded consumer already attached; a third can read the same paths
without touching either:

```text
                          ┌─ WebRTC ────▶ Live        (implemented)
RTSP camera ──▶ MediaMTX ─┼─ recording ─▶ History     (implemented)
                          └─ AI consumer/events       (Component 4)
```

No detection tables, embeddings or AI-specific media processing exist here.
