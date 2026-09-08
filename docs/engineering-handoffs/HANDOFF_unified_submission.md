# Unified Submission Repository — Implementation Handoff

**Repository:** `Genea_VMS`
**Branch:** `refactor/unified-submission`
**Base:** `bdd4b72987da6aa0c44b73934646793eea9748bb` (`main`, one file: `README.md`)
**Plan implemented:** [PLAN_unified_submission.md](../engineering/plans/PLAN_unified_submission.md)
**Requirements authority:** [PRD_unified_submission_repository_for_codex.md](../engineering/requirements/PRD_unified_submission_repository_for_codex.md)
**Date:** 2026-09-08
**Status:** Phases 0–9 executed **and the real-event acceptance closed**. Ready for review.

> **What this document is.** Everything below was executed on this machine against
> this branch, and every number is a measurement taken during this work — not a
> figure copied from a component handoff. Where a validation could not be run, it
> says so and says why. The five component handoffs alongside this file are
> historical evidence from their own branches and are explicitly *not* claims
> about this repository.
>
> A reviewer looking for weaknesses should start at §9 (what is **not** proven)
> and §10 (deviations), then cross-check against §6.

---

## Table of contents

1. [What was done](#1-what-was-done)
2. [Provenance](#2-provenance)
3. [Final repository tree](#3-final-repository-tree)
4. [What changed because of relocation, and what did not](#4-what-changed-because-of-relocation-and-what-did-not)
5. [Compose identity, ports, volumes, networking](#5-compose-identity-ports-volumes-networking)
6. [Validation actually executed](#6-validation-actually-executed)
7. [Failure isolation results](#7-failure-isolation-results)
8. [Fresh-clone results](#8-fresh-clone-results)
9. [What is NOT proven](#9-what-is-not-proven)
10. [Deviations from the plan](#10-deviations-from-the-plan)
11. [Known limitations](#11-known-limitations)
12. [Merge-to-main readiness](#12-merge-to-main-readiness)

---

## 1. What was done

Four independently developed feature branches were consolidated into one
reviewer-facing repository with four independently deployable Compose stacks,
**without changing any application behaviour**.

```text
services/rtsp-simulator/    <- feat/rtsp_simulation  @ a6e02da3e029c742a4d5c2fe92e1fe1de85011f7
services/vms/               <- feat/vms-recording    @ 21feefd6e65bf7f8f4ac246126863aa7a3dc7bbf
services/video-analytics/   <- feat/video_analytics  @ 786062c64fd51c20bafb02c04031ad8558c925de
services/semantic-search/   <- feat/image_retrival   @ 6df06855be8c29d1f87d1509fb26a9222d7de6fb
```

Components 2 and 3 are **one** VMS service. `feat/vms` was not imported
separately: `git merge-base --is-ancestor feat/vms feat/vms-recording` returns
success, so the Component 3 branch already contains Component 2 in full.

Import mechanism: `git read-tree --prefix=<target>/ -u <sha>`, one service per
phase, each validated and committed before the next began. No branch merge, no
working-directory copy, so no untracked `.env`, virtualenv, cache, database or
local asset from any development clone could enter the tree.

### Commits on this branch

| Commit | Subject |
| --- | --- |
| `0fce601` | chore: scaffold unified submission layout |
| `13ea728` | refactor: import rtsp simulator service |
| `c02e0a3` | refactor: import vms live and recording service |
| `fce53c4` | refactor: import video analytics service |
| `6ba33ff` | refactor: import semantic search service |
| `a1922dc` | chore: add unified stack lifecycle scripts |
| `57eea56` | docs: preserve component engineering handoffs |

---

## 2. Provenance

Every source SHA was re-verified against the live remote with `git ls-remote`
immediately before implementation, and all six matched the plan exactly.

| Component | Branch | SHA | Files imported | Snapshot check |
| --- | --- | --- | ---: | --- |
| C1 Simulator | `feat/rtsp_simulation` | `a6e02da3e029c742a4d5c2fe92e1fe1de85011f7` | 42 | exact |
| C2+C3 VMS | `feat/vms-recording` | `21feefd6e65bf7f8f4ac246126863aa7a3dc7bbf` | 43 | exact |
| C4 Analytics | `feat/video_analytics` | `786062c64fd51c20bafb02c04031ad8558c925de` | 91 | exact |
| C5 Semantic Search | `feat/image_retrival` | `6df06855be8c29d1f87d1509fb26a9222d7de6fb` | 92 | exact |

"exact" means: after each import, the `(mode, blob-id, path)` triples under the
target prefix were diffed against `git ls-tree -r <sha>` and the diff was
**empty**. C4's three executable scripts kept mode `100755`.

**The original feature branches are untouched.** No branch was deleted, moved,
force-pushed or rewritten; `refactor/unified-submission` is a new branch created
from the base commit.

The five component handoffs were verified by SHA-256 before and after relocation:

| Handoff | SHA-256 (unchanged by the move) |
| --- | --- |
| component-1-rtsp-simulator.md | `64e45c028863a2f9b5aaec9d81abacdc14222770fe2f7cb11c4e71c524fd0a24` |
| component-2-vms-live-view.md | `9f6484c52d0397b9089461ed12614a59cddc628d23c29aa55ef860853901477f` |
| component-3-recording-playback.md | `d1d032883a3ef38c573f233d560f22f51f6b028f04d4a6c5c4edc58cf8143ec9` |
| component-4-video-analytics.md | `71185cc373d2dac8be676f03e1c6baaeaa48f539fff55d0a45e8c22fdaaf4edf` |
| component-5-semantic-search.md | `fe4edfc7fb4bc557e87da0f1d80c6829cc61a94c7fdf01f7371f4f62106327f0` |

---

## 3. Final repository tree

```text
Genea_VMS/
├── README.md                    (unchanged one-line placeholder; the reviewer README is Part 2)
├── .gitignore
├── docs/
│   ├── engineering/
│   │   ├── plans/PLAN_unified_submission.md
│   │   └── requirements/PRD_unified_submission_repository_for_codex.md
│   └── engineering-handoffs/
│       ├── component-1-rtsp-simulator.md
│       ├── component-2-vms-live-view.md
│       ├── component-3-recording-playback.md
│       ├── component-4-video-analytics.md
│       ├── component-5-semantic-search.md
│       └── HANDOFF_unified_submission.md      (this file)
├── scripts/
│   ├── start-all.sh   status.sh   stop-all.sh   smoke-test.sh
└── services/
    ├── rtsp-simulator/     41 files
    ├── vms/                44 files
    ├── video-analytics/    91 files
    └── semantic-search/    92 files
```

There is **no root Compose file**, no root application package, no shared
requirements file, no shared network and no root `.env.example`.

Committed build contexts stay small and service-scoped — 328 / 452 / 340 /
300 KiB — so relocation did not turn any Docker context into the monorepo root.

---

## 4. What changed because of relocation, and what did not

### Did not change

No API, database schema, event model, camera model, recording semantic,
MediaMTX configuration, WebRTC behaviour, inference scheduling, tracking,
line-crossing, embedding model, ranking, dependency lock, pinned image or public
port. No service gained an import, a mount or a network route into another.

### Changed — the complete list

**Simulator (2 files)**
- `docker-compose.yml`: `name: genea-simulator`; volume `simulator-data` → physical name `genea-simulator-data`
- `README.md`: quick-start path

**VMS (5 files)**
- `docker-compose.yml`: `name: genea-vms`; volumes → `genea-vms-data`, `genea-vms-recordings`
- `.dockerignore`: **new**, keeps caches, virtualenvs, local databases and recordings out of the build context
- `pyproject.toml`: new `e2e = ["playwright==1.62.0"]` optional dependency — the browser suite imported Playwright but nothing declared it, so the tier was not reproducible from a clean checkout
- `tests/test_integration_mediamtx.py`: **docstring only** — removed a suggested in-container invocation the image cannot satisfy (it ships neither a Docker client nor FFmpeg). No test logic or assertion touched.
- `README.md`: monorepo paths and the host E2E environment setup

**Analytics (4 files)**
- `docker-compose.yml` / `docker-compose.test.yml`: physical volume names `genea-analytics-data`, `genea-analytics-test-data`
- `tests/unit/conftest.py`: removed the one pre-existing trailing blank line that `git diff --check` reported. No test logic changed.
- `README.md`: quick-start path; backup command and prose corrected to the explicit physical volume name

**Semantic Search (3 files)**
- `docker-compose.yml` / `docker-compose.test.yml`: physical volume names `genea-semantic-search-data`, `genea-semantic-search-test-data`
- `README.md`: quick-start path

Verified mechanically: after edits, the C4 tree differed from its frozen commit
in exactly 4 files and the C5 tree in exactly 3, with **zero** files added or
removed in either.

---

## 5. Compose identity, ports, volumes, networking

### Project and volume names

| Stack | Project | Volumes (physical) |
| --- | --- | --- |
| Simulator | `genea-simulator` | `genea-simulator-data` |
| VMS | `genea-vms` | `genea-vms-data`, `genea-vms-recordings` |
| Analytics | `genea-analytics` | `genea-analytics-data` (+ `genea-analytics-test-data`) |
| Semantic Search | `genea-semantic-search` | `genea-semantic-search-data` (+ `genea-semantic-search-test-data`) |

Before this work, **Simulator and VMS both resolved to the project name
`genea_vms`**, because their development clones sat in directories of the same
basename. That is fixed. `docker compose ls --all` now shows exactly four
projects, each pointing at its own directory in this repository.

### Ports — all preserved, verified from the running stacks

| Capability | Container | Host |
| --- | ---: | ---: |
| Simulator UI/API | 8080 | 8080 |
| Simulator RTSP | 8554 | 8554 |
| VMS UI/API | 8090 | 8090 |
| VMS RTSP out | 8554 | **8555** |
| VMS WHEP/WebRTC | 8889 | 8889 |
| VMS ICE | 8189/udp | 8189/udp |
| VMS playback | 9996 | 9996 |
| VMS MediaMTX Control API | 9997 | **not published** (verified) |
| Analytics UI/API | 8100 | 8100 |
| Semantic Search UI/API | 8200 | 8200 |

### Networking contracts — unchanged

Every cross-stack hop still goes over a published host port, with
`host.docker.internal:host-gateway` preserved on VMS, Analytics and Semantic
Search:

```text
VMS       --RTSP/TCP--> host.docker.internal:8554/simulator/*
Analytics --RTSP/TCP--> host.docker.internal:8555/vms_*
Analytics --HTTP GET--> host.docker.internal:8090/api/recordings
Search    --HTTP GET--> host.docker.internal:8100/api/*
```

Normalised Compose confirms each service mounts **only its own** volume:
Analytics `[analytics-data → /data]`, Semantic Search
`[semantic-search-data → /data]`, and no peer volume name appears in any other
stack's Compose files.

Static boundary scans: Analytics' only `/recordings` references are the public
HTTP path `{VMS_API_BASE_URL}/api/recordings`; there is no `:9997` and no
`docker.sock`. Semantic Search's executable code, with comments and docstrings
stripped, contains none of `docker.sock`, `/recordings`, `9997`, `rtsp://`,
`rtsps://`, `8554`, `8555`, `9996`, `8090`, `faiss`, `qdrant`, `openai`.

---

## 6. Validation actually executed

Host: macOS (Darwin 25.6.0), `arm64` Apple Silicon. Docker server **29.7.2**
(`linux/arm64`), Docker Compose **v5.5.0**. Everything below was run against
this branch.

### 6.1 Automated test tiers

| Stack | Command | Result | Historical |
| --- | --- | --- | --- |
| C1 | `docker compose run --rm --no-deps simulator pytest -q` | **141 passed**, 2 deselected, 17.63 s | 141 |
| C1 | `docker compose exec -T simulator pytest -m integration -v` | **2 passed**, 141 deselected, 36.46 s | 2 |
| VMS | `docker compose run --rm --no-deps vms pytest -q` | **328 passed**, 48 deselected, 1.09 s | 328 |
| VMS | `.venv/bin/python -m pytest -m integration -v` | **28 passed**, 348 deselected, 85.60 s | 28 |
| VMS | `.venv/bin/python -m pytest -m e2e -v` | **20 passed**, 356 deselected, 449.40 s | 20 |
| C4 | `pytest -m unit -q` | **524 passed**, 76 deselected, 8.28 s | 524 |
| C4 | `pytest -m 'integration and not real_model and not four_camera' -q` | **38 passed**, 562 deselected, 68.03 s | 38 |
| C4 | `pytest -m real_model -q -rA` | **10 passed**, 590 deselected, 4.59 s | 10 |
| C4 | `pytest -m four_camera -q` | **2 passed**, 598 deselected, 75.36 s | 2 |
| C4 | `pytest -m e2e -q` | **26 passed**, 574 deselected, 33.36 s | 26 |
| C5 | `pytest -q -m unit` | **161 passed**, 108 deselected, 3.61 s | 161 |
| C5 | `pytest -q -m integration` | **64 passed**, 205 deselected, 2.19 s | 64 |
| C5 | `pytest -q -m real_model -rA` | **13 passed, 4 skipped**, 11.69 s | 17 (13+4) |
| C5 | `pytest -q -m scale` | **4 passed**, 265 deselected, 10.64 s | 4 |
| C5 | `pytest -q -m e2e` | **13 passed**, 256 deselected, 4.37 s | 13 |
| C5 | `pytest -q -m real_component4 -rA` | **10 passed**, 259 deselected, 14.35 s | 10 |

**1,384 tests passed** (143 Simulator, 376 VMS, 600 Analytics, 265 Semantic
Search). No assertion was changed, no test deleted, no marker excluded and no
skip added anywhere.

One tier still does not run to completion:

- **C5 `real_model` — 4 skips.** `tests/real_model/test_semantic_quality.py`
  skips with *"evaluation images are not cached locally"*. Its corpus lives in
  the deliberately gitignored, user-owned `tests/assets/cache/`. Expected in any
  fresh tree, anticipated by the plan, and recorded here as a **skip, not a
  pass**.

**`real_component4` is now fully green.** On the first run of this branch it was
`4 passed, 5 skipped, 1 FAILED`, because live Component 4 held zero events. Once
a real vehicle event existed (§6.5) the same unmodified tier returned
**10 passed, 0 failed, 0 skipped**, including the previously failing
`test_real_events_become_searchable` and all five previously skipped tests. The
test file was never touched: its SHA-256 is still
`7cf947d9cbda3b49169a71e85513c5d91c319fc0934378d4103893b7e99ec59f`, identical to
the frozen commit. Its own output for this run:

```text
live traversal: pages=5 events=450
real pipeline: events=24 searchable=24 complete=24 model_load=2.5s
               index=10.5s over 6 batches for 48 images (220 ms/image incl. fetch)
  overlap poll: 24 -> 295 known events
10 passed, 259 deselected, 3 warnings in 14.35s
```

### 6.2 Runtime validators

Analytics, in the running container:

```text
ok  platform aarch64 Linux python=3.12.3
ok  fastapi 0.116.1  uvicorn 0.35.0  pydantic 2.13.5  httpx 0.28.1
ok  numpy 2.5.3  PIL 11.3.0  supervision 0.30.2  ultralytics 8.4.49
ok  gstreamer core=1.24.2 factories=6 gstrtsp_typelib=present
ok  torch 2.7.1+cpu torchvision 0.22.1 cuda=False
ok  model /opt/models/yolo11n.pt bytes=5613764
    sha256=0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1
ok  tracker bytetrack   ok inference blank_frame_detections=0
All selected runtime checks passed.
```

`verify_event_store.py` → `problems=0 · event store is consistent`.
Measured warm inference at `imgsz=640`: **53.1 ms**.

Semantic Search, **with the container on `--network none`**:

```text
ok  torch 2.7.1+cpu torchvision 0.22.1 cuda=False
ok  model-manifest files=7 model_id=siglip-base-p16-224@7fd15f06-tf4.57.6-prep1
    revision=7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed
    sha256=2c63cb7d1f2e95ba501893cbb8faeb4ea9a3af295498d35097126228659c2af8
ok  offline-inference dim=768 load_ms=575 image_ms=234 text_ms=78 offline=1
```

Both model checksums match the values frozen in the plan **byte for byte**, and
neither service downloads a model at runtime.

### 6.3 Live media chain

Registered through the public APIs only:

| Step | Result |
| --- | --- |
| C1 camera from the bundled sample | `ffprobe rtsp://localhost:8554/simulator/…` → `h264 320x240` |
| stop / start the C1 camera | RTSP `DESCRIBE 404` when stopped, readable again after start |
| VMS camera on the simulator URL | `OFFLINE` → `ONLINE` within 4–8 s |
| VMS redistributed RTSP | `ffprobe rtsp://localhost:8555/vms_<id>` → `h264 320x240` |
| VMS recording | `GET /api/recordings` → 1 timespan, 48.7 s |
| Historical playback | `HTTP 200`, `Content-Type: video/mp4`, `Accept-Ranges: none`; downloaded file `ffprobe` → `h264 320x240`, 55.33 s, 544,251 bytes |
| Browser WebRTC | proven by the 20 passing VMS E2E tests (real Chromium, moving pixels, recording toggle without interrupting live, MP4 playback) |
| C4 on the VMS RTSP path | `STARTING` → `RUNNING` in 4 s; line geometry round-tripped exactly |
| C4 decoded frames | `GET /snapshot` → `HTTP 200 image/jpeg`, real 320×240 JPEG, 10,683 bytes, `nosniff` |
| C4 inference | `last_inference_at` advancing over those frames |
| C5 upstream | `upstream_state: available` against the live relocated C4 |

### 6.4 Root orchestration

```text
./scripts/stop-all.sh    -> reverse order, "volumes kept", exit 0
./scripts/start-all.sh   -> preflight: four distinct projects; all four ready; exit 0
./scripts/status.sh      -> all four "application ready (HTTP 200)"; exit 0
./scripts/smoke-test.sh  -> passed 38, failed 0; exit 0
./scripts/start-all.sh   -> second run: exit 0, idempotent, no data wipe
```

With `services/video-analytics/.env` absent, `start-all.sh` fails **in
preflight**, starts nothing, and prints the exact `cp` command — it never
creates a `.env` for you.

Service-local operation still works after using the wrappers: `docker compose
ps`, `config --quiet`, `--services --filter status=running` and `exec` all
succeed from each service directory. The scripts also work when invoked by
absolute path from an unrelated working directory, because they derive the
repository root from their own location.

### 6.5 Real-event acceptance: C1 → VMS → C4 → C5

This closes the one path that was blocked when this branch was first validated.
A user-supplied traffic video was driven through the **normal** Component 1
Simulator workflow — no ad-hoc publisher, no manual event insertion, and no
direct access to Component 4's private storage from Component 5.

**Input asset** (local acceptance input, gitignored, never committed):

| Property | Value |
| --- | --- |
| File | `test_videos/traffic_1080p_video_demo.mp4` |
| Video codec | H.264 High @ L4.0, `yuv420p` |
| Resolution | 1920 × 1080 |
| Frame rate | 30/1 |
| Duration | 306.13 s |
| Size | 170,767,056 bytes |
| Audio | AAC LC (the Simulator strips audio by design) |

**Baseline immediately before the run:** Component 4 held **0 events**;
Component 5 reported `known_events: 0`, `searchable_events: 0`,
`index_revision: 0`. Every event below is therefore provably new and produced
from this video during this run.

**Component 1 — uploaded through the public API**

```text
camera      cam_0eb224fb  "acceptance-traffic"  status RUNNING
stream_path acceptance-traffic
ffprobe -rtsp_transport tcp rtsp://localhost:8554/simulator/acceptance-traffic
            -> codec_name=h264  width=1920  height=1080
```

The Simulator's own `ffprobe` correctly reported the source as
`h264 1920x1080 30 fps, 306.13 s, has_audio=true`.

**VMS — registered through the public API**

```text
camera        cam_beb581fb  "acceptance-traffic-vms"
mediamtx_path vms_cam_beb581fb
health        OFFLINE -> ONLINE within 8 s
ffprobe -rtsp_transport tcp rtsp://localhost:8555/vms_cam_beb581fb
              -> codec_name=h264  width=1920  height=1080
```

**Component 4 — real detection, tracking and line crossing**

```text
camera  acam_4963c478  "acceptance-traffic-analytics"
source  rtsp://host.docker.internal:8555/vms_cam_beb581fb   (published host port)
worker  STARTING -> RUNNING in 8 s
line    line_92de82d2  a=(0.08, 0.55)  b=(0.62, 0.55)  direction BOTH
```

The line is a horizontal segment across both carriageways of a real multi-lane
road; traffic in the scene moves top-to-bottom in frame, so vehicles cross it.
**Only runtime line placement was chosen — the crossing implementation, model,
tracker, thresholds and event semantics were not touched.** The very first line
placement produced events immediately.

Real YOLO11n detections produced `car`, `truck` and `bus` events, all category
`vehicle`, all direction `A_TO_B` — the correct sign for downward motion given
the line's normal. The acceptance event:

| Field | Value |
| --- | --- |
| Event id | `evt_f564acbf66c17c128fa00bdcd42cc149` |
| Analytics camera | `acam_4963c478` |
| VMS camera | `cam_beb581fb` |
| Line | `line_92de82d2` |
| Class / category | `car` / `vehicle` |
| Direction | `A_TO_B` |
| Confidence | 0.7003132104873657 |
| Track id | 80 |
| Worker session | `ws_77ce3f6c3fd115da59a0b03b59f3defe` |
| `crossed_at` | 2026-09-08T07:57:59.618Z |
| bbox (normalised) | x 0.2370–0.3188, y 0.5130–0.7009 |
| Frame size | 1920 × 1080 |

Both images were fetched through the **public** endpoints and validated:

```text
GET /api/events/evt_f564.../frame -> HTTP 200  image/jpeg  nosniff
                                     cache-control: private, max-age=31536000, immutable
                                     mjpeg 1920x1080, 511,861 bytes
GET /api/events/evt_f564.../crop  -> HTTP 200  image/jpeg  nosniff
                                     mjpeg 157x203, 9,272 bytes
```

Both were opened and inspected visually: the crop is a dark Opel car seen from
above, and the frame is the traffic scene with that same car at exactly the
recorded bounding box. **The images correspond to the vehicle and the event.**

**Component 5 — normal public-HTTP discovery and SigLIP indexing**

Nothing was inserted into Component 5's store. Its existing polling and backfill
path discovered the events over `GET {C4}/api/*` and embedded them:

```text
before:  known_events=0    searchable_events=0    index_revision=0
after:   known_events=486  searchable_events=486  complete_events=486
         crop_indexed=486  frame_indexed=486  (972 vectors)
         partial=0  failed=0  pending=0  permanent_error=0
         index_revision=972  backfill_complete=true
```

The acceptance event specifically:

```text
GET {C5}/api/events/evt_f564acbf66c17c128fa00bdcd42cc149 -> HTTP 200
  object_class=car  object_category=vehicle  direction=A_TO_B
  index_state=complete   representations={"crop":"indexed","frame":"indexed"}
```

**Text search** (`POST /api/search/text`, unmodified ranking and thresholds):

| Query | Candidates | Result |
| --- | ---: | --- |
| `a car on the road` | 486 | **acceptance event returned at rank 54 of 100, score 0.077336** |
| `car` | 486 | rank 1 is a `car` event, score 0.086529 |
| `truck` | 486 | top 5 are `truck`/`bus` — large vehicles, scores 0.089–0.099 |
| `person walking` | 486 | only cars exist in this corpus, and scores collapse to 0.028–0.033 |
| `truck` + class filter `truck` | **98** (from 486) | all five results are `truck` |

Ranking is therefore genuinely discriminating rather than arbitrary: vehicle
words rank vehicles, a class filter narrows candidates correctly, and a query
for an object the corpus does not contain scores far lower. Scores across 486
near-identical overhead car crops are legitimately tight, which is why the
acceptance event sits mid-list for a generic car query rather than first.

**Image search** (`POST /api/search/image`), using the event's own crop fetched
from Component 4's **public** endpoint — never from its private storage:

```text
candidate_count=486   elapsed_ms=319.4   index_revision=900
rank 1: evt_f564acbf66c17c128fa00bdcd42cc149  car  score 1.000000   <== acceptance event
rank 2: evt_b47234bd7723bf1fec7dda35eec0235c  car  score 0.921594
rank 3: evt_6d813cd502787dea9878755359dbbc8f  car  score 0.904580
```

**The exact newly generated event is retrieved at rank 1 with score 1.000000 out
of 486 candidates.**

### 6.6 Component 4 outage with real indexed data

The earlier structural-only outage proof is now closed with real data. The
corpus was first frozen by stopping the Simulator camera, so the index could not
move underneath the test.

| Check | C4 up (baseline) | C4 stopped | After C4 restart |
| --- | --- | --- | --- |
| C5 `/health` | `200 · ok · upstream available` | **`200 · degraded · search ok · upstream unavailable · indexing paused`** | `200 · ok · upstream available` |
| Index | 486/486, crop 486, frame 486, rev 972 | **486/486, crop 486, frame 486, rev 972 — unchanged** | 486/486, rev 972 |
| `upstream_error_code` | `None` | `component4_unreachable` | `None` |
| Text `a car on the road` | rank **54**, score **0.077336** | rank **54**, score **0.077336** | — |
| Image (event crop) | rank **1**, score **1.0** | rank **1**, score **1.0** | — |
| C5 local event detail | 200 | **200** | 200 |
| C5 crop proxy | 200 | 503 (correct: the image lives upstream) | **200** |
| VMS `/health` | 200 ok | **200 ok** | 200 ok |
| Simulator `/health` | 200 | **200** | 200 |

Text and image retrieval of the real indexed event were **byte-identical before
and during the outage**, and no vector was invalidated.

Recovery: `docker compose start analytics` → Component 5 returned to
`upstream: available` **automatically in 6 seconds**, with the **same container
id** (`5f395d495754`) throughout — Component 5 was never restarted — and its
crop proxy returned to `HTTP 200`.

**Durability bonus.** Midway through this session every container was lost at
the daemon level. All volumes survived, and after `./scripts/start-all.sh` the
Component 4 events, the Component 5 index and the acceptance event
(`index_state: complete`, both representations indexed) were all still present.

---

## 7. Failure isolation results

### `down --remove-orphans` — run once from **each** stack while peers ran

In all four cases only that project's own containers and network were removed.
Peers stayed running and kept answering (`HTTP 200`), and all five production
volumes remained. This is precisely the hazard the old shared `genea_vms`
project name created, and it is gone.

### `down -v` — run once per stack, peer volume `CreatedAt` compared before/after

| Stack | Removed | Peers |
| --- | --- | --- |
| Analytics | only `genea-analytics-data` | all peer volumes kept their **original** `CreatedAt`; C1/VMS/C5 all `HTTP 200` |
| Semantic Search | only `genea-semantic-search-data` | unchanged |
| VMS | only `genea-vms-data` **and** `genea-vms-recordings` | unchanged |
| Simulator | only `genea-simulator-data` | unchanged |

After the Analytics reset, Analytics came back with a clean database (0 cameras)
while the VMS camera was still `ONLINE` + `RECORDING` — a downstream reset does
not reach upstream state.

Throughout every destructive test, **all nine legacy development volumes**
(`genea_vms_*`, `genea-analytics_*`, `genea-semantic-search_*`) remained present
and untouched. Compose itself reported this, e.g.:

```text
volume "genea-analytics_analytics-data" carries the compose label "analytics-data"
but does not match the compose file (using "genea-analytics-data"); it is left untouched
```

### Service-stop isolation

| Action | Observed |
| --- | --- |
| Stop Analytics only | VMS `ok` + camera `ONLINE`/`RECORDING`; C1 `ok`; `rtsp://localhost:8555/vms_…` still `h264 320x240` |
| Restart Analytics | `RUNNING` again in 10 s with a **new** worker session id |
| Stop Component 4 | C5 `HTTP 200` `status: degraded`, `search: ok`, `upstream: unavailable`, `indexing: paused`; search served locally; no vector invalidated; `index_revision` unchanged |
| Restart Component 4 | C5 returned to `upstream: available` **automatically in ~25 s**, no restart, no reload |
| Stop Semantic Search | C1 `200`, VMS `200` + `ONLINE`/`RECORDING`, C4 `200` + worker `RUNNING` |

### Persistence

`./scripts/stop-all.sh` then `./scripts/start-all.sh`: the VMS camera kept an
identical `id`, `mediamtx_path`, `created_at` and `recording_enabled`; the
Analytics camera kept its id and VMS binding; the C1 camera survived; recordings
grew from 1 to 2 timespans. `git status --short` was **empty** — no runtime
artifact appeared in any version-controlled path.

### Rebuild from genuinely empty volumes

After the `down -v` sweep deleted all five production volumes, the whole chain
was rebuilt from nothing: all four `/health` → `200`; C1 camera →
`h264 320x240`; VMS camera `ONLINE` in 8 s → `rtsp://localhost:8555/…` →
`h264 320x240`; C4 `RUNNING` in 8 s with a valid 5,336-byte JPEG snapshot; VMS
recording → 1 timespan of 52.3 s fetched as `video/mp4` and verified by
`ffprobe`; C5 `upstream: available`. Nothing depended on residue.

---

## 8. Fresh-clone results

```bash
git clone --no-local <repo> "$TMP/Genea_VMS"
cd "$TMP/Genea_VMS" && git checkout refactor/unified-submission
```

- `git status --porcelain` → **empty**
- HEAD `57eea56a045ef3ce7a260f813a6f329d87129312`
- no developer absolute path anywhere outside the two archived engineering documents
- Compose parses in a pristine checkout for Simulator, VMS and Semantic Search;
  Analytics fails **by design** until the single documented
  `cp services/video-analytics/.env.example services/video-analytics/.env`,
  after which it parses. The created `.env` is correctly ignored.
- **all four images built from the clone** with no sibling directory present
- suites run from the clone, identical counts: C1 **141**, VMS **328**,
  C4 unit **524**, C5 unit **161**
- `./scripts/start-all.sh` → all four ready; `status.sh` → all four
  "application ready (HTTP 200)"; `smoke-test.sh` → **38 passed, 0 failed**
- live chain under the clone: C1 `RUNNING`; VMS `ONLINE` + `RECORDING`;
  `ffprobe rtsp://localhost:8555/vms_…` → `h264 320x240`; C4 `RUNNING`;
  3 recording timespans; C5 `upstream: available`

A pristine checkout reproduces and operates the whole system with **no sibling
clone, no developer virtualenv, no pre-existing model cache, no hidden
environment variable and no undocumented manual step**.

---

## 9. What is NOT proven

This is the honest part. Nothing here is hidden behind a passing summary.

1. **Component 5's semantic-quality evaluation did not run** (4 skips): its
   corpus is deliberately gitignored and user-owned. This is the only tier in
   the repository that still does not execute to completion.

2. **Retrieval quality is not a general accuracy claim.** The acceptance corpus
   is 486 events from a single five-minute traffic clip filmed from one fixed
   viewpoint, and its class labels come from Component 4's own detector, which
   the Component 4 handoff records can itself be wrong. Ranking was shown to
   discriminate (§6.5), but no benchmark was run and none is claimed.

3. **Only vehicles were exercised.** The supplied clip contains cars, trucks and
   buses. The `person` category is implemented and enabled, but no person
   crossing was produced, so that path remains unproven end to end.

4. **amd64 was not built or tested.** Everything here is `linux/arm64` on Apple
   Silicon with Docker Desktop.

5. **Native Linux `host-gateway` was not exercised.** The `extra_hosts` mappings
   are preserved unchanged but were verified on Docker Desktop for macOS only.

6. **No real IP camera and no credentialed RTSP source end to end.** The
   acceptance source was a file published by the Simulator, not a camera that
   demands authentication.

7. **No load, soak or chaos testing** beyond the stop/start, outage, reset and
   container-loss cycles recorded above. The longest continuous run was minutes.

8. **Browser coverage is Chromium only**, via the component E2E suites.

9. Every component-level limitation recorded in the five historical handoffs
   still applies unchanged, because no application code was modified.

---

## 10. Deviations from the plan

| # | Deviation | Why |
| --- | --- | --- |
| 1 | The branch `refactor/unified-submission` already existed locally at the exact base commit `bdd4b72`, so Phase 1 did not create it. | Same result; base commit verified correct and the branch was absent from the remote. |
| 2 | `git mv PLAN_unified_submission.md …` was replaced by `mv` + `git add`. | The plan was untracked, so `git mv` cannot work. This correction was given explicitly with the task. |
| 3 | Stopped legacy development containers were **removed** (`docker rm`, containers only). | C1 and VMS pin fixed `container_name` values (`rtsp-simulator`, `vms`, …), which are global to the Docker engine, so the unified stacks could not start while the legacy containers held those names. Pre-existing property of those Compose files, not caused by relocation. **No volume was removed** — all nine legacy volumes verified present afterwards, and the legacy stacks can be recreated from their own clones against them. |
| 4 | Host repair: `credsStore` was removed from `~/.docker/config.json` (backed up to `~/.docker/config.json.genea-backup`). | Every `docker pull`/`build` hung indefinitely with zero output. Diagnosis: the registry, container networking and Docker Desktop's proxy were all fine (`HTTP 200`/`401` as expected), but `docker-credential-desktop list` produced no output after 12 s, blocking every pull before registry contact. `auths` was empty `{}`, so nothing was lost. Pulls then completed in under 10 s. **A host environment repair — no repository file, image, container or volume was altered by it.** Restore with `cp ~/.docker/config.json.genea-backup ~/.docker/config.json`. |
| 5 | `brew install python@3.12` on the host. | The plan's VMS host test environment requires Python 3.12; the host had only 3.14, which predates the pinned dependency set. Only affects the host test venv (gitignored). |

No architectural deviation was made. No API, schema, model, ranking, ownership
or isolation property was changed.

---

## 11. Known limitations

- Root orchestration is four Bash wrappers over four independent Compose
  projects, not production orchestration. It is **optional**: every service runs
  standalone from its own directory, and that path is verified to still work.
- Cross-stack integration remains over published host ports, by design.
- `reset-all.sh` was deliberately **not** implemented (plan §19.4). Per-service
  `docker compose down -v` is clearer and safer, and is proven isolated (§7).
- The root `README.md` is still the one-line placeholder: the reviewer-facing
  README, diagrams and demo guide are explicitly Part 2.
- Perfect per-file `git log --follow` through relocation is not preserved. The
  original feature branches and the SHAs in §2 are the audit history.
- Several variables documented in the service `.env.example` files are not
  interpolated by their Compose files and therefore cannot override container
  values today. This is **pre-existing** in every component and was deliberately
  not "fixed" here, since changing configuration semantics is outside a
  relocation refactor.
- Analytics four-camera fairness remains host-dependent; the gate passed on an
  idle host (75.36 s) and was not re-tested under oversubscription.
- First Semantic Search build remains large (~2.9 GB image, ~812 MB checkpoint).

---

## 12. Merge-to-main readiness

| Gate | Status |
| --- | --- |
| One unified branch holds all four services | ✅ |
| Final service count is four; C2+C3 are one VMS | ✅ |
| Every service independently runnable and testable | ✅ |
| Every service has independent persistence | ✅ verified destructively |
| Unique Compose project per stack | ✅ four distinct |
| `down --remove-orphans` / `down -v` isolation | ✅ tested from every stack |
| Original feature branches intact | ✅ untouched |
| No verified application contract changed | ✅ |
| No new cross-service import, mount or shared DB | ✅ scanned |
| Existing ports and networking still work | ✅ live-verified |
| Service-local tests still pass | ✅ 1,384 passed |
| Root orchestration works and is optional | ✅ |
| Clean clone needs no sibling clone | ✅ |
| No developer absolute path, no secret, no generated data tracked | ✅ |
| `git diff --check` | ✅ empty |
| `git status --short` | ✅ empty |
| Full C1→VMS→C4→C5 **new-event** acceptance | ✅ **passed** — real vehicle event indexed and retrieved, see §6.5 |
| Real-data Component 4 outage continuity | ✅ identical text and image retrieval while C4 was down, §6.6 |
| `real_component4` tier | ✅ 10 passed, 0 failed, 0 skipped |
| User review and explicit merge approval | ⏳ pending |

**Recommendation: ready to merge.** Every acceptance gate that this repository
can prove is green, including the real-event path that was previously blocked. A
user-supplied traffic clip was driven through the Simulator, produced real
YOLO11n vehicle events in Component 4, and those events were discovered,
embedded and retrieved by Component 5 through the public HTTP contract alone —
by text, and by image at rank 1 with score 1.000000. The previously failing
`real_component4` tier now passes in full without a single test being modified.

What remains open is listed in §9 and none of it blocks this refactor: it is
either deliberately user-owned data (the semantic-quality corpus), a platform
not available here (amd64, native Linux), or a class of testing explicitly out
of scope (real IP cameras, soak and load).

The branch has not been pushed.

---

*Historical component evidence lives beside this file and describes validation
performed in the original branches, not here.*
