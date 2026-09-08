# Validation evidence

What was actually measured, how, and what was **not** tested.

---

## How to read this evidence

This document separates two different things, and never blends them:

- **Recorded final Part 1 acceptance** — measurements taken on this machine
  against the unified repository during Part 1 acceptance, on **2026-09-08**.
  The authority for every number here is
  [HANDOFF_unified_submission.md](engineering-handoffs/HANDOFF_unified_submission.md).
  These suites were **not** re-run for the documentation work.
- **Part 2 documentation validation** — checks performed while writing this
  reviewer documentation, to confirm that every documented command, URL, port
  and state name is real. Recorded in
  [HANDOFF_reviewer_documentation.md](engineering-handoffs/HANDOFF_reviewer_documentation.md).

Three rules apply throughout:

1. **Every timing here is an observation, not an SLA.** Figures were measured
   once, on one idle host, and are reported so a reviewer knows the order of
   magnitude — not as a performance guarantee.
2. **Nothing here is a production-readiness claim.** No load test, soak test,
   chaos test, security review or certification was performed.
3. **Skips are reported as skips.** The phrase "all tests pass" does not appear
   in this repository, because four tests conditionally skip.

**Validation environment.** macOS (Darwin 25.6.0) on `arm64` Apple Silicon,
Docker server 29.7.2 (`linux/arm64`), Docker Compose v5.5.0. Everything was run
against the unified repository — not against the original component branches.

The five historical component handoffs alongside this file record validation
performed on their **own** branches. They are historical evidence and their
counts are deliberately **not** added to the totals below.

---

## Automated test matrix

### Totals

| Component | Passed | Conditional skips | Failures |
| --- | ---: | ---: | ---: |
| C1 Simulator | 143 | 0 | 0 |
| C2/C3 VMS | 376 | 0 | 0 |
| C4 Analytics | 600 | 0 | 0 |
| C5 Semantic Search | 265 | 4 | 0 |
| **Total** | **1,384** | **4** | **0** |

> Final Part 1 acceptance recorded **1,384 passes, four explicitly documented
> conditional Semantic Search semantic-quality skips, and no failures.**

### By tier

| Component | Tier / command | Result | Duration |
| --- | --- | --- | ---: |
| C1 | `pytest -q` (default) | 141 passed, 2 deselected | 17.63 s |
| C1 | `pytest -m integration -v` | 2 passed, 141 deselected | 36.46 s |
| VMS | `pytest -q` (default) | 328 passed, 48 deselected | 1.09 s |
| VMS | `pytest -m integration -v` | 28 passed, 348 deselected | 85.60 s |
| VMS | `pytest -m e2e -v` (real Chromium) | 20 passed, 356 deselected | 449.40 s |
| C4 | `pytest -m unit -q` | 524 passed, 76 deselected | 8.28 s |
| C4 | `pytest -m 'integration and not real_model and not four_camera' -q` | 38 passed, 562 deselected | 68.03 s |
| C4 | `pytest -m real_model -q` | 10 passed, 590 deselected | 4.59 s |
| C4 | `pytest -m four_camera -q` | 2 passed, 598 deselected | 75.36 s |
| C4 | `pytest -m e2e -q` | 26 passed, 574 deselected | 33.36 s |
| C5 | `pytest -q -m unit` | 161 passed, 108 deselected | 3.61 s |
| C5 | `pytest -q -m integration` | 64 passed, 205 deselected | 2.19 s |
| C5 | `pytest -q -m real_model` | **13 passed, 4 skipped** | 11.69 s |
| C5 | `pytest -q -m scale` | 4 passed, 265 deselected | 10.64 s |
| C5 | `pytest -q -m e2e` | 13 passed, 256 deselected | 4.37 s |
| C5 | `pytest -q -m real_component4` | **10 passed, 0 failed, 0 skipped**, 259 deselected | 14.35 s |

No assertion was changed, no test deleted, no marker excluded and no skip added
anywhere to reach these numbers.

### The four skips, in full

`tests/real_model/test_semantic_quality.py` skips four cases with *"evaluation
images are not cached locally"*. Its corpus lives in
`tests/assets/cache/`, which is deliberately gitignored and user-owned — the
images are not redistributable, so they are not committed. **This is the only
tier in the repository that does not execute to completion**, and it will skip
in any fresh clone. `scripts/capture_eval_set.py` builds that cache from a live
Component 4 for anyone who wants to run the tier.

### `real_component4` — the tier that was fixed by real data, not by editing

On the first run against the unified repository this tier was
`4 passed, 5 skipped, 1 FAILED`, because the live Component 4 held zero events.
Once a real vehicle event existed, the same **unmodified** tier returned
**10 passed, 0 failed, 0 skipped**, including the previously failing
`test_real_events_become_searchable` and all five previously skipped tests. The
test file's SHA-256 was verified identical to its frozen commit before and
after. Its own output:

```text
live traversal: pages=5 events=450
real pipeline: events=24 searchable=24 complete=24 model_load=2.5s
               index=10.5s over 6 batches for 48 images (220 ms/image incl. fetch)
  overlap poll: 24 -> 295 known events
10 passed, 259 deselected, 3 warnings in 14.35s
```

---

## Live RTSP and VMS validation

Everything below was driven through the **public** APIs and UIs only.

| Check | Method | Accepted result | Caveat |
| --- | --- | --- | --- |
| Simulator publishes real RTSP | `ffprobe -rtsp_transport tcp rtsp://localhost:8554/simulator/…` | `h264 320x240` from the bundled sample | Source is a file published by FFmpeg, not camera hardware |
| Camera stop/start changes the route | RTSP `DESCRIBE` before and after | `404` while stopped, readable again after start | — |
| VMS ingests the source | Camera health polling | `OFFLINE → ONLINE` within 4–8 s | Observation, not a guarantee |
| VMS redistributes RTSP | `ffprobe rtsp://localhost:8555/vms_<id>` | `h264 320x240` | — |
| Full-resolution real source | Acceptance run with a 1920×1080 H.264 30 fps clip | Simulator and VMS both probed `h264 1920x1080` | User-supplied local media, never committed |
| Runtime dependency validation | In-container validator | FFmpeg, ffprobe, database, GStreamer 1.24.2, Torch 2.7.1+cpu, Ultralytics 8.4.49 all confirmed | — |

**Model integrity.** The YOLO11n checkpoint
(`sha256=0ebbc80d…4ee1`) and the SigLIP checkpoint
(`revision=7fd15f06…61ed`, `sha256=2c63cb7d…2af8`) both matched their frozen
pinned values byte for byte. Semantic Search's embedding was additionally
verified **with the container on `--network none`**, proving no model is
downloaded at runtime.

---

## Browser and WebRTC validation

| Check | Method | Accepted result | Caveat |
| --- | --- | --- | --- |
| Live view renders real moving video | VMS `e2e` tier, real Chromium via Playwright | 20 passed — moving pixels confirmed, not just a connected player | **Chromium only.** Firefox and Safari untested |
| Recording toggle does not interrupt live | Same tier | Live playback continued across the toggle | — |
| Recorded MP4 plays in the browser | Same tier | Playback confirmed | — |

WebRTC delivery is WHEP from MediaMTX on `:8889` with ICE media on `8189/udp`.
No transcoding is involved, so browser compatibility follows the source codec —
H.264 is the validated path.

---

## Recording and playback validation

| Check | Accepted result | Caveat |
| --- | --- | --- |
| Recording produces finalized timespans | `GET /api/recordings` returned 1 timespan of 48.7 s | Listing shows **finalized** segments only |
| Historical playback serves media | `HTTP 200`, `Content-Type: video/mp4` | Served directly by MediaMTX on `:9996`; no byte passes through the VMS |
| Playback content is genuine | Downloaded file probed `h264 320x240`, 55.33 s, 544,251 bytes | Independently verified with `ffprobe`, not trusted from headers |
| Range requests | `Accept-Ranges: none` | **No byte-range seeking within a segment.** A documented behaviour, not a defect |
| Recording survives restart | After stop/start, recordings grew from 1 to 2 timespans, camera identity preserved | — |

---

## Real analytics event validation

The complete C1 → VMS → C4 → C5 path was closed with **real, newly generated
data**, using a user-supplied traffic clip (H.264 1920×1080, 30 fps, 306 s)
driven through the normal Simulator workflow. That clip is local acceptance
input; it is gitignored and was never committed, which is why the demo guide
asks reviewers to bring their own.

**Baseline immediately before the run:** Component 4 held **0 events**;
Component 5 reported `known_events: 0`, `searchable_events: 0`,
`index_revision: 0`. Every event below is therefore provably new.

| Stage | Accepted result |
| --- | --- |
| C1 | Camera `RUNNING`; probed `h264 1920x1080` |
| VMS | `OFFLINE → ONLINE` within 8 s; redistribution probed `h264 1920x1080` |
| C4 worker | `STARTING → RUNNING` in 8 s; line geometry round-tripped exactly |
| Line | One horizontal segment across both carriageways, direction `BOTH`. **Only the runtime line placement was chosen** — no model, tracker, threshold or crossing logic was touched |
| Detections | Real YOLO11n `car`, `truck` and `bus` events, all category `vehicle`, all direction `A_TO_B` — the correct sign for the observed downward motion |
| Event images | `GET /api/events/<id>/frame` → `HTTP 200 image/jpeg`, 1920×1080, 511,861 bytes. `GET /api/events/<id>/crop` → `HTTP 200 image/jpeg`, 157×203, 9,272 bytes. Both fetched through **public** endpoints |
| Visual confirmation | Both images were opened and inspected: the crop is a dark car seen from above, and the frame shows that same car at exactly the recorded bounding box. **The images correspond to the vehicle and the event** |
| Inference cost | Warm inference at `imgsz=640` measured at **53.1 ms** — one observation on an idle host |
| Event store integrity | `verify_event_store.py` → `problems=0 · event store is consistent` |

---

## Real C4 → C5 validation

Nothing was inserted into Component 5. Its normal polling and backfill path
discovered the events over `GET {C4}/api/*` and embedded them.

| Metric | Before | After |
| --- | ---: | ---: |
| `known_events` | 0 | 486 |
| `searchable_events` | 0 | 486 |
| `complete_events` | 0 | 486 |
| `crop_indexed` / `frame_indexed` | 0 / 0 | 486 / 486 (972 vectors) |
| `partial` / `failed` / `pending` / `permanent_error` | — | 0 / 0 / 0 / 0 |
| `index_revision` | 0 | 972 |
| `backfill_complete` | — | `true` |

**Text search** (`POST /api/search/text`, unmodified ranking and thresholds):

| Query | Candidates | Result |
| --- | ---: | --- |
| `car` | 486 | Rank 1 is a `car` event, score 0.086529 |
| `truck` | 486 | Top 5 are `truck`/`bus` — large vehicles, scores 0.089–0.099 |
| `truck` + class filter `truck` | **98** (from 486) | All five results are `truck` |
| `a car on the road` | 486 | The specific acceptance event returned at rank 54 of 100, score 0.077336 |
| `person walking` | 486 | Only cars exist in this corpus; scores collapse to 0.028–0.033 |

Ranking is therefore genuinely discriminating: vehicle words rank vehicles, a
class filter narrows the candidate set correctly, and a query for an object the
corpus does not contain scores far lower. **Scores across 486 near-identical
overhead car crops are legitimately tight**, which is why a specific event sits
mid-list for a generic car query rather than first. This is reported rather than
hidden: there is no claim that any given event ranks first for a generic query.

**Image search** (`POST /api/search/image`), using the event's own crop fetched
from Component 4's **public** endpoint:

```text
candidate_count=486   elapsed_ms=319.4
rank 1: evt_f564acbf…  car  score 1.000000   <== the acceptance event
rank 2: evt_b47234bd…  car  score 0.921594
rank 3: evt_6d813cd5…  car  score 0.904580
```

The exact newly generated event was retrieved at **rank 1 with score 1.000000
out of 486 candidates**. This is the expected result for an identical query
vector and is **not** a general accuracy claim.

---

## Failure isolation, outage, and recovery

### Component 4 outage with real indexed data

The corpus was first frozen by stopping the Simulator camera, so the index could
not move underneath the test.

| Check | C4 up | **C4 stopped** | After restart |
| --- | --- | --- | --- |
| C5 `/health` | `200 · ok · upstream available` | **`200 · degraded · search ok · upstream unavailable · indexing paused`** | `200 · ok · upstream available` |
| Index state | 486/486, rev 972 | **486/486, rev 972 — unchanged** | 486/486, rev 972 |
| `upstream_error_code` | `None` | `component4_unreachable` | `None` |
| Text `a car on the road` | rank 54, score 0.077336 | **rank 54, score 0.077336** | — |
| Image (event crop) | rank 1, score 1.0 | **rank 1, score 1.0** | — |
| C5 local event detail | 200 | **200** | 200 |
| C5 crop proxy | 200 | 503 — correct: the image lives upstream | **200** |
| VMS / Simulator `/health` | 200 | **200** | 200 |

Text and image retrieval of the real indexed event were **byte-identical before
and during the outage**, and no vector was invalidated. Recovery to
`upstream: available` was automatic in **6 seconds**, with the **same container
id** throughout — Component 5 was never restarted.

### Service-stop isolation

| Action | Observed |
| --- | --- |
| Stop Analytics only | VMS `ok`, camera `ONLINE` + `RECORDING`; Simulator `ok`; `rtsp://localhost:8555/vms_…` still `h264 320x240` |
| Restart Analytics | `RUNNING` again in 10 s, with a **new** worker session id |
| Stop Semantic Search | Simulator, VMS and Analytics all `200`, VMS camera still `ONLINE`/`RECORDING`, C4 worker still `RUNNING` |
| Analytics data reset (`down -v` on that stack only) | Analytics returned with a clean database while the VMS camera was still `ONLINE` + `RECORDING` — a downstream reset does not reach upstream state |

### Compose project isolation

`down --remove-orphans` was run once from **each** stack while the other three
ran. In all four cases only that project's own containers and network were
removed; peers stayed running and answering `HTTP 200`, and every production
volume remained. `down -v` was then run once per stack with peer volume
`CreatedAt` compared before and after: only that stack's own volumes were
removed, every time.

---

## Persistence and rebuild validation

| Check | Accepted result |
| --- | --- |
| Stop/start cycle | The VMS camera kept an identical `id`, `mediamtx_path`, `created_at` and `recording_enabled`; the Analytics camera kept its id and VMS binding; the Simulator camera survived; recordings grew from 1 to 2 timespans |
| No runtime artifact leaked into Git | `git status --short` was **empty** after all runtime activity |
| Unplanned container loss | Every container was lost at the daemon level mid-session. **All volumes survived**, and after `start-all.sh` the Analytics events, the Semantic Search index and the acceptance event (`index_state: complete`, both representations indexed) were all still present |
| Rebuild from genuinely empty volumes | After all five production volumes were deleted, the whole chain rebuilt from nothing: four `/health` → `200`; Simulator `h264 320x240`; VMS `ONLINE` in 8 s; C4 `RUNNING` in 8 s with a valid 5,336-byte JPEG snapshot; recording → 1 timespan of 52.3 s fetched as `video/mp4` and verified with `ffprobe`; C5 `upstream: available`. **Nothing depended on residue** |

Container-level durability was **observed**; it is not a guarantee against host
failure or disk loss, and there is no backup or restore procedure.

---

## Root orchestration and fresh-clone evidence

| Command | Accepted result |
| --- | --- |
| `./scripts/stop-all.sh` | Reverse order, "volumes kept", exit 0 |
| `./scripts/start-all.sh` | Preflight found four distinct projects; all four became ready; exit 0 |
| `./scripts/status.sh` | All four "application ready (HTTP 200)"; exit 0 |
| `./scripts/smoke-test.sh` | **38 passed, 0 failed**; exit 0 |
| `./scripts/start-all.sh` (second run) | Exit 0, idempotent, no data wipe |
| With `services/video-analytics/.env` absent | Fails **in preflight**, starts nothing, prints the exact `cp` command. It never creates a `.env` for you |
| Service-local operation after using the wrappers | `docker compose ps`, `config --quiet`, `--services --filter status=running` and `exec` all succeed from each service directory — the root scripts are optional |

**Fresh clone.** A `git clone --no-local` into a temporary directory produced an
empty `git status --porcelain`; no developer absolute path appeared anywhere
outside the archived engineering documents; Compose parsed for Simulator, VMS
and Semantic Search, with Analytics failing **by design** until the single
documented `cp` (after which it parsed); **all four images built from the clone**
with no sibling directory present; suites re-ran with identical counts (C1 141,
VMS 328, C4 unit 524, C5 unit 161); `start-all.sh`, `status.sh` and
`smoke-test.sh` all passed with **38 passed, 0 failed**; and the live chain came
up under the clone. A pristine checkout reproduces and operates the whole system
with **no sibling clone, no developer virtualenv, no pre-existing model cache,
no hidden environment variable and no undocumented manual step.**

> This was a **local** fresh-clone run performed during Part 1. Final
> certification from the public remote is a separate, later step and has **not**
> been performed.

---

## Part 2 documentation validation

These checks were performed while writing this documentation. They validate
**documentation correctness**, not the application — the 1,384-test matrix was
deliberately not re-run because only Markdown changed.

| Check | Method | Result |
| --- | --- | --- |
| Git baseline | `git rev-parse`, `git status`, branch inspection | Branch created from the exact Part 1 SHA; tracked tree otherwise clean |
| Root script syntax | `bash -n` on all four scripts | All four parse |
| Compose validity | `docker compose config --quiet` in each service directory | All four parse |
| Compose project names | `docker compose config --format json` | `genea-simulator`, `genea-vms`, `genea-analytics`, `genea-semantic-search` — four distinct |
| Compose services | `docker compose config --services` | `simulator`+`mediamtx`; `vms`+`vms-mediamtx`; `analytics`; `semantic-search` |
| Named volumes | `docker volume ls` | `genea-simulator-data`, `genea-vms-data`, `genea-vms-recordings`, `genea-analytics-data`, `genea-semantic-search-data` |
| Published ports | `docker ps` port bindings | Exactly the documented set; **`9997` confirmed not published** |
| Dashboards, Swagger, health, OpenAPI | `curl` against `/`, `/health`, `/docs`, `/openapi.json` on all four ports | **16 of 16 returned HTTP 200** |
| Playback server | `curl http://localhost:9996/list` | Answers (HTTP 400 without parameters), confirming it is published and live |
| Private control API | `curl http://localhost:9997/...` | Connection refused — correctly unreachable from the host |
| C5 index status | `GET /api/index/status` | `known/searchable/complete = 486/486/486`, `crop_indexed=486`, `frame_indexed=486`, `index_revision=972`, `backfill_complete=true`, `upstream_state=available` — matching the recorded acceptance state exactly, which also re-confirms volume persistence |
| Bundled sample media | `ffprobe` | `h264`, 320×240, 10.0 s — confirming it cannot contain a detectable person or vehicle |
| UI labels, states and routes | Read directly from the static UIs, domain models and API routers | Every label, state name and endpoint used in the demo guide exists in the code |
| Documentation links | Relative-link extraction and resolution across all reviewer and service documents | All resolve |
| Mermaid | Syntax review and render check of both diagrams | Both render |
| Terminology and safety | Scans for stale references, private media paths, developer-local absolute paths, secrets and runtime artifacts | Clean |

**Not re-run in Part 2:** the automated test matrix, the browser E2E tier, the
live media chain, and the real analytics-event walkthrough. Those results are
the recorded Part 1 acceptance evidence above. No new event-generating clip was
introduced for the documentation work, and none was needed.

---

## Known untested areas

Stated plainly, because a reviewer's time is better spent here than on the
passing rows.

1. **The Semantic Search semantic-quality evaluation did not run** — 4 skips.
   Its corpus is deliberately gitignored and user-owned. The only tier in the
   repository that does not execute to completion.
2. **Retrieval quality is not a general accuracy claim.** The acceptance corpus
   is 486 events from a single five-minute traffic clip filmed from one fixed
   viewpoint, and its class labels come from Component 4's own detector, which
   can itself be wrong. Ranking was shown to discriminate; no benchmark was run
   and none is claimed.
3. **Only vehicles were exercised live.** The clip contained cars, trucks and
   buses. The `person` category is implemented, enabled and unit-tested, but no
   person crossing was produced end to end.
4. **amd64 was not built or tested.** Everything is `linux/arm64` on Apple
   Silicon with Docker Desktop.
5. **Native Linux `host-gateway` was not exercised.** The `extra_hosts` mappings
   are present and unchanged, but were verified on Docker Desktop for macOS only.
6. **No real IP camera and no credentialed RTSP source end to end.** The
   acceptance source was a file published by the Simulator, not a camera
   demanding authentication. No direct webcam adapter exists.
7. **No load, soak or chaos testing** beyond the stop/start, outage, reset and
   container-loss cycles recorded above. The longest continuous run was minutes.
8. **Browser coverage is Chromium only.**
9. **Analytics four-camera fairness is host-dependent.** The gate passed on an
   idle host (75.36 s) and was not re-tested under oversubscription.
10. **No security review, penetration test, backup/restore drill, or
    multi-node failover test** was performed. There is no authentication or TLS
    to test.
11. Every component-level limitation in the five historical component handoffs
    still applies, because no application code was modified.

---

## Evidence sources

- **[HANDOFF_unified_submission.md](engineering-handoffs/HANDOFF_unified_submission.md)**
  — the authority for every Part 1 figure on this page. §6 is the validation
  record, §7 failure isolation, §8 the fresh clone, and **§9 is "What is NOT
  proven"**, which a sceptical reviewer should read first.
- **[HANDOFF_reviewer_documentation.md](engineering-handoffs/HANDOFF_reviewer_documentation.md)**
  — the Part 2 documentation record.
- Historical component evidence, from the original component branches and
  **not** included in the totals above:
  [C1](engineering-handoffs/component-1-rtsp-simulator.md) ·
  [C2](engineering-handoffs/component-2-vms-live-view.md) ·
  [C3](engineering-handoffs/component-3-recording-playback.md) ·
  [C4](engineering-handoffs/component-4-video-analytics.md) ·
  [C5](engineering-handoffs/component-5-semantic-search.md)
- **How to re-run any of this yourself:** each service README documents its own
  test tiers and commands; the root wrappers are documented in the
  [README](../README.md), and the reviewer walkthrough is
  [demo-guide.md](demo-guide.md).
