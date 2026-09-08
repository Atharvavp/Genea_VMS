# Implementation Plan: Unified Genea Submission Repository

**Plan status:** Ready for review; implementation is not authorized by this document alone
**Prepared:** 2026-09-08
**Target implementation branch:** `refactor/unified-submission`
**Requirements authority:** `PRD_unified_submission_repository_for_codex.md`

## 1. Executive summary

**FROZEN REQUIREMENT:** This is a repository relocation and submission-packaging refactor. It is not a functional or architectural redesign. The result has four independently deployable Compose stacks:

1. `services/rtsp-simulator/` from `feat/rtsp_simulation`.
2. `services/vms/` from `feat/vms-recording`, which already contains Components 2 and 3.
3. `services/video-analytics/` from `feat/video_analytics`.
4. `services/semantic-search/` from `feat/image_retrival`.

**CODEX IMPLEMENTATION DECISION:** Import the four verified commit snapshots with `git read-tree --prefix=<target>/ -u <sha>`. Do not merge the feature branches into one root tree. The branches all diverge from the one-file initial commit and have many conflicting root paths; prefix import preserves the exact committed blobs and modes while avoiding semantic merge resolution. Original branches remain the historical record.

**CODEX IMPLEMENTATION DECISION:** Preserve service-local Compose files and the existing host-published integration topology. Add explicit project names to Simulator and VMS, retain the existing explicit names for Analytics and Semantic Search, and give every production data volume an explicit physical name. Root scripts wrap the four stacks by changing into each service directory; they do not replace the service-local workflows.

**VERIFIED FACT:** No source application file requires a relocation edit. Every Docker build context, Dockerfile `COPY`, bind mount, test-root calculation, and runtime static path is service-relative or container-absolute. Required implementation edits are limited to Compose identity/volume hardening, one missing VMS E2E test dependency declaration, one VMS `.dockerignore`, operational documentation paths, one existing whitespace defect, root scripts, repository ignore rules, and preserved engineering documents.

**RUNTIME VALIDATION REQUIRED:** Docker Compose files were normalized and parsed during planning, but Docker Desktop was not running (`docker.sock` did not exist). No current container, volume, image, live endpoint, or runtime isolation claim was revalidated in this planning session. All such claims remain gates below.

## 2. Source-of-truth hierarchy

Use this order when implementation evidence conflicts:

1. **FROZEN REQUIREMENT:** The unification PRD is authoritative for scope, service boundaries, final mapping, and acceptance.
2. **VERIFIED FACT:** The committed tree at each frozen source SHA is authoritative for code and test content.
3. The Component 1–5 handoffs are historical implementation and validation evidence. They do not override the PRD or the source commits.
4. Service READMEs describe operation, but any claim must be checked against code and Compose.
5. This plan freezes relocation decisions. Claude must stop and record a deviation before making a new architectural decision.

Do not treat the pasted request, handoff prose, untracked component plans, local `.env` files, old containers, or old volumes as application source.

## 3. Documents inspected completely

| Role | Planning-time path | SHA-256 | Treatment |
|---|---|---|---|
| User request | `/Users/atharvapawar/.codex/attachments/51e28b2c-e4b6-412a-ac43-4c57b76edf14/pasted-text.txt` | `ffec0a1aaf289df8d731a118a9f71976795a615dac931a35dd85a36004f1ac4e` | Session instruction; do not import |
| Unification PRD | `/Users/atharvapawar/Downloads/PRD_unified_submission_repository_for_codex.md` | `a96b6f33549c24d7a91d4a985d7c93cf04bbc1f0037461b88d2ce7b1b488aa4c` | Requirements authority; preserve under engineering docs |
| Component 1 handoff | `/Users/atharvapawar/workspace/genea_assignment/Genea_VMS/HANDOFF.md` | `64e45c028863a2f9b5aaec9d81abacdc14222770fe2f7cb11c4e71c524fd0a24` | Historical evidence; identical to the C1 branch blob |
| Component 2 handoff | `/Users/atharvapawar/workspace/genea_assignment/VMS/Genea_VMS/HANDOFF_component_2.md` | `9f6484c52d0397b9089461ed12614a59cddc628d23c29aa55ef860853901477f` | Historical evidence; untracked in its source clone |
| Component 3 handoff | `/Users/atharvapawar/workspace/genea_assignment/VMS/Genea_VMS/HANDOFF_component_3.md` | `d1d032883a3ef38c573f233d560f22f51f6b028f04d4a6c5c4edc58cf8143ec9` | Historical evidence; untracked in its source clone |
| Component 4 handoff | `/Users/atharvapawar/workspace/genea_assignment/ai-services/Genea_VMS/HANDOFF_component_4.md` | `71185cc373d2dac8be676f03e1c6baaeaa48f539fff55d0a45e8c22fdaaf4edf` | Historical evidence; untracked in its source clone |
| Component 5 handoff | `/Users/atharvapawar/workspace/genea_assignment/image_retrival/Genea_VMS/HANDOFF_component_5.md` | `fe4edfc7fb4bc557e87da0f1d80c6829cc61a94c7fdf01f7371f4f62106327f0` | Historical evidence; untracked in its source clone |

The PRD and all five handoffs were read in full. The actual branch trees, Compose files, Dockerfiles, environment examples, application settings, tests, scripts, ignore files, MediaMTX configs, and operational README sections were then inspected independently.

## 4. Verified repository baseline

| Item | Verified value |
|---|---|
| Repository path | `/Users/atharvapawar/workspace/genea_assignment/refactored/Genea_VMS` |
| Git remote | `origin = https://github.com/Atharvavp/Genea_VMS.git` |
| Current branch | `main` |
| Current HEAD | `bdd4b72987da6aa0c44b73934646793eea9748bb` |
| `origin/main` | same SHA |
| Working tree | clean (`## main...origin/main`) before this plan was created |
| Delivery-time status | `?? PLAN_unified_submission.md`; this planning document is the only workspace change |
| Current main tree | one file, `README.md`, containing `# Genea_VMS` |
| Target branch | absent locally, absent as a tracking ref, and absent from the live remote query |

**VERIFIED FACT:** A live `git ls-remote` query on 2026-09-08 matched every locally tracked branch SHA below. Re-run the query immediately before implementation and stop if any SHA differs.

## 5. Source provenance and working-tree evidence

| Component | Frozen branch | Frozen SHA | Committed files / bytes | Source-clone state | Compose project at source location | Primary persisted volume keys |
|---|---|---|---:|---|---|---|
| C1 | `feat/rtsp_simulation` | `a6e02da3e029c742a4d5c2fe92e1fe1de85011f7` | 42 / 300,303 | Tracked files clean; untracked `plan.md` | `genea_vms` (directory-derived) | `simulator-data` |
| C2+C3 | `feat/vms-recording` | `21feefd6e65bf7f8f4ac246126863aa7a3dc7bbf` | 43 / 375,990 | Tracked files clean; untracked C2/C3 handoffs and `PLAN_component_3.md`; ignored `.env`, `.venv`, caches | `genea_vms` (directory-derived) | `vms-data`, `vms-recordings` |
| C4 | `feat/video_analytics` | `786062c64fd51c20bafb02c04031ad8558c925de` | 91 / 755,178 | Tracked files clean; untracked handoff/plan; ignored `.env` | `genea-analytics` (explicit) | `analytics-data`, test-only `analytics-test-data` |
| C5 | `feat/image_retrival` | `6df06855be8c29d1f87d1509fb26a9222d7de6fb` | 92 / 591,977 | Tracked files clean; untracked handoff; ignored `.env` and `tests/assets/cache/` | `genea-semantic-search` (explicit) | `semantic-search-data`, test-only `semantic-test-data` |

**VERIFIED FACT:** “Tracked files clean” means there are no modified or staged tracked files. The source working directories as a whole are not clean because the listed untracked/ignored files exist. Snapshot import must therefore read Git objects by SHA, never copy a source working directory.

**VERIFIED FACT:** No tracked DB, recording, event store, model checkpoint, Python cache, `.env`, Playwright output, or local evaluation-image cache exists in any frozen source commit. The largest committed binary is the intentional 41,621-byte simulator sample video; no tracked file exceeded 5 MiB.

## 6. Branch ancestry

All relevant branches share exactly this base:

```text
bdd4b72987da6aa0c44b73934646793eea9748bb  Initial commit
├── a6e02da3e029c742a4d5c2fe92e1fe1de85011f7  feat/rtsp_simulation
├── e5d4fea46429cef1900d99bf3a0bb80cf19774ad  feat/vms
│   └── 21feefd6e65bf7f8f4ac246126863aa7a3dc7bbf  feat/vms-recording
├── 786062c64fd51c20bafb02c04031ad8558c925de  feat/video_analytics
└── 6df06855be8c29d1f87d1509fb26a9222d7de6fb  feat/image_retrival
```

**VERIFIED FACT:** `git merge-base --is-ancestor origin/feat/vms origin/feat/vms-recording` returned success, and their left/right count is `0 1`. Component 3 contains Component 2 and is the only VMS import.

**VERIFIED FACT:** C1, C2, C4, and C5 are each one commit ahead of `main`; C3 is two commits ahead and directly descends from C2. No relevant branch contains another component branch.

## 7. Tree-collision analysis and mechanism choice

The branches intentionally reuse root paths for different applications. Examples:

| Pair | Identical path names in both trees |
|---|---:|
| C1 vs VMS | 5 |
| VMS vs Analytics | 28 |
| Analytics vs Semantic Search | 43 |
| VMS vs Semantic Search | 24 |

Conflicting names include `.env.example`, `.gitignore`, `README.md`, `docker-compose.yml`, `Dockerfile`, `app/`, `tests/`, and `requirements/`.

### Compared approaches

| Approach | Finding | Decision |
|---|---|---|
| Merge branches, then relocate | Creates widespread add/add conflicts at unrelated application roots and makes conflict resolution part of the import | Reject |
| `git subtree` / history rewrite | Preserves more per-file ancestry but adds complexity and no runtime benefit; branches already remain available | Reject |
| Copy source working directories | Would import untracked `.env`, venvs, caches, local eval assets, and uncommitted plans/handoffs | Reject |
| `git archive` to staging, then copy | Safe, but creates an unnecessary staging/copy step | Acceptable fallback only |
| `git read-tree --prefix` at exact SHA | Imports exact committed blob IDs and modes under a collision-free prefix with no merge parents or working-copy residue | **Chosen** |

### Frozen import commands

Run only from a clean index on `refactor/unified-submission`:

```bash
git read-tree --prefix=services/rtsp-simulator/ -u a6e02da3e029c742a4d5c2fe92e1fe1de85011f7
git read-tree --prefix=services/vms/ -u 21feefd6e65bf7f8f4ac246126863aa7a3dc7bbf
git read-tree --prefix=services/video-analytics/ -u 786062c64fd51c20bafb02c04031ad8558c925de
git read-tree --prefix=services/semantic-search/ -u 6df06855be8c29d1f87d1509fb26a9222d7de6fb
```

Execute one command per phase, validate, and commit before executing the next. Never run all four before the earlier service passes its gate.

## 8. Current Compose topology

| Stack | Services | Build context | Network | `extra_hosts` | Healthcheck | Security/runtime notes |
|---|---|---|---|---|---|---|
| Simulator | `mediamtx`, `simulator` | `./simulator`; MediaMTX image pinned `1.20.1` | implicit project `default` | none | none | fixed container names; simulator stop grace 20s |
| VMS | `vms-mediamtx`, `vms` | `.`; MediaMTX image pinned `1.20.1` | implicit project `default` | VMS maps `host.docker.internal:host-gateway` | none | fixed container names; Control API 9997 not published |
| Analytics | `analytics` | `.`, target `final` | implicit project `default` | `host.docker.internal:host-gateway` | `/health`, 10s interval, 60s start period | init, non-root image, `cap_drop: ALL`, no-new-privileges |
| Semantic Search | `semantic-search` | `.`, target `final` | implicit project `default` | `host.docker.internal:host-gateway` | `/health`, 10s interval, 120s start period | init, non-root image, `cap_drop: ALL`, no-new-privileges |

Test topology:

- Analytics `docker-compose.test.yml` uses the same explicit project `genea-analytics`, adds private `mediamtx`, `publisher`, and `tests` services on `analytics-test`, and keeps production `analytics` behind a profile.
- Semantic Search `docker-compose.test.yml` uses the same explicit project `genea-semantic-search`, adds only a `tests` service and a test data volume; its fake Component 4 is in-process.
- Simulator integration tests run in the already-running simulator container.
- VMS integration tests invoke host Docker directly; VMS browser E2E invokes host Playwright against running Simulator and VMS stacks.

**VERIFIED FACT:** All four production Compose files parse from their extracted branch snapshots when Analytics is supplied its documented `.env.example`. Analytics alone fails interpolation without `CURSOR_SIGNING_KEY`; that is an explicit startup prerequisite, not a relocation failure.

**VERIFIED FACT:** Current source-clone project names collide: both Simulator and VMS resolve to `genea_vms`. This makes cross-stack orphan handling unsafe even before unification.

## 9. Frozen final Compose identity and volume strategy

| Stack | Explicit project name | Logical volume | Explicit physical volume |
|---|---|---|---|
| Simulator | `genea-simulator` | `simulator-data` | `genea-simulator-data` |
| VMS | `genea-vms` | `vms-data` | `genea-vms-data` |
| VMS | `genea-vms` | `vms-recordings` | `genea-vms-recordings` |
| Analytics | `genea-analytics` | `analytics-data` | `genea-analytics-data` |
| Analytics tests | `genea-analytics` | `analytics-test-data` | `genea-analytics-test-data` |
| Semantic Search | `genea-semantic-search` | `semantic-search-data` | `genea-semantic-search-data` |
| Semantic tests | `genea-semantic-search` | `semantic-test-data` | `genea-semantic-search-test-data` |

Implement with top-level Compose `name:` and volume `name:` keys. Keep logical mount keys and container mount points unchanged. Do not add `external: true`.

**CODEX IMPLEMENTATION DECISION:** Do not migrate or reuse the possible legacy developer volumes (`genea_vms_simulator-data`, `genea_vms_vms-data`, `genea_vms_vms-recordings`, `genea-analytics_analytics-data`, `genea-semantic-search_semantic-search-data`). Their current existence/content could not be inspected with Docker stopped; the handoffs indicate they may hold acceptance state. They are not submission source and must remain untouched. The unified branch starts with new, explicit volumes. Normal `down`/`up` must preserve them; only a service-scoped `down -v` may remove that service's named volumes.

**RUNTIME VALIDATION REQUIRED:** Prove with Docker labels and destructive tests on disposable unified volumes that `down --remove-orphans` and `down -v` for one project do not affect peers. Do not infer this from names alone.

## 10. Ports and browser endpoints

| Owner | Capability | Container port | Default host port | Public URL |
|---|---|---:|---:|---|
| Simulator | UI/API/docs | 8080/tcp | 8080 | `http://localhost:8080`, `/docs` |
| Simulator MediaMTX | RTSP | 8554/tcp | 8554 | `rtsp://localhost:8554/simulator/<stream_path>` |
| VMS | UI/API/docs | 8090/tcp | 8090 | `http://localhost:8090`, `/docs` |
| VMS MediaMTX | redistributed RTSP | 8554/tcp | 8555 | `rtsp://localhost:8555/vms_<camera-id>` |
| VMS MediaMTX | WHEP/WebRTC HTTP | 8889/tcp | 8889 | `http://localhost:8889/<path>/whep` |
| VMS MediaMTX | ICE | 8189/udp | 8189/udp | advertised ICE candidate |
| VMS MediaMTX | playback | 9996/tcp | 9996 | `http://localhost:9996/get?...` |
| VMS MediaMTX | Control API | 9997/tcp | **not published** | Compose network only |
| Analytics | UI/API/docs | 8100/tcp | 8100 | `http://localhost:8100`, `/docs` |
| Semantic Search | UI/API/docs | 8200/tcp | 8200 | `http://localhost:8200`, `/docs` |

No dynamic port allocation, reverse proxy, shared gateway, or root port abstraction is allowed. Service-local environment overrides remain supported to the same extent as today. Root scripts discover actual published HTTP ports with `docker compose port`; they do not parse or source `.env` as shell code.

## 11. Networking contracts to preserve

```text
Simulator FFmpeg --RTSP/TCP--> mediamtx:8554                      (C1 private network)
VMS MediaMTX --RTSP/TCP--> host.docker.internal:8554/simulator/*  (published host boundary)
Browser --WHEP/ICE--> localhost:8889 + localhost:8189/udp          (VMS public boundary)
Analytics --RTSP/TCP--> host.docker.internal:8555/vms_*            (published host boundary)
Analytics --HTTP GET--> host.docker.internal:8090/api/recordings   (VMS public read-only API)
Semantic Search --HTTP GET--> host.docker.internal:8100/api/*      (C4 public read-only API)
```

Preserve the following invariants:

- each stack has only its own default network;
- no cross-stack network attachment or service-name DNS is introduced;
- VMS owns its MediaMTX and its private unauthenticated 9997 Control API;
- Analytics never calls 9997, mounts VMS data, or imports VMS code;
- Semantic Search never contacts VMS/MediaMTX/RTSP, mounts Analytics data, or imports Analytics code;
- the existing `host.docker.internal:host-gateway` mappings on VMS, Analytics, and Semantic Search remain;
- Simulator does not need `extra_hosts` because it only publishes to its local MediaMTX;
- separate SQLite files and private volumes remain mandatory.

## 12. Persistence ownership

| Service | Container paths | Contents | Other services allowed to mount/read? |
|---|---|---|---|
| Simulator | `/data/simulator.db`, `/data/videos`, `/data/tmp`; read-only `/data/local-sources` bind | camera state and uploaded sources | No |
| VMS backend | `/data/vms.db` | camera desired state and recording preference | No |
| VMS MediaMTX | `/recordings` | MP4 segments | No; browser accesses only playback HTTP |
| Analytics | `/data/analytics.db`, `/data/events/.../{crop.jpg,frame.jpg}` | analytics desired state and durable events | No |
| Semantic Search | `/data/semantic.db`, `/data/semantic.lock`, quarantine data | mirrored metadata and vectors | No |

`RECORDING_STORAGE_HOST_PATH` remains the VMS-only optional bind-mount escape hatch. A relative value such as `./recordings` must continue to resolve relative to `services/vms/`; root wrappers must `cd` into that directory before Compose execution.

## 13. Environment and configuration map

### Simulator

- Compose-interpolated/passed: `MEDIAMTX_HOST`, `MEDIAMTX_RTSP_PORT`, `PUBLIC_RTSP_HOST`, `PUBLIC_RTSP_PORT`, `RTSP_PATH_PREFIX`, `SIMULATOR_HTTP_PORT`, `MAX_UPLOAD_BYTES`, `LOG_LEVEL`.
- Container paths hardcoded by Compose: `/data`, `/data/simulator.db`, `/data/videos`, `/data/local-sources`.
- Settings also define `FFMPEG_BINARY`, `FFPROBE_BINARY`, startup/stop/tail/probe timeouts.

### VMS

- Compose host-only: `VMS_HTTP_PORT`, `VMS_RTSP_PORT`, `VMS_WEBRTC_HTTP_PORT`, `VMS_WEBRTC_ICE_UDP_PORT`, `VMS_PLAYBACK_HTTP_PORT`, `RECORDING_STORAGE_HOST_PATH`.
- Passed to VMS/MediaMTX: `MEDIAMTX_API_URL`, `MEDIAMTX_PLAYBACK_URL`, `PUBLIC_WEBRTC_HOST`, derived public ports, `PUBLIC_PLAYBACK_HOST`, `RECORDING_STORAGE_PATH`, `RECORDING_SEGMENT_DURATION`, `RECORDING_RETENTION_HOURS`, health/reconcile intervals, `LOG_LEVEL`.
- Private internal defaults: `http://vms-mediamtx:9997`, `http://vms-mediamtx:9996`.

### Analytics

- Required before Compose interpolation: `CURSOR_SIGNING_KEY` (at least 32 bytes at runtime). The tracked `.env.example` contains an explicit non-secret local-development value and warns it must change outside local use.
- Passed: data/model paths, Torch threads, log level, `VMS_API_BASE_URL`, signing key; host port is Compose-only.
- Build downloads and checksum-verifies YOLO11n from the pinned GitHub URL; runtime has no model download path.

### Semantic Search

- No secret or Hugging Face credential is required or accepted.
- Passed: data/model paths, Torch threads, batch size, poll/overlap/full-reconcile cadence, log level, and `COMPONENT4_API_BASE_URL`; host port is Compose-only.
- Docker build downloads and verifies the seven-file SigLIP snapshot at revision `7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed`; runtime forces Hugging Face/Transformers offline.

### Exact environment ownership

| Service | Compose host-only/interpolation | Passed into application/container | Defined by app/example but not passed by current Compose |
|---|---|---|---|
| Simulator | `SIMULATOR_HTTP_PORT` | `MEDIAMTX_HOST`, `MEDIAMTX_RTSP_PORT`, `PUBLIC_RTSP_HOST`, `PUBLIC_RTSP_PORT`, `RTSP_PATH_PREFIX`, `MAX_UPLOAD_BYTES`, `LOG_LEVEL`; Compose hardcodes `DATA_DIR`, `DATABASE_PATH`, `UPLOAD_VIDEO_DIR`, `SOURCE_VIDEO_DIR` | `FFMPEG_BINARY`, `FFPROBE_BINARY`, `FFMPEG_STARTUP_GRACE_SECONDS`, `FFMPEG_STOP_TIMEOUT_SECONDS`, `FFMPEG_STDERR_TAIL_LINES`, `FFPROBE_TIMEOUT_SECONDS`; example path overrides are ineffective |
| VMS | `VMS_HTTP_PORT`, `VMS_RTSP_PORT`, `VMS_WEBRTC_HTTP_PORT`, `VMS_WEBRTC_ICE_UDP_PORT`, `VMS_PLAYBACK_HTTP_PORT`, `RECORDING_STORAGE_HOST_PATH` | `MEDIAMTX_API_URL`, `MEDIAMTX_PLAYBACK_URL`, `PUBLIC_WEBRTC_HOST`, derived `PUBLIC_WEBRTC_PORT`, `PUBLIC_PLAYBACK_HOST`, derived `PUBLIC_PLAYBACK_PORT`, `RECORDING_STORAGE_PATH`, `RECORDING_SEGMENT_DURATION`, `RECORDING_RETENTION_HOURS`, `CAMERA_HEALTH_POLL_SECONDS`, `RECONCILE_MIN_INTERVAL_SECONDS`, `LOG_LEVEL`; Compose hardcodes `DATABASE_PATH` | `MEDIAMTX_TIMEOUT_SECONDS`, `PUBLIC_WEBRTC_SCHEME`, `PUBLIC_PLAYBACK_SCHEME`; example `DATABASE_PATH` override is ineffective |
| Analytics | `ANALYTICS_HTTP_PORT` | `ANALYTICS_DATA_DIR`, `ANALYTICS_DB_PATH`, `ANALYTICS_EVENT_DIR`, `ANALYTICS_MODEL_PATH`, `ANALYTICS_TORCH_THREADS`, `ANALYTICS_LOG_LEVEL`, `VMS_API_BASE_URL`, required `CURSOR_SIGNING_KEY` | `ANALYTICS_MODEL_SHA256`, `ANALYTICS_JPEG_QUALITY`, `ANALYTICS_STALL_SECONDS`, `ANALYTICS_STOP_TIMEOUT_SECONDS` |
| Semantic Search | `SEMANTIC_HTTP_PORT` | `SEMANTIC_DATA_DIR`, `SEMANTIC_DB_PATH`, `SEMANTIC_MODEL_DIR`, `SEMANTIC_TORCH_THREADS`, `SEMANTIC_INDEX_BATCH_SIZE`, `SEMANTIC_POLL_INTERVAL_SECONDS`, `SEMANTIC_OVERLAP_SECONDS`, `SEMANTIC_FULL_RECONCILE_SECONDS`, `SEMANTIC_LOG_LEVEL`, `COMPONENT4_API_BASE_URL` | `SEMANTIC_MAX_RETRY_SECONDS`, `SEMANTIC_UPLOAD_MAX_BYTES`, `SEMANTIC_UPLOAD_MAX_PIXELS`, `SEMANTIC_UPSTREAM_IMAGE_MAX_BYTES`, `SEMANTIC_SHUTDOWN_TIMEOUT_SECONDS` |

**VERIFIED FACT — pre-existing configuration limitation:** Several variables documented in `.env.example` are not interpolated into Compose and therefore cannot override container values via a service `.env` today. These include Simulator subprocess timeouts/binary names and container path variables; VMS timeout/scheme and `DATABASE_PATH`; Analytics model checksum/JPEG/stall/stop values; and Semantic Search retry/upload/upstream/shutdown bounds. This is not caused by relocation.

**CODEX IMPLEMENTATION DECISION:** Do not expand the configuration surface in this refactor. Preserve defaults and record the mismatch in `HANDOFF_unified_submission.md`. Changing application configuration semantics requires a separate reviewed task.

**CODEX IMPLEMENTATION DECISION:** Do not add a root `.env.example`. No root-owned value is required: each stack reads its service-local `.env`, and root scripts query Compose for published ports. `start-all.sh` must fail preflight with `cp services/video-analytics/.env.example services/video-analytics/.env` when Analytics lacks its required key; it must not create or overwrite `.env` automatically.

## 14. Relocation and build-context impact

| Service | Relocation result | Required path edit |
|---|---|---|
| Simulator | Compose remains beside `mediamtx/`, `sample-media/`, and `simulator/`; build context stays `./simulator` | README quick-start path only |
| VMS | Compose, Dockerfile, `app/`, `tests/`, and `mediamtx/` move together; build context stays `.` | README paths; add `.dockerignore` |
| Analytics | Compose, Dockerfile, `app/`, `tests/`, `scripts/`, and `requirements/` move together; both build contexts stay service-local | README paths/physical volume name only |
| Semantic Search | Same relative layout; production and test build contexts remain `.` | README paths only |

**VERIFIED FACT:** The extracted committed build contexts are small before network-fetched dependencies/models: Simulator 240 KiB, VMS 448 KiB, Analytics 924 KiB, Semantic Search 736 KiB, and the Analytics publisher fixture 12 KiB. Relocation under `services/` does not make any Docker context the monorepo root.

**VERIFIED FACT:** Dockerfile `COPY` statements refer only to files inside their existing contexts. Runtime paths such as `/srv`, `/srv/analytics`, `/srv/semantic`, `/opt/models`, and `/data` are container paths, not developer-machine paths, and must not change.

**VERIFIED FACT:** Tests derive roots from `Path(__file__)` within each service or run with service-local Compose binds. No test refers to a sibling clone. The only developer-style repository wording found in tracked content is C1's `cd Genea_VMS` quick start and historical handoff prose; update operational README commands, not historical validation claims.

Required Docker/Compose edits are exactly:

1. `services/rtsp-simulator/docker-compose.yml`: add `name: genea-simulator`; set `volumes.simulator-data.name: genea-simulator-data`.
2. `services/vms/docker-compose.yml`: add `name: genea-vms`; set explicit names for `vms-data` and `vms-recordings`.
3. `services/vms/.dockerignore`: add `.git`, `.env`, virtualenv/cache/DB/data/editor patterns while retaining `app`, `tests`, `pyproject.toml`, Dockerfile, and MediaMTX config needed by tests/build.
4. `services/video-analytics/docker-compose.yml`: retain `name: genea-analytics`; name the production volume `genea-analytics-data`.
5. `services/video-analytics/docker-compose.test.yml`: retain its project name and name its test volume `genea-analytics-test-data`.
6. `services/semantic-search/docker-compose.yml`: retain `name: genea-semantic-search`; name the production volume `genea-semantic-search-data`.
7. `services/semantic-search/docker-compose.test.yml`: retain its project name and name its test volume `genea-semantic-search-test-data`.

Create `services/vms/.dockerignore` with this exact initial content; add nothing without evidence from a context audit:

```dockerignore
.git
.gitignore
.env
.venv
venv
__pycache__
**/__pycache__
*.py[cod]
.pytest_cache
*.egg-info
.DS_Store
data
recordings
*.db
*.db-wal
*.db-shm
```

Do not change Dockerfile stages, base images, dependency locks, model URLs/checksums, MediaMTX versions/configuration, service names, container names, ports, mount targets, `extra_hosts`, security options, healthchecks, or restart policies.

## 15. Existing test matrix and exact commands

Historical counts below are **HANDOFF EVIDENCE**, not newly executed results. The frozen trees and commands are verified; VMS collection was additionally checked and produced `328 selected / 48 deselected` for its default expression, matching 376 total collected tests. Actual pass/fail results must be recorded during implementation.

### 15.1 Simulator

```bash
cd services/rtsp-simulator
docker compose config --quiet
docker compose build simulator
docker compose run --rm --no-deps simulator pytest -q
docker compose up -d
docker compose exec -T simulator pytest -m integration -v
```

- Historical: 141 default tests and 2 RTSP integration tests passed.
- The integration tier publishes through the real local MediaMTX and reads back with `ffprobe`.
- No committed automated browser runner exists. The handoff records a Puppeteer walkthrough, but its runner is not in the branch. Preserve the static UI tests and perform the manual browser checks in §28.

### 15.2 VMS (Components 2 and 3)

```bash
cd services/vms
docker compose config --quiet
docker compose build vms
docker compose run --rm --no-deps vms pytest -q
```

Create a fresh host test environment rather than relying on the old source clone's `.venv`:

```bash
cd services/vms
python3.12 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -e '.[dev,e2e]'
.venv/bin/python -m pytest -m integration -v -p no:cacheprovider
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -m e2e -v -p no:cacheprovider
```

- Historical: 328 default, 28 integration, and 20 browser E2E tests passed.
- Integration requires host Docker and host FFmpeg. It must not run inside the VMS container because that image intentionally has neither Docker CLI/socket nor FFmpeg.
- Browser E2E requires Simulator and VMS up and `RECORDING_SEGMENT_DURATION=5s` for practical recording timeouts.

**VERIFIED FACT — reproducibility defect:** `tests/test_e2e_live_view.py` imports Playwright, but `pyproject.toml` does not declare it. The old local VMS venv contains Playwright `1.62.0`.

**CODEX IMPLEMENTATION DECISION:** Add a separate `e2e = ["playwright==1.62.0"]` optional dependency and update only the misleading container invocation in the integration test docstring plus README setup commands. Do not install a browser or Docker client in the runtime image and do not mount the Docker socket.

### 15.3 Video Analytics

```bash
cd services/video-analytics
test -f .env || { echo 'copy .env.example to .env first' >&2; exit 1; }
docker compose config --quiet
docker compose build analytics
docker compose -f docker-compose.yml -f docker-compose.test.yml build tests
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm --no-deps tests pytest -m unit -q
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm tests pytest -m 'integration and not real_model and not four_camera' -q
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm tests pytest -m real_model -q -rA
docker compose stop analytics
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm tests pytest -m four_camera -q
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm tests pytest -m e2e -q
docker compose up -d analytics
docker compose exec -T analytics python scripts/verify_event_store.py
```

- Historical: 524 unit, 38 integration, 10 real-model, 2 four-camera, and 26 browser tests passed.
- The real-model tier downloads the checksum-pinned Ultralytics `bus.jpg` test asset at test time when absent. Network must be available and an unexpected skip is a failed gate.
- Run the four-camera tier on an otherwise idle host; the historical handoff records a starvation failure under deliberate host oversubscription.
- `git diff --check main...786062c...` finds one pre-existing “new blank line at EOF” in `tests/unit/conftest.py`. Remove only that blank line; no test logic changes.

### 15.4 Semantic Search

```bash
cd services/semantic-search
docker compose config --quiet
docker compose build semantic-search
docker compose -f docker-compose.test.yml build tests
docker compose -f docker-compose.test.yml run --rm tests pytest -q -m unit
docker compose -f docker-compose.test.yml run --rm tests pytest -q -m integration
docker compose -f docker-compose.test.yml run --rm tests pytest -q -m real_model -rA
docker compose -f docker-compose.test.yml run --rm tests pytest -q -m scale
docker compose -f docker-compose.test.yml run --rm tests pytest -q -m e2e
COMPONENT4_API_BASE_URL=http://host.docker.internal:8100 \
  docker compose -f docker-compose.test.yml run --rm tests pytest -q -m real_component4 -rA
docker run --rm --network none genea-semantic-search-semantic-search:latest \
  /opt/venv/bin/python scripts/validate_runtime.py \
  --platform --imports --torch --model-manifest --offline-inference
docker compose -f docker-compose.test.yml run --rm tests \
  /opt/venv/bin/python scripts/validate_real_component4.py \
  --c5 http://host.docker.internal:8200 --c4 http://host.docker.internal:8100
```

- Historical: 161 unit, 64 integration, 17 real-model, 4 scale, 13 browser, and 10 real-Component-4 tests passed.
- `tests/real_model/test_semantic_quality.py` deliberately skips when the gitignored user-owned `tests/assets/cache/` corpus is absent. This is not permission to claim the semantic-evaluation tier passed in a fresh clone. Record the skip explicitly and run the evaluation only after generating a new corpus with `scripts/capture_eval_set.py`.
- The service model itself is acquired only during image build and must validate with `--network none` at runtime.

### 15.5 Validation coverage matrix

| Validation | Simulator | VMS | Analytics | Semantic Search |
|---|---|---|---|---|
| Compose parses from service directory | required | required | required with documented `.env` | required |
| Starts independently | required | required | required | required (may be upstream-degraded) |
| Unit/API/static tests | default suite | default suite | `-m unit` | `-m unit` |
| Native/integration tests | RTSP integration | real MediaMTX/recording | GStreamer/tracker/private fixtures | fake-C4 + real SQLite |
| Real model | N/A | N/A | `-m real_model` | `-m real_model` + offline runtime |
| Scale/four-camera | multiple-camera manual + integration | four-camera recording integration | `-m four_camera` | `-m scale` |
| Browser | manual UI | Playwright full live/recording | `-m e2e` | `-m e2e` |
| Live upstream tier | source for VMS | consumes C1 | consumes VMS | `-m real_component4` |
| Persistent data isolated | runtime gate | runtime gate | runtime gate | runtime gate |
| Stop/down isolation | runtime gate | runtime gate | runtime gate | runtime gate |

Any unexpected skip, xfail, changed count, weakened assertion, deleted test, or new broad marker exclusion blocks the phase until explained in the unified handoff.

## 16. Exact final repository tree

The final Part 1 tree is shallow at the root. `README.md` remains the one-line main-branch placeholder because the polished reviewer README is explicitly Part 2; service READMEs remain complete operational references.

```text
Genea_VMS/
├── README.md
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
│       └── HANDOFF_unified_submission.md
├── scripts/
│   ├── start-all.sh
│   ├── status.sh
│   ├── stop-all.sh
│   └── smoke-test.sh
└── services/
    ├── rtsp-simulator/       # Appendix A.1 manifest, minus HANDOFF.md
    ├── vms/                  # Appendix A.2 manifest, plus .dockerignore
    ├── video-analytics/      # Appendix A.3 manifest
    └── semantic-search/      # Appendix A.4 manifest
```

There is no root Compose file, root application package, root database/data directory, shared requirements file, shared application library, or root `.env.example`.

## 17. Import, exclusion, and documentation rules

### Imported from commits

- Import every tracked path in each frozen commit into its mapped service directory.
- Move C1's tracked `HANDOFF.md` to `docs/engineering-handoffs/component-1-rtsp-simulator.md`; do not duplicate it.
- Preserve executable modes. C4's three scripts are `100755`; all other source files are `100644`.

### Imported from supplied untracked documents

- Copy C2–C5 handoffs from the exact planning-time paths in §3 after verifying their SHA-256 values.
- Copy the PRD to `docs/engineering/requirements/PRD_unified_submission_repository_for_codex.md` after verifying its hash.
- Move this plan to `docs/engineering/plans/PLAN_unified_submission.md` on the implementation branch.

### Deliberately excluded

- all `.git` directories and worktree metadata;
- all local `.env` files and credentials;
- source-clone `.venv`, egg-info, Python/pytest caches, and editor files;
- C5 `tests/assets/cache/` and all downloaded/captured images;
- Docker containers, images, networks, volumes, SQLite DBs, uploads, recordings, events, semantic indexes, logs, and model caches;
- untracked `plan.md`, `PLAN_component_3.md`, and `PLAN_component_4.md` because they are neither frozen source commits nor supplied authorities for this task;
- the pasted request file because it is session instruction, not repository documentation;
- an independent `feat/vms` import because C3 is proven to contain C2;
- any feature-branch README at repository root; each remains service-local under its prefix.

### Handoff edits allowed

Prepend a short notice to each component handoff: it records historical validation in the original branch/clone and is not proof of unified-repository validation. Update only links that would otherwise break:

- service README links become `../../services/<service>/README.md`;
- C3→C2 and C5→C4 handoff links point to sibling handoff filenames;
- references to unimported component plans remain plain historical names, not broken links.

Do not rewrite historical commands/results to imply they ran in the unified tree. New results belong only in `HANDOFF_unified_submission.md`.

## 18. Root `.gitignore` and hygiene

Create a root ignore file covering, at minimum:

```gitignore
.DS_Store
.env
.env.*
!.env.example
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
.venv/
venv/
node_modules/
playwright-report/
test-results/
data/
recordings/
models/
tests/assets/cache/
*.db
*.db-wal
*.db-shm
*.log
```

Keep service-local `.gitignore` and `.dockerignore` files because each service remains independently usable. Do not ignore lock files, manifests, bundled sample media, intentional test fixtures, or engineering handoffs.

Repository scans before every service commit and at the final gate:

```bash
git diff --check
git status --short
git ls-files
git grep -nI -E '/Users/|/home/[^/]+/|[A-Za-z]:\\\\Users\\\\' -- \
  ':!docs/engineering/plans/PLAN_unified_submission.md' \
  ':!docs/engineering/requirements/PRD_unified_submission_repository_for_codex.md'
git grep -nI -E 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,}' -- .
git grep -nI -E '(HF_TOKEN|HUGGING_FACE_HUB_TOKEN|API_KEY|PASSWORD|SECRET)[[:space:]]*=' -- .
git ls-files | grep -E '(^|/)(\.env|\.DS_Store|__pycache__|\.pytest_cache|\.venv|data|recordings|playwright-report|test-results)(/|$)|\.(db|db-wal|db-shm|pyc|log)$'
```

The two engineering documents are allowlisted only because they must record the inspected local paths. No executable, configuration, README quick start, test, or script may contain or depend on a developer absolute path. Review every secret-keyword hit; documented placeholders and hostile test credentials are not secrets, but the scan must not simply be ignored.

## 19. Root orchestration design

All root scripts must be Bash, executable, use `set -Eeuo pipefail`, derive the repository root from their own file location, quote every path, and execute Compose in a subshell after `cd` into the service directory. They must not source `.env` as shell code, use absolute developer paths, invoke a root Compose file, prune Docker globally, or delete volumes.

### 19.1 `scripts/start-all.sh`

Preflight before starting anything:

1. Require `docker`, `docker compose`, and `curl`.
2. Require the four service directories and Compose files.
3. Run `(cd "$service" && docker compose config --quiet)` for all four stacks. This makes a missing Analytics signing key fail before partial startup.
4. Read each project name from `docker compose config --format json` with standard `awk`; require exactly `genea-simulator`, `genea-vms`, `genea-analytics`, and `genea-semantic-search`, all unique.
5. Do not delete or stop conflicting containers. If Compose reports a port/container collision, name it and exit.

Start in this order:

```text
rtsp-simulator → vms → video-analytics → semantic-search
```

For each stack:

1. `(cd "$service" && docker compose up -d --build)`.
2. Discover the bound HTTP port with `docker compose port <service> <container-port>`.
3. Poll the service's `/health` using `curl --fail --silent --show-error`, with 60-second application-readiness budgets for Simulator and VMS and 180 seconds for Analytics and Semantic Search. Build duration occurs before the poll and is not part of the readiness budget.
4. On success, print the HTTP/API URL and relevant media ports obtained through `docker compose port`.

Readiness means:

| Stack | Required signal | Not required for startup success |
|---|---|---|
| Simulator | `/health` HTTP 200 (DB + FFmpeg + ffprobe ready) | a camera exists or is publishing |
| VMS | `/health` HTTP 200 (DB + MediaMTX Control API reachable) | any registered camera is online |
| Analytics | `/health` HTTP 200 (DB, event store, detector ready) | any analytics camera or VMS event exists |
| Semantic Search | `/health` HTTP 200 (`search: ok`) | upstream C4 is available; `status: degraded` is acceptable and printed distinctly |

**CODEX IMPLEMENTATION DECISION:** Do not automatically roll back earlier stacks when a later start/readiness step fails. Automatic rollback could stop a stack that was already running before the script. Print the failed stack, leave successful stacks available for diagnosis, and direct the operator to `./scripts/stop-all.sh`.

The script must be idempotent: a second run may rebuild/reconcile but must not wipe state or create duplicate projects.

### 19.2 `scripts/status.sh`

For each stack, in dependency order:

- print the explicit project/service heading;
- run service-local `docker compose ps`;
- discover its bound HTTP port when running;
- request `/health`, print the HTTP code and response body;
- distinguish `running`, `application ready`, `degraded upstream`, and `unavailable` rather than converting all states to one boolean;
- print UI/docs/media URLs where a published port exists.

Status is read-only and continues through unavailable stacks. It exits nonzero only when one or more applications cannot be queried; an upstream-degraded but locally searchable Semantic Search is reported as degraded and does not become “down.”

### 19.3 `scripts/stop-all.sh`

Stop in reverse order:

```text
semantic-search → video-analytics → vms → rtsp-simulator
```

Use service-local `docker compose down` without `-v` and without global prune. Continue after a failure so upstream stacks still get a stop attempt, then exit nonzero with a summary of failures. It must be safe to rerun.

### 19.4 Reset decision

**CODEX IMPLEMENTATION DECISION:** Do not add `reset-all.sh`. Explicit physical volumes make a safe reset possible, but a global destructive helper is not needed for submission operation. The service-local, explicit `docker compose down -v` commands are clearer and less likely to erase wanted data.

## 20. Root smoke validation

Add `scripts/smoke-test.sh` as a lightweight coexistence check, not as a replacement for component suites or manual media E2E. It must:

1. repeat the four Compose parse/project-name/uniqueness assertions;
2. require every expected Compose service to be running;
3. require HTTP 200 from all four `/health` endpoints;
4. require response bodies to contain each service's documented local readiness fields;
5. print Semantic Search upstream degradation without failing local-search readiness;
6. print a clear message that media flow, browser playback, event creation, and ranking still require §28.

Run:

```bash
bash -n scripts/*.sh
./scripts/start-all.sh
./scripts/status.sh
./scripts/smoke-test.sh
./scripts/start-all.sh        # idempotency check
```

No root script may modify application state through HTTP. Full integration uses explicit acceptance commands so created camera/event state and cleanup are auditable.

## 21. Ordered implementation phases

Every phase starts with `git status --short` and requires a clean index/worktree except for the known files of that phase. Every phase ends with a commit only after its gate passes. “Forbidden” applies even when a cleanup seems attractive.

### Phase 0 — Reconfirm investigation baseline

**Purpose:** Detect branch movement or local contamination before changing anything.

**Exact commands:**

```bash
git status --short --branch
git rev-parse HEAD
git ls-remote --heads origin main feat/rtsp_simulation feat/vms feat/vms-recording feat/video_analytics feat/image_retrival refactor/unified-submission
git merge-base --is-ancestor e5d4fea46429cef1900d99bf3a0bb80cf19774ad 21feefd6e65bf7f8f4ac246126863aa7a3dc7bbf
git diff --check main...a6e02da3e029c742a4d5c2fe92e1fe1de85011f7
git diff --check main...21feefd6e65bf7f8f4ac246126863aa7a3dc7bbf
git diff --check main...786062c64fd51c20bafb02c04031ad8558c925de || true
git diff --check main...6df06855be8c29d1f87d1509fb26a9222d7de6fb
```

**Expected:** main and source SHAs match §§4–6; target branch is absent; only C4 reports the recorded blank EOF line.

**Failure:** Any source SHA moved, tracked local file is modified, target exists, C3 no longer contains C2, or a new whitespace finding appears. Stop; update evidence and obtain plan review.

**Changes allowed:** None.
**Rollback:** Not applicable.
**Success gate:** Baseline exactly matches this plan.

### Phase 1 — Create the unified branch and engineering skeleton

**Purpose:** Isolate all implementation from `main` and establish only root-owned structure.

**Commands and files:**

```bash
git switch -c refactor/unified-submission bdd4b72987da6aa0c44b73934646793eea9748bb
mkdir -p docs/engineering/plans docs/engineering/requirements docs/engineering-handoffs scripts services
git mv PLAN_unified_submission.md docs/engineering/plans/PLAN_unified_submission.md
cp /Users/atharvapawar/Downloads/PRD_unified_submission_repository_for_codex.md \
  docs/engineering/requirements/PRD_unified_submission_repository_for_codex.md
shasum -a 256 docs/engineering/requirements/PRD_unified_submission_repository_for_codex.md
```

Create root `.gitignore` exactly as §18. Do not add `.gitkeep`; Git need not track empty service/script/handoff directories.

**Allowed:** root `.gitignore`, archived PRD, moved plan.
**Forbidden:** service files, root Compose, root README expansion, application edits.
**Tests:** `git diff --check`; verify PRD hash; `git status --short`; re-read plan from its new path.
**Failure:** PRD hash mismatch, plan lost, or any application file appears.
**Rollback:** Before commit, switch back to `main` only after moving the untracked plan safely; after commit, `git revert` the phase commit.
**Commit:** `chore: scaffold unified submission layout`.

### Phase 2 — Import and validate Component 1

**Purpose:** Relocate the exact Simulator snapshot first.

**Source:** `a6e02da3e029c742a4d5c2fe92e1fe1de85011f7`.

**Commands:**

```bash
git read-tree --prefix=services/rtsp-simulator/ -u a6e02da3e029c742a4d5c2fe92e1fe1de85011f7
```

Modify only:

- `services/rtsp-simulator/docker-compose.yml` for §9 identity/volume names;
- `services/rtsp-simulator/README.md` for monorepo `cd services/rtsp-simulator` commands.

Leave its `HANDOFF.md` in place until Phase 7 so the raw import is easy to compare.

**Forbidden:** simulator Python/frontend/test/MediaMTX/Dockerfile changes; port/network changes.

**Tests:** exact blob/mode comparison before edits; Compose config; default test suite; live stack; RTSP integration; `curl /health`; create a camera from bundled sample; `ffprobe` its RTSP URL; stop/start path check; `git diff --check`.

**Expected:** default and integration suites pass with unchanged counts; H.264 sample is readable at `:8554`; normal down/up preserves camera state.

**Failure:** any test skip/failure, build context escapes service directory, path bind breaks, project/volume name differs, or RTSP changes. Stop.

**Rollback:** Before commit, unstage the exact prefix and move it to a temporary diagnostic directory; after commit, revert only this phase commit.

**Success gate:** The exact C1 snapshot plus only the two allowed relocation edits passes every C1 test and live RTSP check before commit.

**Commit:** `refactor: import rtsp simulator service`.

### Phase 3 — Import and validate final VMS (Components 2 + 3)

**Purpose:** Add one VMS containing live view, recording, and playback.

**Source:** `21feefd6e65bf7f8f4ac246126863aa7a3dc7bbf`. Do not import `feat/vms` separately.

**Command:**

```bash
git read-tree --prefix=services/vms/ -u 21feefd6e65bf7f8f4ac246126863aa7a3dc7bbf
```

Modify only:

- `services/vms/docker-compose.yml` for §9;
- new `services/vms/.dockerignore`;
- `services/vms/pyproject.toml` for Playwright `1.62.0` in an `e2e` extra;
- `services/vms/tests/test_integration_mediamtx.py` docstring to remove the nonfunctional in-container Docker claim;
- `services/vms/README.md` for monorepo paths, fresh E2E setup, and explicit volume names where referenced.

**Forbidden:** API/schema/recording/MediaMTX/frontend/test assertion changes; Docker socket mount; FFmpeg/GStreamer in VMS.

**Tests:** exact pre-edit tree check; default 328; host integration 28; Playwright E2E 20 against C1+VMS; browser live pixels; recording toggle/history/MP4; VMS restart/persistence; C1 remains healthy; Compose name and volumes; `git diff --check`.

**Expected:** same IDs/paths across restarts; VMS `ONLINE`; recording progresses to listable history; live player remains live when recording toggles.

**Failure:** any C2/C3 regression, skipped tier, Control API published, VMS reads simulator internals, C1 lifecycle affected, or playback URL changes.

**Rollback:** Revert only the VMS phase commit; C1 remains intact.

**Success gate:** Default, integration, browser, live, recording, persistence, and isolation checks all pass with no unexpected skip before commit.

**Commit:** `refactor: import vms live and recording service`.

### Phase 4 — Import and validate Video Analytics

**Purpose:** Add C4 without coupling it to VMS internals.

**Source:** `786062c64fd51c20bafb02c04031ad8558c925de`.

**Command:**

```bash
git read-tree --prefix=services/video-analytics/ -u 786062c64fd51c20bafb02c04031ad8558c925de
```

Modify only the production/test Compose volume names, README monorepo/backup paths, and the final blank line in `tests/unit/conftest.py`.

**Forbidden:** application logic, detector/tracker/crossing/event behavior, lock files, models, test assertions, VMS network/volume attachment.

**Tests:** all five C4 tiers in §15.3; runtime validator; event-store verifier; configure a real VMS RTSP path; decoded frames and inference; stop/start one source; stop Analytics and prove VMS live/recording remains healthy; static boundary scan; `git diff --check`.

**Expected:** all tiers pass without unexpected skips; four-camera gate passes on idle host; application image remains one Uvicorn worker; VMS state/endpoints are unchanged when Analytics stops.

**Failure:** C4 needs a VMS private network/volume/code import, the model checksum changes, event semantics change, target host is too loaded for the frozen gate, or VMS health changes due to C4 lifecycle.

**Rollback:** Revert only C4 commit; C1 and VMS remain validated.

**Success gate:** All C4 tiers and C1→VMS→C4 runtime/failure-isolation checks pass before commit.

**Commit:** `refactor: import video analytics service`.

### Phase 5 — Import and validate Semantic Search

**Purpose:** Add C5 as a C4-public-HTTP-only consumer.

**Source:** `6df06855be8c29d1f87d1509fb26a9222d7de6fb`.

**Command:**

```bash
git read-tree --prefix=services/semantic-search/ -u 6df06855be8c29d1f87d1509fb26a9222d7de6fb
```

Modify only production/test Compose volume names and README monorepo paths.

**Forbidden:** model manifest/revision, preprocessing, vector schema, ranking, API, application/test logic, C4 volume/code access, runtime model download.

**Tests:** all C5 tiers in §15.4; offline runtime validation; real-C4 contract/pipeline; restart idempotency; C4 outage with local search continuity; C5 stop with C1–C4 healthy; static boundary scan; `git diff --check`.

**Expected:** locally ready HTTP 200 even if upstream is degraded; exact event-level max-score behavior remains; restart does not re-embed unchanged vectors; C4 recovery is automatic.

**Failure:** any model identity change, runtime download, private C4 access, local search failure during C4 outage, or upstream lifecycle effect from C5.

**Rollback:** Revert only C5 commit.

**Success gate:** All mandatory C5 tiers, real-C4 boundary checks, offline-model check, outage continuity, and stop isolation pass before commit.

**Commit:** `refactor: import semantic search service`.

### Phase 6 — Add root lifecycle and smoke scripts

**Purpose:** Add optional full-stack convenience without a fifth orchestration architecture.

**Source:** New root integration code defined by §§19–20; no feature-branch source.

**Files:** `scripts/start-all.sh`, `scripts/status.sh`, `scripts/stop-all.sh`, `scripts/smoke-test.sh` only.

**Tests:** `bash -n`; shell-format review; run start/status/smoke/start-again/stop; invoke each service-local Compose command independently afterward; simulate a downstream readiness failure; confirm no volume removal and no automatic rollback.

**Forbidden:** root Compose, shared network, application HTTP mutation, implicit `.env` creation, volume deletion, global prune.

**Failure:** scripts only work from repository root, source `.env` as code, hide degraded state, stop unrelated projects, or make local Compose unusable.

**Rollback:** Revert this script-only commit; all service-local operation remains.

**Success gate:** Syntax, preflight, readiness, idempotency, failure reporting, reverse stop, and post-wrapper service-local operation all pass.

**Commit:** `chore: add unified stack lifecycle scripts`.

### Phase 7 — Preserve handoffs and finish repository hygiene

**Purpose:** Make provenance auditable without importing source-clone residue.

**Source:** The five handoff paths and checksums in §3; no untracked component plan is a source.

**Commands:**

```bash
shasum -a 256 \
  services/rtsp-simulator/HANDOFF.md \
  /Users/atharvapawar/workspace/genea_assignment/VMS/Genea_VMS/HANDOFF_component_2.md \
  /Users/atharvapawar/workspace/genea_assignment/VMS/Genea_VMS/HANDOFF_component_3.md \
  /Users/atharvapawar/workspace/genea_assignment/ai-services/Genea_VMS/HANDOFF_component_4.md \
  /Users/atharvapawar/workspace/genea_assignment/image_retrival/Genea_VMS/HANDOFF_component_5.md

git mv services/rtsp-simulator/HANDOFF.md \
  docs/engineering-handoffs/component-1-rtsp-simulator.md
cp /Users/atharvapawar/workspace/genea_assignment/VMS/Genea_VMS/HANDOFF_component_2.md \
  docs/engineering-handoffs/component-2-vms-live-view.md
cp /Users/atharvapawar/workspace/genea_assignment/VMS/Genea_VMS/HANDOFF_component_3.md \
  docs/engineering-handoffs/component-3-recording-playback.md
cp /Users/atharvapawar/workspace/genea_assignment/ai-services/Genea_VMS/HANDOFF_component_4.md \
  docs/engineering-handoffs/component-4-video-analytics.md
cp /Users/atharvapawar/workspace/genea_assignment/image_retrival/Genea_VMS/HANDOFF_component_5.md \
  docs/engineering-handoffs/component-5-semantic-search.md
```

The pre-edit hashes must match §3. Add historical-validation notices and repair only links specified in §17. Run all scans in §18. The C4 README physical-volume correction belongs to Phase 4 and must already be present.

**Forbidden:** importing untracked component plans, `.env`, caches, local assets, data, screenshots, or modifying historical validation results.

**Tests:** Markdown link/path checks; `git diff --check`; `git status --short`; `git ls-files`; secret/path/generated-artifact scans; Compose context sizes remain service-scoped.

**Failure:** checksum mismatch before intended link/banner edits, misleading history, broken links, secret-looking unreviewed content, or generated artifacts tracked.

**Rollback:** Revert the docs/hygiene commit only.

**Success gate:** All five hashes matched before permitted edits, every final link resolves, all hygiene scans pass, and no excluded artifact is tracked.

**Commit:** `docs: preserve component engineering handoffs`.

### Phase 8 — Unified smoke, full E2E, persistence, and failure isolation

**Purpose:** Validate the complete relocated chain from a known state.

**Source:** The unified service commits from Phases 2–5 and validation definitions in §§26–28.

**Files allowed:** A draft `docs/engineering-handoffs/HANDOFF_unified_submission.md` and, only if a root-smoke defect is found, `scripts/smoke-test.sh`. Runtime data stays in Docker volumes or `/private/tmp`.

Run §27 and §28 exactly. Record commands, timestamps, commit SHA, test counts, skips, JSON evidence, MediaMTX paths, volume names, and failures in a draft `docs/engineering-handoffs/HANDOFF_unified_submission.md`.

**Forbidden:** changing application behavior to make E2E easier; reusing undocumented old camera IDs/events/volumes.

**Success gate:** The phase cannot pass until the §34 input is resolved and all automated tiers, media flow, recording/playback, real analytics event flow, C5 indexing/search, persistence, and isolation checks pass.

**Expected:** The four relocated services behave like their source implementations and coexist without shared runtime ownership.

**Failure:** Any failed/hidden/skipped mandatory tier, contract change, persistence loss, cross-stack lifecycle effect, or inability to generate the required real event blocks the phase.

**Rollback:** Revert only a smoke-script correction if it caused regression; do not roll back service imports to conceal a runtime failure. Preserve evidence and return to the last validated commit.

**Commit:** `test: add unified repository smoke validation` only if smoke-script tests changed; do not commit runtime data. The handoff is not final until Phase 10.

### Phase 9 — Fresh-clone acceptance

**Purpose:** Prove the repository is self-contained and residue-independent.

**Source:** The committed unified branch only.

**Files allowed:** No tracked file changes are expected. Create only ignored `.env`/`.venv` and Docker-owned runtime state inside the clean clone.

Run §29 in a new directory after targeted cleanup of only the new unified projects/volumes. Do not use a sibling source clone. Rebuild without relying on old images where practical, recreate state through public APIs, and repeat the E2E subset.

**Tests/commands:** Every command and proof obligation in §29, followed by the relevant §27–28 checks.

**Forbidden:** Copying any file/state from source clones, reusing old unified volumes, or editing the clean checkout to make it pass.

**Failure:** any external source-tree path, old volume/container, pre-existing venv, hidden environment variable, runtime model credential/download, or developer DB/media/index is required.

**Expected:** A pristine checkout produces the same service/test/runtime results using only documented setup.

**Rollback:** Tear down only the clean-room unified projects; retain logs and return to the prior unified commit. Any code fix becomes a separate reviewed commit followed by re-running affected phases.

**Success gate:** Clean checkout remains Git-clean apart from documented ignored local files, all mandatory checks pass, and no sibling/residue dependency is observed.

**Commit:** none unless a minimal relocation defect is found, fixed in its own reviewed commit, and all affected earlier phase gates are rerun.

### Phase 10 — Final handoff and merge gate

**Purpose:** Freeze truthful validation evidence and obtain review before merging.

**Source:** Actual Phase 0–9 command output only.

**Files:** `docs/engineering-handoffs/HANDOFF_unified_submission.md` only, unless a factual broken documentation link is separately reviewed.

Finalize `docs/engineering-handoffs/HANDOFF_unified_submission.md` with actual evidence only. Run §30. Commit:

```text
docs: record unified submission validation
```

**Allowed:** Evidence, exact results, known limitations, and approved deviations.
**Forbidden:** Claiming an unrun validation, application changes, history rewrites, direct unreviewed merge.
**Tests:** Full §30 checklist, `git diff --check`, secret/path scans, `git status --short`, and review of final tree against Appendix A.
**Expected:** Clean branch, complete handoff, all gates green.
**Failure:** Any unresolved failure condition in the PRD or unchecked §30 item.
**Rollback:** Revert the handoff commit if inaccurate; do not merge.
**Success gate:** User reviews the branch and explicitly authorizes merge.

Do not merge. Push `refactor/unified-submission`, have the user review it, and merge to `main` only after explicit approval and every gate is green.

## 22. Exact source-snapshot verification

Immediately after each `read-tree` and before intentional edits, compare mode/blob/path triples. Use a new temporary directory created with `mktemp -d`; do not write manifests into the repository.

```bash
SOURCE_SHA=a6e02da3e029c742a4d5c2fe92e1fe1de85011f7
TARGET_PREFIX=services/rtsp-simulator
CHECK_DIR=$(mktemp -d /private/tmp/genea-tree-check.XXXXXX)

git ls-tree -r "$SOURCE_SHA" \
  | while read -r mode type oid path; do printf '%s %s %s\n' "$mode" "$oid" "$path"; done \
  | sort > "$CHECK_DIR/source"
git ls-files -s "$TARGET_PREFIX" \
  | while read -r mode oid stage path; do
      path=${path#"$TARGET_PREFIX"/}
      printf '%s %s %s\n' "$mode" "$oid" "$path"
    done \
  | sort > "$CHECK_DIR/target"
diff -u "$CHECK_DIR/source" "$CHECK_DIR/target"
```

Repeat with each SHA/prefix. Expected output is empty. After edits, `git diff -- <service>` must contain only the enumerated files for that phase.

## 23. Service-isolation invariants

These are merge-blocking invariants, not aspirations:

- Four production Compose projects and no fifth root project.
- C2+C3 are one VMS stack; there is no `services/vms-live` or duplicate C2 import.
- Each service can build, test, start, stop, and remove its own volumes from its directory.
- Each production service owns a different explicitly named data volume.
- No application database, `/data`, recordings directory, or event image directory is mounted by another application.
- No Python import resolves outside its service build context.
- No service depends on a root virtualenv or shared requirements file.
- No MediaMTX instance is shared between Simulator and VMS.
- No `docker compose down`, `down --remove-orphans`, or `down -v` affects a peer project.
- Simulator failure can make the registered VMS camera offline but cannot terminate VMS/C4/C5 containers.
- VMS failure cannot corrupt or terminate Analytics/C5 local state.
- Analytics failure cannot affect VMS live/recording.
- C5 failure cannot affect C1–C4; C4 failure leaves C5 local search functional.
- Public APIs, response shapes, model identities, ports, and media semantics remain unchanged.

## 24. Shutdown and failure handling

- Normal root shutdown uses Compose `down` in reverse dependency order and preserves named volumes.
- Simulator retains its 20-second grace for managed FFmpeg children.
- Analytics and Semantic Search retain 30-second Compose grace and their existing bounded application shutdown.
- VMS backend shutdown must continue to leave MediaMTX-derived state for next-start reconciliation; no recording files are deleted.
- If one `down` fails, root shutdown continues to peers and reports all failures.
- If one service fails readiness, root start leaves already-running stacks available for diagnosis and exits nonzero.
- A degraded upstream is not silently called healthy: `status.sh` prints local health and upstream state separately.
- No script retries forever. Readiness loops have the timeouts in §19.
- No cleanup command may use `docker system prune`, broad volume prune, an unresolved variable, a wildcard target, or a repository-root recursive deletion.

## 25. Exact static isolation checks

Run from repository root:

```bash
# No root application package or master Compose.
test ! -e docker-compose.yml
test ! -e compose.yml
test ! -d app

# Cross-private-volume mounts must not appear in peer Compose files.
! git grep -nE 'vms-data|vms-recordings|analytics-data|semantic-search-data' -- \
  'services/rtsp-simulator/docker-compose*.yml'
! git grep -nE 'simulator-data|analytics-data|semantic-search-data' -- \
  'services/vms/docker-compose*.yml'
! git grep -nE 'simulator-data|vms-data|vms-recordings|semantic-search-data' -- \
  'services/video-analytics/docker-compose*.yml'
! git grep -nE 'simulator-data|vms-data|vms-recordings|analytics-data' -- \
  'services/semantic-search/docker-compose*.yml'

# Executable C5 boundary scan used by the component tests remains authoritative.
git grep -nI -E 'docker\.sock|/recordings|:9997|rtsp://|rtsps://' -- services/semantic-search/app || true

# Build contexts must all normalize beneath their service directory.
(cd services/rtsp-simulator && docker compose config --format json)
(cd services/vms && docker compose config --format json)
(cd services/video-analytics && docker compose config --format json)
(cd services/semantic-search && docker compose config --format json)
```

The illustrative cross-volume grep is not a substitute for review; parse the normalized Compose output and inspect every mount source/target. Test fixtures may mention hostile/forbidden values as assertions. Executable-code scans must distinguish test/security prose from actual integrations.

## 26. Compose project and volume isolation procedure

Use only disposable unified volumes. First verify no pre-existing volume with a final name contains wanted data; if one exists, stop and obtain backup/cleanup approval.

1. Start all four stacks and record:

```bash
docker compose ls --all
docker ps -a --filter label=com.docker.compose.project \
  --format '{{.Names}} {{.Label "com.docker.compose.project"}} {{.Label "com.docker.compose.service"}}'
docker volume inspect genea-simulator-data genea-vms-data genea-vms-recordings \
  genea-analytics-data genea-semantic-search-data
```

2. Create one durable marker through each application's public API/data flow; for VMS, ensure a recording segment exists.
3. From `services/semantic-search`, run `docker compose down --remove-orphans`. Assert C1–C4 containers and volumes still exist and respond.
4. Bring C5 back. From `services/video-analytics`, run `docker compose down -v`. Assert only `genea-analytics-data` is removed; C1, VMS, C5 and their volumes remain. Recreate Analytics and confirm a clean local DB without changes upstream.
5. Repeat `down --remove-orphans` once for each other stack while peers are running.
6. Repeat `down -v` on disposable volumes for Simulator, VMS, and C5 one at a time, checking peer volume IDs before/after. VMS `down -v` may delete both VMS-owned volumes and nothing else.
7. End with `./scripts/stop-all.sh`; confirm the named volumes remain.

**Failure:** Any peer container stops, peer volume ID disappears, or peer endpoint changes because of another project's Compose lifecycle.

## 27. Unified automated validation sequence

Run in this order so expensive tests do not hide cheap structural failures:

```bash
git diff --check
bash -n scripts/*.sh

(cd services/rtsp-simulator && docker compose config --quiet)
(cd services/vms && docker compose config --quiet)
(cd services/video-analytics && docker compose config --quiet)
(cd services/semantic-search && docker compose config --quiet)

# Component-local tests: execute every command in §15.

./scripts/start-all.sh
./scripts/status.sh
./scripts/smoke-test.sh
```

Record exact command lines, elapsed times, pass/fail/skip counts, image IDs, `docker compose version`, host architecture, and the tested Git SHA. Do not summarize a tier as passed when it skipped required cases.

## 28. Full-stack E2E and manual acceptance

### 28.1 Prerequisites

- Docker Engine/Desktop with Compose v2, `curl`, `jq`, `ffprobe`, a browser, and Python 3.12 for VMS host tests.
- `services/video-analytics/.env` created explicitly from its example. The local example key is acceptable only for this local acceptance.
- Start from new unified volumes. Set `RECORDING_SEGMENT_DURATION=5s` for the VMS acceptance run.
- A legal H.264 clip containing a person or supported vehicle moving across a drawable line is required for real event generation. The bundled color bars prove media transport but intentionally produce no YOLO event.

### 28.2 Create the C1→VMS chain

```bash
RECORDING_SEGMENT_DURATION=5s ./scripts/start-all.sh

SIM_JSON=$(curl --fail --silent --show-error -X POST http://localhost:8080/api/cameras \
  -H 'content-type: application/json' \
  -d '{"name":"unified-e2e-source","auto_start":true,"loop":true,"source":{"kind":"local","local_path":"sample-320x240.mp4"}}')
SIM_ID=$(printf '%s' "$SIM_JSON" | jq -r .id)
SIM_PATH=$(printf '%s' "$SIM_JSON" | jq -r .stream_path)
ffprobe -v error -rtsp_transport tcp -show_streams \
  "rtsp://localhost:8554/simulator/$SIM_PATH"

VMS_JSON=$(curl --fail --silent --show-error -X POST http://localhost:8090/api/cameras \
  -H 'content-type: application/json' \
  -d "{\"name\":\"unified-e2e-vms\",\"rtsp_url\":\"rtsp://host.docker.internal:8554/simulator/$SIM_PATH\",\"recording_enabled\":true}")
VMS_ID=$(printf '%s' "$VMS_JSON" | jq -r .id)
VMS_PATH=$(printf 'vms_%s' "$VMS_ID")
ffprobe -v error -rtsp_transport tcp -show_streams \
  "rtsp://localhost:8555/$VMS_PATH"
```

Poll `/api/cameras/$VMS_ID/status` until `ONLINE`; open `http://localhost:8090`, require moving WebRTC pixels, and check the WHEP request goes to port 8889. After at least one finalized segment, query:

```bash
curl --fail --silent --show-error \
  "http://localhost:8090/api/recordings?camera_id=$VMS_ID&date=$(date -u +%F)" | jq
```

Fetch the returned playback URL, verify `Content-Type: video/mp4`, and inspect it with `ffprobe`.

### 28.3 Add Analytics

```bash
AN_JSON=$(curl --fail --silent --show-error -X POST http://localhost:8100/api/cameras \
  -H 'content-type: application/json' \
  -d "{\"vms_camera_id\":\"$VMS_ID\",\"name\":\"unified-e2e-analytics\",\"rtsp_url\":\"rtsp://host.docker.internal:8555/$VMS_PATH\"}")
AN_ID=$(printf '%s' "$AN_JSON" | jq -r .id)

curl --fail --silent --show-error -X PUT \
  "http://localhost:8100/api/cameras/$AN_ID/line" \
  -H 'content-type: application/json' \
  -d '{"name":"center-line","a":{"x":0.5,"y":0.1},"b":{"x":0.5,"y":0.9},"direction":"BOTH","enabled":true}' | jq
```

Poll the Analytics camera until `runtime.state == RUNNING`; fetch its snapshot and verify it is JPEG. Repeat with the object-bearing H.264 input and require at least one new event whose `crop` and `frame` routes return valid JPEGs. Query its recording lookup and verify the VMS-provided URL behavior.

Stop only Analytics:

```bash
(cd services/video-analytics && docker compose stop analytics)
curl --fail http://localhost:8090/health
curl --fail "http://localhost:8090/api/cameras/$VMS_ID/status"
(cd services/video-analytics && docker compose start analytics)
```

Require VMS live/recording continuity and Analytics recovery with a new worker session.

### 28.4 Add Semantic Search

Poll `http://localhost:8200/api/index/status` until the newly created C4 event is searchable. Then:

```bash
curl --fail --silent --show-error -X POST http://localhost:8200/api/search/text \
  -H 'content-type: application/json' \
  -d '{"query":"a person or vehicle crossing a line","top_k":10,"filters":{}}' | jq
```

Download the known event crop through C4 to a temporary file and submit it to `/api/search/image`; require the source event to rank first or record the exact existing semantic assertion used by the real-C4 tier. Verify one result per event, metadata filters, and event detail.

Stop only C4 and prove C5 local continuity:

```bash
(cd services/video-analytics && docker compose stop analytics)
curl --fail http://localhost:8200/health
curl --fail --silent --show-error -X POST http://localhost:8200/api/search/text \
  -H 'content-type: application/json' \
  -d '{"query":"vehicle","top_k":5,"filters":{}}' | jq
(cd services/video-analytics && docker compose start analytics)
```

Require the same indexed event IDs during outage, degraded upstream status with HTTP 200, and automatic return to upstream `available`. Stop C5 and require C1–C4 endpoints to remain healthy.

### 28.5 Persistence, restart, and cleanup

Capture IDs and result ordering, run `./scripts/stop-all.sh`, then `./scripts/start-all.sh`. Require:

- C1 camera ID/source, VMS registration/recording preference/history, Analytics events, and C5 vectors persist;
- C5 `index_revision` and top result IDs are unchanged when no new C4 event exists;
- no runtime artifact appears in `git status --short`;
- service-local `docker compose down` remains usable.

Delete API-created camera state through the owning public APIs only after evidence is recorded. Do not manually delete DB rows/files. Preserve disposable volumes until the failure-isolation tests in §26 complete.

## 29. Fresh-clone acceptance

### 29.1 Clean-room preparation

1. Ensure the unified branch is committed and optionally pushed.
2. Stop all unified stacks with `./scripts/stop-all.sh`.
3. Inspect final-name volumes. If they contain wanted state, back them up and obtain explicit approval before deletion. Never touch the legacy source volume names listed in §9.
4. Remove only the five explicitly named production volumes for this disposable validation, or run on a fresh Docker context/VM.
5. Confirm no old `genea_vms`, `genea-simulator`, `genea-vms`, `genea-analytics`, or `genea-semantic-search` container/project is running.

### 29.2 New checkout

```bash
CLEAN_PARENT=$(mktemp -d /private/tmp/genea-unified-acceptance.XXXXXX)
SOURCE_REPO=$(git rev-parse --show-toplevel)
git clone --no-local "$SOURCE_REPO" "$CLEAN_PARENT/Genea_VMS"
cd "$CLEAN_PARENT/Genea_VMS"
git checkout refactor/unified-submission
test -z "$(git status --porcelain)"
cp services/video-analytics/.env.example services/video-analytics/.env
```

When the branch is pushed, repeat with the GitHub URL and `--branch refactor/unified-submission --single-branch` so local Git object availability is not a hidden dependency.

### 29.3 Proof obligations

- Run all four Compose parses before any build.
- Build all images with no sibling directories available.
- Create the VMS host test venv from scratch using §15.2; do not copy the old venv.
- Run all component suites and record expected data-dependent skips explicitly.
- Run root start/status/smoke and the E2E chain with newly created API state.
- Verify model builds use public network access only as documented; run both final model runtimes with network disabled where their validators support it.
- Unset ambient component variables before testing defaults; do not rely on hidden shell exports.
- Search the checkout for local absolute paths and generated artifacts.
- Stop/start and verify persistence, then perform targeted disposable cleanup.

**Success means** no source sibling clone, old container/project/volume, developer DB/recording/event/index, developer venv, cached model, Hugging Face credential, hidden environment variable, or undocumented manual setup was required.

## 30. Final consistency and merge-to-main gate

All boxes must be checked and evidenced in the unified handoff:

- [ ] Branch is `refactor/unified-submission`; source and final SHAs are recorded.
- [ ] Original branches and remote refs are unchanged.
- [ ] Four and only four deployable production stacks exist.
- [ ] C2+C3 are one VMS imported from the C3 SHA; `feat/vms` was not independently imported.
- [ ] Every service starts from its own directory and all Compose files parse.
- [ ] Explicit project and volume names match §9.
- [ ] Project `down`, `down --remove-orphans`, and disposable `down -v` isolation passed.
- [ ] Ports, MediaMTX boundaries, `host.docker.internal`, and public API contracts match §§10–11.
- [ ] No shared DB/data volume/network/application import exists.
- [ ] Every mandatory component test tier passed with counts/skips documented.
- [ ] C1 RTSP, VMS WebRTC, recording/playback, C4 decode/inference/event, and C5 indexing/text/image search passed.
- [ ] C5 remained searchable while C4 was down.
- [ ] Each downstream stop left upstream services healthy.
- [ ] Normal root stop/start preserved state; root start was idempotent.
- [ ] Fresh local clone and fresh remote clone procedures passed.
- [ ] Runtime models work offline; no runtime Hugging Face credential is used.
- [ ] No generated data, credentials, or operational developer absolute paths are tracked.
- [ ] `git diff --check` returns no output.
- [ ] `git status --short` returns no output after the final handoff commit.
- [ ] `git ls-files` contains only the intended tree in §16/Appendix A.
- [ ] The final handoff distinguishes historical evidence from newly executed results and records deviations.
- [ ] User reviewed the unified branch and explicitly approved merge.

Only then push and merge with a non-destructive, reviewable GitHub PR. Exact CLI fallback after explicit approval:

```bash
git push -u origin refactor/unified-submission
# Review/approve the PR first. Then, only with explicit merge approval:
git switch main
git pull --ff-only origin main
git merge --no-ff refactor/unified-submission
git push origin main
```

Do not force-push or rewrite feature-branch history. If `main` moved, rerun affected diff/merge checks and resolve through review; do not force the old base.

## 31. Commit boundaries and rollback map

| Commit | Contains | Must not contain | Rollback |
|---|---|---|---|
| `chore: scaffold unified submission layout` | root ignore, archived PRD/plan location | service code | revert commit |
| `refactor: import rtsp simulator service` | exact C1 import + C1 Compose/README relocation edits | other services | revert commit |
| `refactor: import vms live and recording service` | exact C3 snapshot + listed VMS reproducibility edits | independent C2 import | revert commit |
| `refactor: import video analytics service` | exact C4 snapshot + volume/docs/whitespace edits | application semantics | revert commit |
| `refactor: import semantic search service` | exact C5 snapshot + volume/docs edits | model/ranking changes | revert commit |
| `chore: add unified stack lifecycle scripts` | four root scripts | application code/root Compose | revert commit |
| `docs: preserve component engineering handoffs` | five handoffs and repaired links | rewritten validation claims | revert commit |
| `test: add unified repository smoke validation` | only smoke corrections if needed | product behavior changes | revert commit |
| `docs: record unified submission validation` | final evidence | unexecuted claims | revert commit |

If a phase fails before commit, do not use `git reset --hard` or broad `git clean`. Unstage only the phase prefix, move the failed new tree to a uniquely named directory under `/private/tmp` for diagnosis, and return to the prior clean commit. If a committed phase must be removed, use `git revert`; source branches and prior validated service commits remain available.

## 32. Risks, controls, and known limitations

| Risk | Evidence/control |
|---|---|
| Root tree collisions | 5–43 overlapping names per branch pair; avoided by `read-tree --prefix` |
| C2 duplicated/regressed | ancestry proves C3 contains C2; import C3 only |
| Compose orphan collision | C1/VMS currently both `genea_vms`; explicit unique project names and runtime destructive isolation gate |
| Apparent data loss after project rename | explicit physical volume names; new clean volumes by design; legacy volumes left untouched and documented |
| Relative bind/env resolution | root scripts `cd` to service directory; normalized paths inspected |
| Docker context bloat/secrets | service-local contexts, existing ignores, new VMS `.dockerignore`, context-size and tracked-file scans |
| Host networking regression | preserve all `host-gateway` mappings and published-host integration |
| Test weakening after move | exact commands/counts, any unexpected skip blocks, no assertion changes allowed |
| VMS browser test is not fresh-clone reproducible | add only Playwright 1.62.0 as separate E2E extra; create venv explicitly |
| C4 source whitespace gate | remove one known EOF blank line only |
| C4 model/test network | model build and checksum-pinned bus test asset require documented network; runtime remains offline for model acquisition |
| C5 build size/time | ~812 MB model and ~3 GB image are preserved known costs |
| C5 semantic-eval cache | ignored user-owned cache cannot be copied; evaluation must use newly captured data or be explicitly recorded as not run |
| Clean clone accidentally uses old state | explicit final volumes, targeted removal/backup gate, fresh API-created state, no sibling clones |
| Scope creep | application files are frozen except the enumerated VMS test-packaging correction; deviations require review |

Known acceptable limitations retained from the PRD/handoffs:

- Apple Silicon/Docker Desktop is the only historically validated host; amd64 and native Linux remain unverified until run.
- Root orchestration is Bash over four independent projects, not production orchestration.
- Published host ports remain the cross-stack boundary.
- Runtime state is single-host/single-process where each service already requires it.
- C4 throughput and four-camera fairness remain host-dependent.
- C5 first build/backfill remains large and CPU-bound.
- Existing authentication, observability, retention, codec, and browser limitations remain unchanged.
- Perfect per-file `git log --follow` through relocation is not preserved; the source branches and recorded SHAs are the audit history.
- The Part 1 root README stays minimal until the separate Part 2 reviewer-packaging work.

## 33. Runtime validations still required

The planning session verified source/configuration statically but could not run Docker. Claude must not inherit historical claims as current results. Required live validations are:

1. all production and test image builds from the unified paths;
2. all component suites and actual counts/skips;
3. each service-local start/stop/down workflow;
4. normalized Compose names, mounts, networks, ports, healthchecks, and labels;
5. C1 real RTSP publish/read;
6. C1→VMS health and browser WebRTC;
7. VMS native recording, discovery, playback, retention-sensitive tests, and persistence;
8. VMS Control API remains private;
9. C4 H.264/TCP decode, real model, tracker, event durability, four-camera isolation, and VMS independence;
10. C5 model manifest/offline runtime, indexing, text/image ranking, restart idempotency, C4 outage continuity, and C4 independence;
11. root script readiness, idempotency, reverse shutdown, and failure reporting;
12. Compose `down --remove-orphans` / disposable `down -v` isolation;
13. clean local and remote clone reproduction;
14. Apple Silicon result; amd64/native Linux only if actually available—otherwise keep them openly unverified;
15. no unexpected runtime artifacts in Git paths.

## Appendix A — Exact imported file manifests

The following manifests are exhaustive for each frozen commit. Prefix each path with the target directory shown. Apply only the removals/additions/edits explicitly listed in §§14–18; any other final path is a plan deviation.

### A.1 `services/rtsp-simulator/` from `a6e02da3e029c742a4d5c2fe92e1fe1de85011f7`

Final disposition: all paths below remain under the prefix except `HANDOFF.md`, which moves to `docs/engineering-handoffs/component-1-rtsp-simulator.md`.

```text
.env.example
.gitignore
HANDOFF.md
README.md
docker-compose.yml
mediamtx/mediamtx.yml
sample-media/README.md
sample-media/sample-320x240.mp4
simulator/.dockerignore
simulator/Dockerfile
simulator/app/__init__.py
simulator/app/api/__init__.py
simulator/app/api/cameras.py
simulator/app/api/errors.py
simulator/app/config.py
simulator/app/domain/__init__.py
simulator/app/domain/models.py
simulator/app/logging_config.py
simulator/app/main.py
simulator/app/persistence/__init__.py
simulator/app/persistence/camera_repository.py
simulator/app/persistence/database.py
simulator/app/services/__init__.py
simulator/app/services/camera_manager.py
simulator/app/services/ffmpeg_command.py
simulator/app/services/probe.py
simulator/app/services/process_manager.py
simulator/app/services/source_storage.py
simulator/app/static/app.js
simulator/app/static/index.html
simulator/app/static/styles.css
simulator/pyproject.toml
simulator/tests/__init__.py
simulator/tests/conftest.py
simulator/tests/test_api_cameras.py
simulator/tests/test_ffmpeg_command.py
simulator/tests/test_integration_rtsp.py
simulator/tests/test_lifecycle.py
simulator/tests/test_probe.py
simulator/tests/test_static_ui.py
simulator/tests/test_storage.py
simulator/tests/test_validation.py
```

### A.2 `services/vms/` from `21feefd6e65bf7f8f4ac246126863aa7a3dc7bbf`

Final addition not in source: `.dockerignore`.

```text
.env.example
.gitignore
Dockerfile
README.md
THIRD_PARTY_NOTICES.md
app/__init__.py
app/api/__init__.py
app/api/cameras.py
app/api/errors.py
app/api/recordings.py
app/config.py
app/domain/__init__.py
app/domain/models.py
app/logging_config.py
app/main.py
app/persistence/__init__.py
app/persistence/camera_repository.py
app/persistence/database.py
app/security/__init__.py
app/security/rtsp_url.py
app/services/__init__.py
app/services/camera_manager.py
app/services/mediamtx_client.py
app/services/recording_manager.py
app/static/app.js
app/static/index.html
app/static/styles.css
app/static/vendor/mediamtx-reader.js
docker-compose.yml
mediamtx/mediamtx.yml
pyproject.toml
tests/__init__.py
tests/conftest.py
tests/test_api_cameras.py
tests/test_api_recordings.py
tests/test_camera_manager.py
tests/test_e2e_live_view.py
tests/test_integration_mediamtx.py
tests/test_mediamtx_client.py
tests/test_repository.py
tests/test_rtsp_url_security.py
tests/test_static_ui.py
tests/test_validation.py
```

### A.3 `services/video-analytics/` from `786062c64fd51c20bafb02c04031ad8558c925de`

```text
.dockerignore
.env.example
.gitignore
Dockerfile
README.md
THIRD_PARTY_NOTICES.md
app/__init__.py
app/analytics/__init__.py
app/analytics/crossing.py
app/analytics/detector.py
app/analytics/gst_pipeline.py
app/analytics/sampling.py
app/analytics/stream_worker.py
app/analytics/tracker.py
app/analytics/types.py
app/api/__init__.py
app/api/cameras.py
app/api/errors.py
app/api/events.py
app/api/lines.py
app/config.py
app/domain/__init__.py
app/domain/models.py
app/logging_config.py
app/main.py
app/persistence/__init__.py
app/persistence/camera_repository.py
app/persistence/database.py
app/persistence/event_repository.py
app/persistence/event_storage.py
app/persistence/line_repository.py
app/security/__init__.py
app/security/event_paths.py
app/security/rtsp_url.py
app/services/__init__.py
app/services/camera_manager.py
app/services/event_service.py
app/services/presentation.py
app/services/vms_recordings.py
app/static/app.js
app/static/index.html
app/static/styles.css
docker-compose.test.yml
docker-compose.yml
pyproject.toml
requirements/runtime.lock
requirements/test.lock
requirements/torch-cpu.txt
scripts/seed_ultralytics.py
scripts/validate_runtime.py
scripts/verify_event_store.py
tests/__init__.py
tests/conftest.py
tests/e2e/__init__.py
tests/e2e/conftest.py
tests/e2e/test_analytics_ui.py
tests/fakes/__init__.py
tests/fakes/app_harness.py
tests/fakes/detector.py
tests/fakes/e2e_server.py
tests/fakes/factories.py
tests/fakes/publisher_client.py
tests/fakes/worker.py
tests/fixtures/mediamtx.yml
tests/fixtures/publisher/Dockerfile
tests/fixtures/publisher/control.py
tests/integration/__init__.py
tests/integration/conftest.py
tests/integration/test_detector_real.py
tests/integration/test_four_camera_isolation.py
tests/integration/test_gstreamer_rtsp.py
tests/integration/test_tracker_real.py
tests/integration/test_worker_reconnect.py
tests/unit/__init__.py
tests/unit/conftest.py
tests/unit/test_api_cameras.py
tests/unit/test_api_events.py
tests/unit/test_api_lines.py
tests/unit/test_camera_manager.py
tests/unit/test_config.py
tests/unit/test_crossing.py
tests/unit/test_database.py
tests/unit/test_detector.py
tests/unit/test_event_storage.py
tests/unit/test_marker_selection.py
tests/unit/test_repositories.py
tests/unit/test_rtsp_url.py
tests/unit/test_sampling.py
tests/unit/test_static_ui.py
tests/unit/test_tracker.py
tests/unit/test_vms_recordings.py
```

### A.4 `services/semantic-search/` from `6df06855be8c29d1f87d1509fb26a9222d7de6fb`

```text
.dockerignore
.env.example
.gitignore
Dockerfile
README.md
THIRD_PARTY_NOTICES.md
app/__init__.py
app/api/__init__.py
app/api/errors.py
app/api/events.py
app/api/index.py
app/api/search.py
app/config.py
app/domain/__init__.py
app/domain/models.py
app/embeddings/__init__.py
app/embeddings/manifest.py
app/embeddings/runtime.py
app/integrations/__init__.py
app/integrations/component4.py
app/logging_config.py
app/main.py
app/persistence/__init__.py
app/persistence/database.py
app/persistence/repositories.py
app/persistence/schema.sql
app/retrieval/__init__.py
app/retrieval/ranking.py
app/retrieval/snapshot.py
app/security/__init__.py
app/security/images.py
app/services/__init__.py
app/services/discovery.py
app/services/indexer.py
app/services/inference.py
app/services/reconciliation.py
app/services/search.py
app/static/app.js
app/static/favicon.svg
app/static/index.html
app/static/styles.css
docker-compose.test.yml
docker-compose.yml
pyproject.toml
requirements/runtime.in
requirements/runtime.lock
requirements/test.in
requirements/test.lock
requirements/torch-cpu.txt
scripts/benchmark_index.py
scripts/capture_eval_set.py
scripts/fetch_model.py
scripts/reindex.py
scripts/validate_real_component4.py
scripts/validate_runtime.py
tests/__init__.py
tests/assets/semantic_eval_manifest.json
tests/conftest.py
tests/e2e/__init__.py
tests/e2e/conftest.py
tests/e2e/test_dashboard.py
tests/fakes/__init__.py
tests/fakes/component4_server.py
tests/fakes/factories.py
tests/integration/__init__.py
tests/integration/conftest.py
tests/integration/test_api.py
tests/integration/test_discovery_indexing.py
tests/integration/test_recovery.py
tests/real_component4/__init__.py
tests/real_component4/conftest.py
tests/real_component4/test_live_contract.py
tests/real_component4/test_live_pipeline.py
tests/real_model/__init__.py
tests/real_model/conftest.py
tests/real_model/test_embedding_runtime.py
tests/real_model/test_semantic_quality.py
tests/scale/__init__.py
tests/scale/conftest.py
tests/scale/test_search_scale.py
tests/unit/__init__.py
tests/unit/conftest.py
tests/unit/test_component4_client.py
tests/unit/test_config.py
tests/unit/test_database.py
tests/unit/test_domain_models.py
tests/unit/test_errors.py
tests/unit/test_image_security.py
tests/unit/test_inference_coordinator.py
tests/unit/test_repositories.py
tests/unit/test_retrieval.py
tests/unit/test_static_ui.py
```

## 34. Unresolved question requiring user input

Implementation must stop at the first failed phase gate, preserve the last validated commit, and record any approved deviation in the unified handoff.

No unresolved repository or architecture question remains. Project names, branch mapping, C2/C3 supersession, volume ownership, ports, network topology, import mechanism, root configuration, reset behavior, and test commands are resolved by repository evidence and this plan.

**OPEN QUESTION REQUIRING USER INPUT:** Which legally usable H.264 person/vehicle clip should be used for the fresh-clone real event-generation acceptance? The repository contains only `sample-320x240.mp4`, an SMPTE/color-bar source on which YOLO correctly emits no event. The historical C4 acceptance used a generated pan over a real street image, but that clip is not committed, and the C5 handoff explicitly records the lack of a source capable of creating a new event after startup. Before Phase 8, provide/identify an owned or redistributable clip, or explicitly approve a documented ephemeral download-and-generate procedure. Until resolved, media transport, decoding, and model tests can pass, but a newly generated C4 event flowing into C5 from a truly clean clone cannot honestly be claimed.
