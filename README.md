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
  -d '{"name":"Parking Entrance","rtsp_url":"rtsp://10.0.0.9:554/Streaming/Channels/101"}'
```

A syntactically valid URL is accepted even if the camera is unreachable; it
then shows as `OFFLINE` and goes `ONLINE` by itself when the source appears.

Stop with `docker compose down` (registrations persist in a named volume) or
`docker compose down -v` (wipes everything).

## Ports

| Service | Purpose | Host port |
| --- | --- | ---: |
| `vms` | UI + REST API | `8090` |
| `vms-mediamtx` | RTSP | `8555` |
| `vms-mediamtx` | WebRTC HTTP / WHEP signalling | `8889` |
| `vms-mediamtx` | WebRTC ICE (UDP) | `8189/udp` |
| `vms-mediamtx` | Control API | **not published** — compose network only |

`8555` avoids the RTSP Camera Simulator's `8554`, so both stacks run side by
side. The ICE UDP port is published on the same number it listens on, because
an ICE candidate advertises the port it was gathered from.

## Architecture

### Desired state vs. runtime state

| | Where it lives | Who owns it |
| --- | --- | --- |
| Name, source URL, enabled | SQLite (`/data/vms.db`) | the operator |
| MediaMTX paths | MediaMTX, at runtime | derived from SQLite |
| Camera health | this process's memory | derived from MediaMTX |
| Player state | the browser | that browser alone |

SQLite is the source of truth; MediaMTX is treated as a cache of it and rebuilt
whenever the two disagree. **Health is not persisted**: a stored `ONLINE` is
stale the moment the process stops and would have to be distrusted at startup
anyway, so it is recomputed from MediaMTX on every poll. `cameras` therefore
has no `health_state`/`last_error` columns.

### One MediaMTX path per enabled camera

Camera `cam_ab12cd34` owns the path `vms_cam_ab12cd34`. The name is derived
from the id, so it never changes — editing a camera's URL does not invalidate
its WebRTC URL, and the player only has to reconnect. Users cannot choose
paths; the `vms_` prefix is what tells reconciliation "this one is mine".

Each path is configured as:

```json
{"source": "<the RTSP URL>", "sourceOnDemand": false,
 "rtspTransport": "tcp", "record": false}
```

- `sourceOnDemand: false` (pull always) so health answers "can the VMS obtain
  this source?" independently of whether anyone is watching. MediaMTX retries an
  offline source on its own and recovers when it returns.
- `rtspTransport: tcp` because UDP RTP does not survive Docker port mapping.
- `record: false` states the Component 3 boundary explicitly.

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

### Status model

Two states, deliberately never conflated:

**Camera health** — "can the VMS obtain this RTSP source?", from MediaMTX:

| State | Means |
| --- | --- |
| `ONLINE` | MediaMTX has the stream (`available: true`) |
| `OFFLINE` | The path is configured but MediaMTX cannot read the source |
| `UNKNOWN` | MediaMTX is unreachable, the path is not configured yet, or the camera is disabled |

**Player state** — this browser's WebRTC session for one tile: `CONNECTING`,
`LIVE`, `RECONNECTING`, `ERROR`. It never reaches the server.

A tile shows both, so "the camera is down" and "my connection is down" are
distinguishable at a glance.

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

A camera response:

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
    "state": "ONLINE", "last_error": null,
    "checked_at": "2026-09-06T00:00:00.000Z", "mediamtx_available": true
  },
  "created_at": "2026-09-06T00:00:00.000Z",
  "updated_at": "2026-09-06T00:00:00.000Z"
}
```

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
| `validation_error` | 422 | bad payload; `details.fields[]` names the offending field |
| `camera_not_found` | 404 | unknown id |
| `internal_error` | 500 | unhandled |

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
| `PUBLIC_WEBRTC_HOST` | `localhost` | what the browser is told to connect to |
| `MEDIAMTX_API_URL` | `http://vms-mediamtx:9997` | Control API, compose network only |
| `DATABASE_PATH` | `/data/vms.db` | inside the `vms-data` volume |
| `CAMERA_HEALTH_POLL_SECONDS` | `2.0` | health refresh interval |
| `LOG_LEVEL` | `INFO` | |

**Serving other machines:** set `PUBLIC_WEBRTC_HOST` to the host's address.
It reaches both the `webrtc_url` the API hands out and MediaMTX's
`webrtcAdditionalHosts`, so the ICE candidates match. Leaving it at `localhost`
means only the Docker host's own browser can play video.

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

**Credentials are stored in plain SQLite.** That is a deliberate V1 limit for a
trusted local/lab deployment; encryption or a secret store belongs to a later
security component.

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
| `test_static_ui` | dialogs ship closed, assets versioned, player lifecycle rules, health/player separation |
| `test_integration_mediamtx` | the shipped config boots; dynamic path CRUD; a live FFmpeg stream flipping `available` true then false; the Control API really does echo passwords |
| `test_e2e_live_view` | video file → simulator → RTSP → VMS MediaMTX → WebRTC → Chromium, with real pixels |

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

### Verified by hand

- MediaMTX restart: paths vanish, the poll spots the drift, a reconcile
  restores all four, players recover.
- VMS backend restart: registrations survive, the startup reconcile reapplies
  the enabled cameras.
- `docker compose down` then `up`: same camera ids, all `ONLINE` again.
- MediaMTX down at VMS startup: the UI and API serve normally, cameras list as
  `UNKNOWN`, a camera added during the outage is kept and applied when MediaMTX
  returns.

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

- A WebRTC session that stalls without the peer connection failing can read
  `LIVE` until ICE consent checks time out (~30 s). The health badge, which
  comes from MediaMTX, is the authoritative signal in that window.
- MediaMTX 1.20.1 exposes no per-source error, so an `OFFLINE` camera gets a
  generic message rather than "authentication failed" or "no route to host".
  `docker compose logs vms-mediamtx` has the detail.
- RTSP credentials are stored unencrypted (see Security).
- No migration framework. A future schema change needs a migration step or a
  volume reset.
- No authentication, no multi-user support, no metrics endpoint.
- Capacity is whatever the host can decode; there is no admission control.
  Four cameras is a functional demonstration, not a capacity claim.

## Not in scope

Recording, playback, timeline, snapshots, AI/analytics, event search, ONVIF,
discovery, PTZ, authentication, multi-user, Kubernetes, and any simulator
integration are all out of scope for Component 2.

Recording is Component 3. The attachment point is left clean: MediaMTX already
holds every camera as a named path, so a recording consumer can read those
paths (or MediaMTX's own recording can be switched on per path) without
changing the live delivery path. No recording schema, retention, storage or
playback logic exists here — `record: false` is set explicitly on every managed
path so turning it on later is a deliberate act.
