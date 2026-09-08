# VMS Recording & Historical Playback (Component 3) — Engineering Handoff

> **Historical document — preserved as audit evidence, not re-validated here.**
> This handoff records the implementation and validation of VMS Recording & Playback (Component 3) as it was
> carried out in its own branch and working clone. Every command, count, measurement
> and acceptance result below was executed **there**, before the unified-submission
> refactor, and is reproduced unchanged. It is **not** a claim about the unified
> repository.
>
> The implementation it describes now lives at `services/vms/`, imported byte-identically
> from `feat/vms-recording` at `21feefd6e65bf7f8f4ac246126863aa7a3dc7bbf`.
> Validation actually executed against the unified repository is recorded separately in
> [HANDOFF_unified_submission.md](HANDOFF_unified_submission.md).

---

**Repository:** `Genea_VMS`
**Component:** Recording and Historical Playback (Component 3)
**Branch:** `feat/vms-recording`
**Base:** Component 2 (live view) — see [component-2-vms-live-view.md](component-2-vms-live-view.md)
**Status:** Implemented, tested, and verified end-to-end in Docker
**Date of handoff:** 2026-09-06

> **How to review this document.** §§1–9 describe what was built and why. §10 is
> the verification record — what was proven against the real server, not read
> from documentation. §§11–13 are the honest parts: limitations, things
> deliberately not built, and open questions. A reviewer hunting for weaknesses
> should start at §11 and cross-check against §§4–7.

---

## Table of contents

1. [What changed](#1-what-changed)
2. [Architecture](#2-architecture)
3. [The verified MediaMTX 1.20.1 recording contract](#3-the-verified-mediamtx-1201-recording-contract)
4. [State ownership](#4-state-ownership)
5. [Schema and the Component 2 upgrade](#5-schema-and-the-component-2-upgrade)
6. [Recording lifecycle](#6-recording-lifecycle)
7. [Historical playback](#7-historical-playback)
8. [Security](#8-security)
9. [Frontend](#9-frontend)
10. [Verification record](#10-verification-record)
11. [Known limitations](#11-known-limitations)
12. [Deliberate non-goals](#12-deliberate-non-goals)
13. [Open questions](#13-open-questions)
14. [Files changed](#14-files-changed)

---

## 1. What changed

Component 2 delivered live view. Component 3 adds, beside it and without
disturbing it:

- a per-camera `recording_enabled` preference, defaulting to **off**;
- continuous recording through MediaMTX's own recorder, configured per path;
- persistent recording storage in a Docker volume (or a host bind mount);
- discovery of what was recorded, by camera and UTC day;
- historical playback in the browser, in a native `<video controls>`;
- age-based retention, performed by MediaMTX itself.

**Nothing was added to the live path.** No FFmpeg, no GStreamer, no transcoding,
no media through Python — for recorded video any more than for live. The VMS
gained one new outbound dependency (MediaMTX's playback server) and one new
column.

The whole of Component 2 still passes: the 189 tests it shipped with are all
still here, extended rather than relaxed where the new field changed a signature.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Browser (single page, vanilla JS — no build step)                        │
│   • camera grid, one isolated WebRTC player per tile      (Component 2)  │
│   • recordings dialog: date picker + <video controls>     (Component 3)  │
└────────┬──────────────────┬──────────────────────────┬───────────────────┘
         │ HTTP (JSON)      │ WHEP + WebRTC/ICE        │ GET /get (MP4)
┌────────▼──────────────┐   │                          │
│ FastAPI (vms, :8090)  │   │                          │
│   api/cameras.py      │   │                          │
│   api/recordings.py ◀─┼───┼──────────────┐           │
└────────┬──────────────┘   │              │           │
         │                  │              │ GET /list │
┌────────▼──────────────┐   │   ┌──────────▼───────────▼───────────────────┐
│ CameraManager         │   │   │ MediaMTX 1.20.1 (vms-mediamtx)           │
│   • desired state     │   └───│   :8889 WebRTC   :8189 ICE               │
│   • reconciliation    │       │   :9996 playback server   ← published    │
│   • health + recording│──────▶│   :9997 Control API       ← NOT published│
│     state (in memory) │ ctrl  │        │                                 │
├───────────────────────┤       │        └─▶ /recordings  (volume)         │
│ RecordingManager      │──────▶│            vms_<id>/<date>/<time>.mp4    │
│   • id → path         │ /list └──────────────────────────────────────────┘
│   • date → window     │
│   • builds public URLs│
└────────┬──────────────┘
         │
┌────────▼──────────────┐
│ SQLite: desired state │  id, name, rtsp_url, mediamtx_path,
│  (+ recording_enabled)│  enabled, recording_enabled, timestamps
└───────────────────────┘
```

### Why these boundaries

| Decision | Reason |
| --- | --- |
| MediaMTX native recording | It already holds every camera as a pull-always path. A separate recorder would mean a second RTSP consumer per camera, a supervision problem, and a way for recording to break live. Component 2 set `record: false` explicitly so switching it on would be one deliberate change — it was. |
| MediaMTX's playback server serves the media | It reads its own recording layout, handles time-range assembly and emits browser-playable MP4. A FastAPI `FileResponse` or proxy would put Python in the media path for no gain and one more failure mode. |
| No SQLite recordings index | MediaMTX answers the discovery question authoritatively from the files themselves. An index would be a second source of truth that can disagree with the disk, and would need reconciliation, rebuild-on-restart and stale-row repair — all of which exist only because the index exists. |
| A separate `MediaMTXPlaybackClient` | The playback server is a different port and a different failure domain from the Control API. Separate clients make it structurally hard for a playback outage to be reported as a control-plane outage. |
| A separate `RecordingManager` | `CameraManager` owns desired state and reconciliation. Recording *discovery* is a read-only query with different failure semantics; folding it in would have put playback failures inside the class that owns camera health. |
| Recording state derived, never persisted | Same argument Component 2 made for health: a stored `RECORDING` is stale the moment the process stops and would have to be distrusted at startup anyway. |

---

## 3. The verified MediaMTX 1.20.1 recording contract

Everything below was **verified empirically** against `bluenviron/mediamtx:1.20.1`
with real containers and a real H.264 stream, not read from documentation. The
integration suite re-verifies each claim on every run.

### Path configuration

```json
{"source": "<RTSP URL>", "sourceOnDemand": false, "rtspTransport": "tcp",
 "record": true,
 "recordPath": "/recordings/%path/%Y-%m-%d/%H-%M-%S-%f",
 "recordFormat": "fmp4", "recordPartDuration": "1s", "recordMaxPartSize": "50M",
 "recordSegmentDuration": "5m", "recordDeleteAfter": "24h"}
```

Produces `/recordings/vms_cam_ab12cd34/2026-09-05/23-17-38-002549.mp4`. The `/`
separators in the template do create real subdirectories.

### The finding that shaped the client

**A partial path replacement silently destroys history visibility.** Sending the
Component 2 payload (`source`/`sourceOnDemand`/`rtspTransport`/`record` only)
resets `recordPath` to MediaMTX's default `./recordings/%path/%Y-%m-%d_%H-%M-%S-%f`:

```
before partial replace:  /list → 1 timespan
after  partial replace:  /list → 404 no recording segments found
after  full    replace:  /list → 1 timespan   (the same one)
```

The files were never touched — the server was simply looking somewhere else.
This is why `ensure_path` always sends the entire recording block, **including
when `record` is false**, and why both a unit test and an integration test lock
it down. It is the single most dangerous thing about this API: the failure is
silent, and it looks exactly like data loss.

### Playback server

| Need | Call | Behaviour |
| --- | --- | --- |
| What exists | `GET :9996/list?path=&start=&end=` | Array of `{start, duration, url}` |
| The media | `GET :9996/get?path=&start=&duration=&format=mp4` | `Content-Type: video/mp4` |

- **`/list` returns timespans, not files.** Consecutive segments are merged: 15
  three-second files came back as one 44.99-second entry. A row in the UI is
  therefore everything recorded between two interruptions of the source.
- **Empty history has two different shapes**, and both are normal:
  - `404 no recording segments found` — the path has recorded before, but not in
    this window;
  - `400 lstat /recordings/<path>: no such file or directory` — the path has
    **never** recorded, so its directory does not exist yet. This is the state of
    every camera in the seconds after recording is switched on, and it must read
    as an empty list. *This case was missed by the first round of manual
    verification and only surfaced when the integration suite ran against a
    freshly configured path* — see §10.
- **`400 path '...' is not configured`** is the genuinely unavailable case, and
  is what a disabled camera looks like.
- `/get?format=mp4` writes `ftyp`, then `moov`, then `mdat` — the `moov` box is
  at the **front**, so a `<video>` element can start playing immediately. The
  default `fmp4` variant was not needed.
- **`Accept-Ranges: none`**, `Transfer-Encoding: chunked`. There is no
  server-side byte-range seeking.
- `playbackAllowOrigins: ["*"]` produces the `Access-Control-Allow-Origin` header
  the browser needs.

### Other verified behaviour

- `recordDeleteAfter` really does delete expired recordings, with no help from
  the VMS. With `15s`, everything recorded was gone within 25 seconds.
- **Durations are normalised on read-back**: `5m` → `5m0s`, `24h` → `1d`,
  `0.01h` → `36s`. Config comparison therefore parses durations into seconds
  rather than comparing strings.
- Deleting a path makes `/list` *and* `/v3/recordings/get/{path}` both answer
  `400 not configured`, even though the files remain. Recreating the path makes
  the same files visible again.
- `GET /v3/recordings/get/{path}` lists segment **start times only** — no
  durations and no file paths. It is not a substitute for `/list`.
- MediaMTX 1.20.1 exposes **no recorder-writer health**: nothing reports whether
  writes are currently succeeding. This is why `RECORDING` is inferred (§4).
- No native way to exclude audio from a recording was found.

---

## 4. State ownership

Component 2 kept two state models rigorously apart. Component 3 adds two more,
and the discipline is the same.

| State | Values | Lives in | Derived from |
| --- | --- | --- | --- |
| Camera health | `ONLINE`/`OFFLINE`/`UNKNOWN` | process memory | MediaMTX `/v3/paths/list` |
| Player state | `CONNECTING`/`LIVE`/`RECONNECTING`/`ERROR` | one browser | that WebRTC session |
| **Recording state** | `DISABLED`/`WAITING`/`RECORDING`/`ERROR` | process memory | desired state + source health + recording-config failures |
| **History state** | `LOADING`/`AVAILABLE`/`EMPTY`/`UNAVAILABLE` | the recordings dialog | that one `/api/recordings` request |

### The separation that matters

**Recording state and history state are not the same axis, and conflating them
would be the easy mistake.** The playback server can be down while the recorder
is working perfectly, and a camera can be `RECORDING` while `/list` fails.

Enforced structurally, not by convention:

- `RecordingUnavailable` is raised only by `MediaMTXPlaybackClient`, and
  `CameraManager` never imports or catches it;
- `CameraManager._recording_errors` is written only by `_note_recording_error`,
  which is reachable only from `_apply` — the *control* path;
- `RecordingManager` holds no reference to `CameraManager` and cannot mutate
  camera state even by accident;
- the frontend's `loadRecordings()` calls neither `renderGrid` nor `updateTile`.

Tested at every layer: unit (`test_recording_state_never_reflects_a_playback_failure`),
API (`test_a_playback_outage_does_not_change_recording_state`), static UI
(`test_a_history_failure_stays_in_the_dialog`), and in a real browser
(`test_a_history_failure_does_not_change_the_recording_badge`).

### `RECORDING` is inferred

> MediaMTX 1.20.1 does not expose recorder-writer health. `RECORDING` means
> recording is configured and MediaMTX reports the source as available. It is
> **not** proof that bytes are currently being written successfully.

The UI says so in the badge's tooltip; the README and §11 say so in words. Log
scraping was rejected as a substitute, the same way Component 2 rejected it for
per-source health.

---

## 5. Schema and the Component 2 upgrade

One column:

```sql
ALTER TABLE cameras ADD COLUMN recording_enabled INTEGER NOT NULL DEFAULT 0;
```

There is still **no migration framework**, and adding one for a single column
would have been heavier than the change it manages. Instead `initialize_database`
runs `upgrade_schema`, which inspects `PRAGMA table_info(cameras)` and adds only
what is missing. It is safe to run on every start, on a fresh database, and on
one that is already current.

`DEFAULT 0` matters: an upgraded Component 2 camera must not start writing to
disk because the operator installed a new version.

This was verified on a real Component 2 database twice — in a unit test that
builds one with the old DDL, and for real on the running stack, whose three
existing cameras survived with `recording_enabled = false` and their credentialed
URLs intact (`schema_upgraded added=recording_enabled` in the log).

**No `recordings` table exists**, and a test asserts it does not.

---

## 6. Recording lifecycle

`record` is `camera.enabled && camera.recording_enabled`. The preference is
stored independently of `enabled`, so:

```text
enabled=true,  recording_enabled=false  ->  live only
enabled=true,  recording_enabled=true   ->  live + continuous recording
enabled=false, recording_enabled=true   ->  nothing now; resumes on enable
```

| Event | What happens |
| --- | --- |
| Recording ON/OFF | The same path is replaced with the same source and full recording block, only `record` differs. **Live is configured identically either way and the path is never deleted**, so live playback is not interrupted and existing history stays listable. |
| Camera disabled | The path is deleted (Component 2 semantics, unchanged). The preference is kept in SQLite. |
| Camera re-enabled | The path is recreated with `record` from the stored preference — recording resumes with no operator action. |
| Source lost | Health → `OFFLINE`, recording → `WAITING`. The path stays configured, so existing history stays listable. |
| Source returns | MediaMTX reconnects on its own; recording resumes on the same path. No recreation, no retoggle. |
| MediaMTX restart | Dynamic paths are lost. The health poll sees the drift, reconciliation reapplies every enabled path with its full recording block, and history becomes visible again. |
| Backend restart | SQLite keeps the preferences. The startup reconcile calls `path_config_matches` first and **skips the replacement when MediaMTX is already correct**, so a VMS restart does not interrupt a healthy recorder. Only the `"startup"` reason may skip; every other reason to reconcile is a reason to distrust what MediaMTX holds. |
| Compose restart, volumes kept | Camera ids, preferences and recorded media all survive. |

Retention is `recordDeleteAfter`, derived from `RECORDING_RETENTION_HOURS`.
**The VMS runs no cleanup job and contains no code that deletes a media file.**

---

## 7. Historical playback

```text
GET /api/recordings?camera_id=cam_ab12cd34&date=2026-09-05
```

`RecordingManager` does the three things MediaMTX cannot:

1. resolves a **camera id** to the MediaMTX path that camera owns — the client
   never supplies a path, so it cannot reach another camera's recordings;
2. turns a UTC calendar day into `[dateT00:00:00Z, nextDayT00:00:00Z)`;
3. builds public playback URLs from VMS settings, ignoring the `url` MediaMTX
   returns (which reflects whatever `Host` header the VMS's own request carried —
   a compose-internal address the browser cannot reach).

Dates are UTC calendar days on both sides: the frontend sends
`new Date().toISOString().slice(0, 10)`, so "today" means the same thing in the
browser and in MediaMTX's file layout.

| Outcome | Response |
| --- | --- |
| Recordings exist | `200` with chronological `items` |
| Enabled camera, nothing that day | `200` with `items: []` — including a camera that has never recorded |
| Unknown camera | `404 camera_not_found` |
| Disabled camera | `409 disabled_camera_history_unavailable` |
| Bad date | `422 validation_error` |
| Playback server down / path not configured | `503 recording_unavailable` |

`503` is a **historical-playback** failure. It does not change the camera's
recording state, and the tile badge does not become `REC ERROR`.

Recording ids are `rec_` + a truncated SHA-256 of `camera_id|start|duration`.
They are opaque keys for the frontend to render a list with; they encode no
filesystem path, so they cannot be turned into one. There is no
`GET`/`DELETE /api/recordings/{id}` — see §12.

---

## 8. Security

Everything Component 2 enforced still holds. What Component 3 adds:

| Risk | Mitigation |
| --- | --- |
| A client reaching another camera's recordings | The API takes a camera id. The VMS resolves it to the path that camera owns; a MediaMTX path is never accepted from a client |
| Path traversal / arbitrary file access | No API accepts a filesystem path, none returns one, and **no code in this repo opens, serves or deletes a media file**. `date` must match `^\d{4}-\d{2}-\d{2}$` and is then parsed as a real calendar day |
| Host paths leaking to clients | `/recordings` never appears in a response; a test asserts it |
| Control API address leaking | Playback URLs are built from the *public* playback settings; a test asserts `9997` and `vms-mediamtx` never appear in a recordings response |
| Credentials in recording/playback errors | Both go through `sanitize_text`. It now also masks the password of any credentialed URL embedded in free text, so a message whose originating URL the caller does not know (the playback client never sees a source URL) still cannot carry one |
| Credentials echoed by the config API | `path_config_matches` reads `/v3/config/paths/get`, which contains the password — so it returns a bare `bool`. Nothing derived from that payload escapes the client |

**The playback port is unauthenticated**, exactly as the WHEP port is. Anyone who
can reach `9996` and knows a path name can fetch that camera's recordings. Same
trusted-network posture as Component 2, now covering recorded video too. Port
`9997` remains unpublished; the compose test that asserts it still passes.

---

## 9. Frontend

Extended, not replaced. Same single page, same vanilla JS, no build step.

- **A third badge per tile** — `REC OFF` / `WAITING` / `RECORDING` / `REC ERROR`
  — next to the health and player badges, never merged with either.
- **`Recording: ON/OFF`** on each tile, using the existing
  `PATCH /api/cameras/{id}`. No new endpoint, and it never touches enable/disable.
- **A `Recordings` dialog**: camera name, UTC date picker defaulting to today,
  the four history states, a chronological list of start → end and duration, and
  a native `<video controls preload="metadata" playsinline>`.
- **The recording checkbox ships unchecked** in the add form.

Invariants carried over from Component 2 and asserted by static tests:

- the dialog and every conditional block ship `hidden`, and visibility still goes
  through the one `setModalOpen` helper;
- **`syncTilePlayer` contains no reference to `recording`**, so toggling
  recording cannot restart a live player. Verified in the browser: the badge goes
  to `RECORDING` while the player stays `LIVE`;
- the 2-second poll never opens the recordings dialog and never reloads its list,
  so it cannot disturb a list being read or a recording being watched;
- **the frontend builds no playback URL.** `9996`, `/get?path=` and `format=mp4`
  all appear nowhere in `app.js`; the element's `src` is whatever the API
  returned;
- a stale response cannot paint over a newer one — each load carries a request id;
- closing the dialog pauses the video and removes its `src`, so a large MP4 does
  not keep downloading in the background.

---

## 10. Verification record

### Automated — 376 tests, all passing

**328 default (0.8 s) + 28 integration (~85 s) + 20 browser E2E (~7.5 min).**
Component 2 shipped 215; every one of them still runs.

| Suite | Was | Now | Added |
| --- | ---: | ---: | --- |
| `test_repository` | 13 | 20 | schema, the Component 2 upgrade, preference persistence, no recordings table |
| `test_validation` | 25 | 65 | recording fields, settings validation, duration rendering, playback URLs, UTC date parsing |
| `test_mediamtx_client` | 17 | 38 | full payload, the partial-replace lockdown, config comparison, the playback client |
| `test_camera_manager` | 36 | 52 | preference flow, state derivation, restart skip, playback/recording separation |
| `test_api_cameras` | 34 | 40 | recording fields, toggles, disable/enable persistence |
| `test_api_recordings` | — | 26 | the whole recordings API |
| `test_static_ui` | 29 | 52 | recording controls, the dialog, frontend invariants |
| `test_integration_mediamtx` | 15 | 28 | the §3 contract, against the real server |
| `test_e2e_live_view` | 11 | 20 | the recording flow, in a real browser |

### What the integration suite proves against real MediaMTX 1.20.1

The playback server boots from the shipped config with CORS; a full recording
config is accepted and compares equal despite duration normalisation; a real
H.264 stream produces files in the expected layout; `/list` returns timespans;
`/get?format=mp4` returns `video/mp4` with `moov` before `mdat`; recording off
keeps history listable; **a partial replacement hides history and a full one
restores it**; a deleted path refuses history and recreating it restores the same
files; an empty day and a never-recorded path are both empty, not errors;
`recordDeleteAfter` deletes expired recordings by itself; a source that
disappears and returns resumes recording on the same path; four cameras record
concurrently and stopping one leaves the other three recording.

### What the browser suite proves

A new camera is not recording until asked; the tile toggle turns recording on and
the badge converges to `RECORDING` **while the live session stays `LIVE`**; a
recorded MP4 loads from the playback server into a native `<video controls>` and
its `currentTime` actually advances; the list is chronological and the date
defaults to today (UTC); an empty day shows the empty state, not an error; **a
failing `/api/recordings` shows history `UNAVAILABLE` while the tile still reads
`RECORDING` and `LIVE`**; a disabled camera's Recordings button is disabled and
its preference survives; a source outage moves recording to `WAITING` and back
without intervention; and one camera recording leaves a live-only camera alone.

### Manual acceptance (Docker, both stacks)

| # | Scenario | Result |
| --- | --- | --- |
| 1 | Existing Component 2 database upgraded on first start | `schema_upgraded added=recording_enabled`; 3 cameras kept, all `recording_enabled=false`, credentialed URLs intact |
| 2 | Two cameras registered, A recording, B live only | A `ONLINE`/`RECORDING`, B `ONLINE`/`DISABLED` |
| 3 | History listed for each | A: 1 timespan; B: `[]` — no cross-talk |
| 4 | `playback_url` fetched with `curl` | `Content-Type: video/mp4`; `ffprobe` → `h264 320x240`, duration matching |
| 5 | Recording off on A | Live stayed `ONLINE`; the old history stayed listable |
| 6 | Recording on again | Back to `RECORDING` |
| 7 | `docker compose restart vms-mediamtx` | `reconciled reason=mediamtx-recovered enabled=2`; recording resumed; history returned (2 items) |
| 8 | Source stopped, then restarted | `RECORDING` → `WAITING` → `RECORDING`; no recreation, no retoggle |
| 9 | Backend rebuilt and restarted | Preference and history survived (3 items); **no `mediamtx_path_ensured` for the already-correct camera**, so the recorder was never interrupted |
| 10 | History on a disabled camera | `409 disabled_camera_history_unavailable`, with a message saying the files are still on disk |
| 11 | `docker compose down` then `up`, volumes kept | Same camera id, still `RECORDING`, history intact (4 items) |
| 12 | On-disk layout | `/recordings/vms_cam_<id>/2026-09-05/23-17-38-002549.mp4` |

### Issues found during verification, and fixed

1. **A never-recorded path returned `503`, not an empty list.** The first round
   of manual verification only ever queried paths that had already recorded, so
   it saw `404 no recording segments found` and concluded that was the empty
   case. The integration suite, querying a freshly configured path, got
   `400 lstat /recordings/vms_cam_...: no such file or directory` instead — the
   state of *every* camera in the seconds after recording is switched on. The
   client now treats that specific 400 as an empty history while keeping
   `path ... is not configured` as unavailable, and both a unit and an
   integration test cover it. This is the one bug the tests caught that manual
   checking had missed, and it would have made the common case look broken.
2. **`sanitize_text` could not scrub what it was not told about.** It masks
   passwords from URLs the caller passes in, but the playback client never sees a
   source URL and so had nothing to pass. Rather than argue that the playback
   server never echoes one, a generic backstop was added that masks the password
   of any credentialed URL embedded in free text. Every existing scrubbing test
   still passes.
3. **A browser test asserted `LIVE` before live had started.** It waited for the
   `RECORDING` badge, which converges faster than a WebRTC session, then asserted
   the player was `LIVE`. The precondition was strengthened to wait for both, so
   "still `LIVE` after the outage" now means something.

---

## 11. Known limitations

Every one of these is verified behaviour, not a suspicion.

- **`RECORDING` is inferred, not measured.** MediaMTX 1.20.1 exposes no
  recorder-writer health. A full or unwritable disk is not reliably visible
  through its APIs, so the badge could read `RECORDING` while writes are failing.
  This is the weakest claim the system makes, and it is stated in the UI tooltip
  as well as here.
- **A disabled camera's history cannot be browsed.** Disabling removes the
  MediaMTX path, and the playback server refuses any path it does not have
  configured. The files are untouched and reappear on re-enable. A path with
  `sourceOnDemand: true` and `record: false` *would* make history visible without
  pulling the source, but it leaves a live-capable path configured for a camera
  the operator disabled — a worse lie than the honest `409`.
- **A row in the list is a timespan, not a file**, and it includes the segment
  currently being written. `RECORDING_SEGMENT_DURATION` is a floor on file length,
  not on what the list shows, and actual boundaries follow keyframes.
- **No server-side seeking.** `Accept-Ranges: none`; seeking works only within
  what the browser has buffered. `format=mp4` puts `moov` first so playback starts
  at once, but the end of a long recording is not reachable until it downloads.
- **Audio is not stripped.** No native way to exclude it was found, and adding
  FFmpeg purely to strip audio was out of scope. A camera publishing audio will
  have it recorded.
- **Codec pass-through.** H.265 records but will not play in most browsers, and
  nothing transcodes.
- **Orphaned recording directories are never cleaned up.** Retention applies to
  configured paths, so deleting a camera removes its path and whatever it recorded
  stops ageing out. It stays until removed by hand or by `down -v`. This is the
  clearest functional gap in the component.
- **The playback port is unauthenticated** (§8).
- Recording inherits every Component 2 operational limit: single process, no
  metrics, no admission control, no capacity claim.

---

## 12. Deliberate non-goals

Out of scope by the PRD: scrub timeline, per-camera retention or segment
profiles, disk quotas, storage tiers, cloud/object storage, snapshots,
thumbnails, motion/event recording, schedules, AI, auth, ONVIF, PTZ, Kubernetes,
production observability.

Considered and deliberately **not** built, with reasons:

| Not built | Why |
| --- | --- |
| Filesystem playback fallback | The playback server works. A fallback would be a second, differently-behaved media path maintained for a failure that has not been observed. |
| FastAPI media proxy / `FileResponse` | Puts Python in the media path, adds a copy and a failure mode, and breaks the "no media through this process" property that both components rest on. |
| SQLite recordings index | MediaMTX is already authoritative. An index needs reconciliation, rebuild-on-restart and stale-row repair — all costs that exist only because the index exists. |
| Manual deletion of a recording | `/v3/recordings/deletesegment` deletes one **segment start**, while the UI shows **timespans**. Mapping one to the other means approximating which segments a span covers, and being wrong deletes the wrong footage. Deleting media is the one irreversible thing here. |
| Orphan cleanup for deleted cameras | It is real (§11), but it means the VMS unlinking files under a root it otherwise never touches. Not worth introducing alongside everything else. |
| Source-on-demand paths for disabled cameras | Would restore disabled-camera history at the cost of leaving a live-capable path configured for a disabled camera. |

---

## 13. Open questions

1. **Is the disabled-camera `409` the right answer?** The alternative is a
   history-only path shape that contradicts what "disabled" means. Reviewers who
   want history while disabled should say so explicitly — it is a product
   decision, not a technical obstacle.
2. **Should orphan cleanup exist?** Deleting a camera currently strands its
   recordings forever. A sweep restricted to `vms_`-prefixed directories under the
   configured root, for cameras that no longer exist, would be small — but it is
   the first code that deletes media.
3. **Should `RECORDING` be weaker than it looks?** Given it cannot be verified,
   is a distinct label (`RECORDING?`, or `CONFIGURED`) more honest than a tooltip?
4. **Is a timespan the right unit for the list?** It is what MediaMTX offers and
   it matches "what was recorded between interruptions", but an operator asked to
   find 14:32 may expect five-minute rows.
5. **Should the playback port be authenticated before the WHEP port is?** Both are
   open; recordings are arguably the more sensitive of the two.
6. **Does `path_config_matches` compare enough?** It checks source, `record`,
   `sourceOnDemand`, `rtspTransport`, and the recording block. A field added to
   the payload later must be added there too, or a restart will silently skip a
   path that has drifted.

---

## 14. Files changed

Component 2 files **modified** (extended, never rewritten):

```
app/config.py                     + playback settings, recording settings,
                                    duration validation, URL/template helpers
app/domain/models.py              + recording_enabled, RecordingState,
                                    CameraRecordingStatus, RecordingItem,
                                    RecordingListResponse, recording_id
app/persistence/database.py       + recording_enabled column, upgrade_schema()
app/persistence/camera_repository.py  + recording_enabled through CRUD
app/security/rtsp_url.py          + embedded-credential backstop in sanitize_text
app/services/mediamtx_client.py   + RecordingPathConfig, full-block ensure_path,
                                    path_config_matches, MediaMTXPlaybackClient
app/services/camera_manager.py    + recording preference, state derivation,
                                    restart skip
app/api/cameras.py                (unchanged — the view model carried it)
app/api/errors.py                 + recording_unavailable,
                                    disabled_camera_history_unavailable
app/main.py                       + playback client, RecordingManager, router
app/static/index.html             + recording checkbox, badges, recordings dialog
app/static/app.js                 + recording badge/toggle, recordings dialog
app/static/styles.css             + recording badges, list rows, date picker
mediamtx/mediamtx.yml             + playback server on :9996
docker-compose.yml                + playback port, recordings volume, env
.env.example                      + every new setting, documented
README.md                         rewritten for both components
```

**New:**

```
app/services/recording_manager.py     camera id -> path, date -> window, URLs
app/api/recordings.py                 GET /api/recordings
tests/test_api_recordings.py          26 tests
HANDOFF_component_3.md                this document
```

Tests extended: `test_repository`, `test_validation`, `test_mediamtx_client`,
`test_camera_manager`, `test_api_cameras`, `test_static_ui`,
`test_integration_mediamtx`, `test_e2e_live_view`, `conftest`.

---

*Component 2's handoff remains accurate for everything it describes and is the
reference for the live path: [component-2-vms-live-view.md](component-2-vms-live-view.md).
The user-facing guide is [README.md](../../services/vms/README.md).*
