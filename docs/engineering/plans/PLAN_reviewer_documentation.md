# Part 2 Implementation Plan — Reviewer-Facing Documentation

## 1. Executive summary

This plan turns the completed Genea VMS monorepo into a reviewer-ready submission without changing application behavior. Claude Code must create a concise root landing page, a decision-oriented architecture document, a deterministic demo guide, a validation evidence document, and the final Part 2 handoff. It must also make only the verified factual corrections listed here in three service READMEs.

The reviewer narrative must remain streaming-first:

1. RTSP source simulation and ingest.
2. Browser live view through WebRTC/WHEP.
3. MediaMTX-native recording and historical playback.
4. Analytics as an isolated extension.
5. Semantic event retrieval as a second isolated extension.

The implementation is documentation-only. No Python, JavaScript, HTML, CSS, Compose, MediaMTX, shell-script, test, model, sample-media, or runtime-configuration changes are planned. Screenshots, Loom, publishing, release work, and submission packaging remain Part 3.

## 2. Planning baseline and claim discipline

Use these labels while implementing and reviewing important claims:

- **VERIFIED FACT** — confirmed in current code/configuration, current Git state, or final measured Part 1 evidence.
- **FROZEN REQUIREMENT** — required by the approved Part 2 PRD or original assignment.
- **CODEX IMPLEMENTATION DECISION** — a restrained documentation choice made by this plan.
- **ASSUMPTION** — plausible but not verified; never publish it as fact.
- **DOCUMENTATION VALIDATION REQUIRED** — must be checked again after documentation is written.
- **OPEN QUESTION REQUIRING USER INPUT** — a choice Claude must not make silently.

| Classification | Finding |
|---|---|
| VERIFIED FACT | Repository root is `Genea_VMS`; current branch is `main`. |
| VERIFIED FACT | `HEAD` and local `main` are `52029245cf084b6e26578814ce5bca3480787782`. |
| VERIFIED FACT | That commit is `Merge branch 'refactor/unified-submission'`; the merged feature tip is `80ba165`. |
| VERIFIED FACT | The tracked working tree was clean before this plan was created. This plan is the only intended new file from the planning task. |
| VERIFIED FACT | `origin/main` is `bdd4b72987da6aa0c44b73934646793eea9748bb`; local `main` is ten commits ahead. Do not branch from stale `origin/main`. |
| VERIFIED FACT | Neither local nor remote-tracking `docs/reviewer-submission` exists at planning time. |
| VERIFIED FACT | Origin is `https://github.com/Atharvavp/Genea_VMS.git`; remote publication/certification remains Part 3. |
| VERIFIED FACT | Root `README.md` is an 11-byte placeholder containing `# Genea_VMS` without a terminating newline. |
| VERIFIED FACT | There are five logical components and four deployable services; Components 2 and 3 share `services/vms/`. |
| VERIFIED FACT | Four independent Compose projects are coordinated by root wrappers; there is no root Compose project, shared application database, or shared Compose network. |
| VERIFIED FACT | During planning, all four running UIs, `/health`, `/docs`, and `/openapi.json` endpoints returned HTTP 200. |
| VERIFIED FACT | Final Part 1 evidence records 1,384 passed tests, four conditional C5 semantic-quality skips, and no failures; final real C4-to-C5 was 10 passed, 0 failed, 0 skipped. |
| FROZEN REQUIREMENT | Primary outputs are `README.md`, `docs/architecture.md`, `docs/demo-guide.md`, and `docs/validation.md`, with embedded Mermaid. |
| FROZEN REQUIREMENT | Final implementation handoff is `docs/engineering-handoffs/HANDOFF_reviewer_documentation.md`. |
| CODEX IMPLEMENTATION DECISION | Archive this approved plan under `docs/engineering/plans/` during implementation so the root stays reviewer-focused. |
| CODEX IMPLEMENTATION DECISION | Use one overview Mermaid in README and at most one complementary boundary diagram in the architecture document. |
| DOCUMENTATION VALIDATION REQUIRED | Re-run URL, command, link, Mermaid, content-safety, and Git-hygiene checks after all edits. |
| ASSUMPTION | The existing GitHub origin will be the final submission URL. Use it for the clone command but do not claim it is current/public until Part 3. |

## 3. Git baseline, branch, and commits

### 3.1 Preconditions

Before implementation, run:

```bash
git branch --show-current
git rev-parse HEAD
git rev-parse main
git status --short --branch
git log --oneline --decorate -n 12
git show-ref --verify --quiet refs/heads/docs/reviewer-submission; test $? -eq 1
```

Expected baseline:

```text
branch: main
HEAD:   52029245cf084b6e26578814ce5bca3480787782
main:   52029245cf084b6e26578814ce5bca3480787782
```

`git status` may show only this untracked planning artifact. If any other change exists, stop and ask the user how to preserve it. Do not clean, reset, stash, or overwrite user work.

### 3.2 Branch creation

Create the branch from verified local `main`, carrying this plan with it:

```bash
git switch -c docs/reviewer-submission 52029245cf084b6e26578814ce5bca3480787782
mkdir -p docs/engineering/plans
git mv PLAN_reviewer_documentation.md docs/engineering/plans/PLAN_reviewer_documentation.md
```

Do not fetch, pull, rebase onto, or branch from `origin/main` merely because it is a remote-tracking ref. If the desired branch exists by implementation time, inspect its ancestry/worktree first and stop for user direction if it diverges.

### 3.3 Commit strategy

Use reviewable documentation commits after the matching gate passes:

1. `docs: preserve reviewer documentation plan`
2. `docs: add reviewer landing page and architecture guide`
3. `docs: add demo and validation guides`
4. `docs: correct service documentation references`
5. `docs: record reviewer documentation handoff`

Adjacent phases may be combined when that improves reviewability, but never mix a runtime change into a docs commit. Do not push; Part 3 owns remote publication.

## 4. Source-of-truth hierarchy

Resolve conflicts in this order:

1. Current code, routes, Compose files, MediaMTX configuration, and scripts.
2. `docs/engineering-handoffs/HANDOFF_unified_submission.md` for final measured Part 1 evidence.
3. `PRD_part2_reviewer_documentation_for_codex.md` for Part 2 scope.
4. `Video Streaming Challenge - Genea.pdf` for assignment requirements/evaluation.
5. Part 1 PRD and approved plan for frozen architecture/provenance.
6. Historical component handoffs.
7. Service READMEs after checking them against implementation.
8. Explicit implementation decisions.
9. Explicit assumptions.

Never retain a stronger claim when a higher-priority source supports only a narrower one.

## 5. Inputs and repository surfaces inspected

The planning audit covered both attached inputs completely, including visual review of all three rendered assignment-PDF pages, plus:

- root `README.md`, `.gitignore`, scripts, tracked tree, ignored-artifact state, branch graph, and remote configuration;
- `docs/engineering-handoffs/HANDOFF_unified_submission.md` in full;
- `docs/engineering-handoffs/component-1-rtsp-simulator.md`, `component-2-vms-live-view.md`, `component-3-recording-playback.md`, `component-4-video-analytics.md`, and `component-5-semantic-search.md` in full;
- `docs/engineering/requirements/PRD_unified_submission_repository_for_codex.md` and `docs/engineering/plans/PLAN_unified_submission.md` in full;
- all four service READMEs, Compose/Docker/MediaMTX/env/dependency files;
- application entry points, public routes, domain contracts, static UIs, persistence/integration code, and relevant tests;
- read-only current Docker status and public endpoint checks against already-running stacks.

Claude must repeat a focused inventory in Phase 1 because content can drift after planning. It need not re-read every historical artifact unless the baseline changed.

## 6. Repository findings

### 6.1 Root lifecycle

| Script | Verified behavior | Documentation consequence |
|---|---|---|
| `scripts/start-all.sh` | Preflights Docker, Compose/project names, curl, daemon, and required C4 `.env`; starts C1 → VMS → C4 → C5 with `up -d --build`, then polls readiness. | Canonical startup. State that first build can be long and needs network access for images/dependencies/model assets. |
| `scripts/status.sh` | Read-only Compose status plus app health, URLs/media endpoints, and C5 upstream state; returns nonzero on query/health failures. | Canonical readiness/status check. |
| `scripts/smoke-test.sh` | Non-mutating structural/project/HTTP checks; final evidence is 38 passed/0 failed. | Use after status; do not call it media/browser validation. |
| `scripts/stop-all.sh` | Stops C5 → C4 → VMS → C1 with `docker compose down`, continues across failures, retains volumes. | Canonical shutdown; explicitly state persistence remains. |

There is no root reset script. Do not invent one or recommend volume deletion in the reviewer path.

### 6.2 Deployables, ports, and persistence

| Logical component | Deployable / project | Compose services | Published ports | Named persistence |
|---|---|---|---|---|
| C1 RTSP Simulator | `services/rtsp-simulator/` / `genea-simulator` | `simulator`, `mediamtx` | `8080` HTTP, `8554/tcp` RTSP | `genea-simulator-data` |
| C2 VMS + C3 Recording | `services/vms/` / `genea-vms` | `vms`, `vms-mediamtx` | `8090` HTTP, `8555/tcp` RTSP, `8889/tcp` WHEP, `8189/udp` ICE, `9996/tcp` playback | `genea-vms-data`, `genea-vms-recordings` |
| C4 Analytics | `services/video-analytics/` / `genea-analytics` | `analytics` | `8100` HTTP | `genea-analytics-data` |
| C5 Semantic Search | `services/semantic-search/` / `genea-semantic-search` | `semantic-search` | `8200` HTTP | `genea-semantic-search-data` |

VMS MediaMTX control `9997` is private/unpublished. Do not present it as a reviewer API.

### 6.3 Cross-stack boundaries

- VMS pulls C1 through `rtsp://host.docker.internal:8554/simulator/<stream_path>`.
- C4 pulls VMS redistribution through `rtsp://host.docker.internal:8555/vms_<camera-id>`.
- C4 optionally reads VMS recording history over `http://host.docker.internal:8090/api/recordings`.
- C5 discovers events and proxies event images/recording lookup solely through C4's public HTTP API at `http://host.docker.internal:8100`.
- Host-gateway mappings support host-published boundaries on supported Docker platforms.
- No application DB, filesystem mount, or Compose network is shared across services.

### 6.4 C1 behavior to explain

- UI creates a camera by uploading a file or selecting the read-only `sample-media/` mount; one FFmpeg publisher publishes to C1 MediaMTX.
- States are `CREATED`, `STARTING`, `RUNNING`, `STOPPING`, `STOPPED`, `ERROR`; actions include Add, Start, Stop, Restart, Edit, Delete, and Copy.
- Host route is `rtsp://localhost:8554/simulator/<stream_path>`.
- H.264 is the safe end-to-end demo codec. C1 can publish H.265, but that is not the browser/analytics baseline.
- Committed `sample-320x240.mp4` is a short looping color-bar fixture for transport/live/recording/playback. It has no supported moving object and cannot prove analytics/search.
- `RUNNING` means its publisher process is alive, not guaranteed downstream frame receipt.

### 6.5 C2/C3 behavior to explain

- VMS registers RTSP sources and reconciles MediaMTX paths via private port 9997.
- Source health (`ONLINE`, `OFFLINE`, `UNKNOWN`) is distinct from browser-player state (`CONNECTING`, `LIVE`, `RECONNECTING`, `ERROR`).
- Media bypasses FastAPI: MediaMTX pulls/redistributes RTSP, serves WHEP/WebRTC, records, and serves playback.
- Recording is off by default, active only while enabled, and its desired preference persists.
- Recording UI states include `DISABLED`, `WAITING`, `RECORDING`, `ERROR`; `RECORDING` is inferred from configuration/source availability, not byte-level proof.
- History is queried by camera and UTC date and returns finalized timespans. Playback is direct on 9996; accepted evidence showed `Accept-Ranges: none`.

### 6.6 C4 behavior to explain

- Standalone FastAPI service with its own SQLite metadata and JPEG event artifacts.
- One enabled-camera worker uses RTSP/TCP GStreamer H.264 decode and latest-frame/drop semantics.
- Shared YOLO11n CPU detector; ByteTrack state is per camera worker/session.
- Supported classes: `person`, `bicycle`, `car`, `motorcycle`, `bus`, `truck`, grouped as person/vehicle.
- One normalized line per camera; configuration accepts `A_TO_B`, `B_TO_A`, `BOTH`; emitted event stores actual crossing direction.
- Events expose durable metadata, crop, frame, and optional recording lookup through public HTTP APIs.
- Source interruption reconnects with exponential backoff/jitter. C4 failure does not stop core streaming/recording or C5 search over already indexed data.
- No VMS discovery/control, re-identification, or event retention.

### 6.7 C5 behavior to explain

- Polls/reconciles C4 public APIs; never reads C4 private DB/files.
- Embeds crop and frame using pinned SigLIP and persists 768-D normalized `float32` vectors plus metadata in its SQLite DB.
- Builds immutable NumPy snapshots and performs exact cosine search; it is not a vector DB or RAG.
- Aggregates crop/frame scores with the maximum per event.
- Supports text and JPEG/PNG image search plus camera/category/class/direction/UTC/top-k/min-score filters.
- During C4 outage, local search/detail remains available; new indexing and C4-backed images/recording lookup degrade explicitly.
- Polling resumes automatically; local state persists and the index is rebuildable from C4.
- Event search only: no arbitrary-video search, transcription, identity search, or general retrieval-accuracy claim.

## 7. Part 1 evidence to carry forward

Publish this as **measured during final Part 1 acceptance**, never as an SLA or newly rerun Part 2 result.

| Component | Passed | Conditional skips | Breakdown/meaning |
|---|---:|---:|---|
| C1 | 143 | 0 | 141 default + 2 integration. |
| C2/C3 | 376 | 0 | 328 default + 28 integration + 20 end-to-end. |
| C4 | 600 | 0 | Unit, integration, real-model, four-camera, end-to-end. |
| C5 | 265 | 4 | Includes real-C4 10/10; four distinct quality cases skipped without user-owned cached fixture. |
| **Total** | **1,384** | **4** | No failures in recorded final acceptance. |

Avoid “all tests passed.” Use: “Final Part 1 acceptance recorded 1,384 passes, four explicitly documented conditional C5 semantic-quality skips, and no failures.”

Live accepted evidence:

- real H.264 `320x240` C1 stream and stop/start route behavior;
- VMS `OFFLINE` → `ONLINE`, H.264 RTSP redistribution, and Chromium moving-pixel WebRTC validation;
- finalized recording, HTTP 200 `video/mp4` playback, independently probed H.264;
- real vehicle crossing with visually checked C4 crop/frame from user-owned acceptance media;
- C5 final accepted state: 486 known/searchable/complete events, 486 crop + 486 frame representations, revision 972, no failed events;
- text `car` ranked a car first on the fixed traffic corpus; filters worked;
- same event-crop query returned its source event rank 1 at cosine score 1.0;
- final `real_component4`: 10 passed, 0 failed, 0 skipped;
- C5 degraded-but-searchable C4 outage and automatic recovery without C5 restart;
- root smoke 38/0, persistence, failure isolation, repeated startup, and local fresh-clone evidence.

Detailed timings belong only in `docs/validation.md`, copied exactly from final evidence and labeled observations rather than guarantees.

## 8. Current documentation assessment

- Root README is only a placeholder. The three reviewer docs do not exist.
- Existing engineering docs are deep provenance/evidence, not a five-minute entry point.
- Part 1 PRD/plan are architecture provenance; final unified handoff is evidence authority.

Service README audit:

| File | Assessment | Planned action |
|---|---|---|
| `services/rtsp-simulator/README.md` | Materially consistent with current UI, paths, ports, and behavior. | No change. |
| `services/vms/README.md` | Accurate overall; title omits C3 and one paragraph says C1 lives in its own clone. | Correct only title and stale clone wording. |
| `services/video-analytics/README.md` | Accurate overall; retains VMS-clone wording and broken terminal handoff link. | Correct those two items. |
| `services/semantic-search/README.md` | Accurate overall; two `HANDOFF_component_5.md` links are broken. | Replace both links. |

Do not rewrite service READMEs for voice or duplicate root documentation.

## 9. Reviewer journey and documentation architecture

Design for three depths:

1. **Two minutes:** README opening explains assignment, core path, deployables/components, architecture.
2. **Five to ten minutes:** mapping, quick start, endpoints, validation, choices, scale/reliability, limitations.
3. **Hands-on:** demo guide, then architecture/validation, Swagger, service READMEs, handoffs as needed.

Canonical roles:

- `README.md`: navigation and the complete shortest truthful story.
- `docs/architecture.md`: why boundaries, decisions, and tradeoffs exist.
- `docs/demo-guide.md`: exact operator actions and observable results.
- `docs/validation.md`: what was measured, provenance, and untested scope.
- Service READMEs: component setup/configuration/internals.
- Engineering handoffs: detailed provenance and acceptance history.

## 10. Target tree and change scope

```text
README.md                                      # rewrite
docs/
├── architecture.md                           # create
├── demo-guide.md                             # create
├── validation.md                             # create
├── engineering/plans/
│   ├── PLAN_unified_submission.md
│   └── PLAN_reviewer_documentation.md        # move this plan
└── engineering-handoffs/
    ├── HANDOFF_reviewer_documentation.md     # create last
    ├── HANDOFF_unified_submission.md
    └── component-*.md
services/
├── rtsp-simulator/README.md                   # unchanged
├── vms/README.md                              # narrow correction
├── video-analytics/README.md                  # narrow correction
└── semantic-search/README.md                  # narrow correction
```

Do not copy the external Part 2 PRD or assignment PDF into the repo without a separate user request.

## 11. Exact planned file specifications

### 11.1 `README.md` — modify

**Purpose:** reviewer landing page, understandable in about five minutes and sufficient to start without internal handoffs.

**Exact section order:**

1. `# Genea Video Management System`
2. One short positioning paragraph: open-source, single-host RTSP/WebRTC VMS with recording/playback plus optional analytics/search.
3. `## Assignment coverage` — Assignment → Implementation table; core rows before extensions.
4. `## Architecture at a glance` — primary Mermaid plus reading guide.
5. `## Five components, four services` — responsibility/deployable/UI table.
6. `## Quick start` — prerequisites, C4 `.env`, clone/start/status/smoke, first-build note.
7. `## Open the system` — dashboard, Swagger, health, media, and port tables.
8. `## Demo in 12 steps` — short path linked to full guide; bundled versus user-supplied media distinction.
9. `## Engineering choices` — short bullets linking to architecture.
10. `## Validation summary` — accepted Part 1 evidence and Part 2 checks, linked to validation.
11. `## Reliability and scalability` — compact current/future subsections.
12. `## Known limitations`.
13. `## Repository map` — top-level roles only.
14. `## More detail` — primary docs, service READMEs, unified/component handoffs, Part 1 PRD/plan.
15. `## Stop the system` — or place this immediately after Quick start if it scans better.

**Evidence/source:** root scripts, Compose/config/routes, UIs, verified service docs, final unified handoff, assignment, Part 2 PRD.

**Links:** the three new reviewer docs; every service README; unified/five component handoffs; Part 1 requirement/plan. Link to the Part 2 handoff only from a restrained evidence index, if at all.

**Commands/URLs to verify:** all commands and endpoint tables in Sections 17–18.

**Runtime claims to validate:** readiness, HTTP URLs, smoke count if described as rerun in Part 2, and demo outcomes.

**Do not duplicate:** detailed test tiers, complete APIs/event schemas, deep tradeoffs/outage timeline, or full walkthrough.

**Validation:** Markdown links, Mermaid rendering, copy/paste commands, endpoints, terminology, safety scans, and a five-minute read-through.

### 11.2 `docs/architecture.md` — create

**Purpose:** explain why the system is shaped this way within the take-home/single-host boundary.

**Exact outline:**

1. `# Architecture`
2. `## Goals and constraints`
3. `## System context` — streaming-first, five components/four deployables.
4. `## Responsibility boundaries` — owns/does-not-own table.
5. `## Media flow`.
6. `## Control and API flow`.
7. `## Recording and playback`.
8. `## Analytics pipeline`.
9. `## Event and semantic-search lifecycle`.
10. `## Persistence and isolation`.
11. `## Failure and recovery behavior`.
12. `## Architecture decisions and tradeoffs`.
13. `## Scalability` — current versus future explicitly separated.
14. `## Security and deployment caveats`.
15. `## Known limitations and non-goals`.
16. `## Evidence and further reading`.

Decision table topics, each with context, choice, benefit, and tradeoff:

- MediaMTX for ingest, redistribution, WebRTC, recording, playback.
- WHEP/WebRTC for browser live.
- no GStreamer processing in VMS live path; GStreamer only where C4 needs frames;
- standalone C4 failure boundary;
- MediaMTX-native recording instead of FastAPI media proxy;
- per-camera C4 workers with a shared detector;
- event-oriented C5, C4 as authority, C5 as public-API consumer/derived state;
- pinned SigLIP plus exact NumPy instead of external vector DB at tested scale;
- independent Compose stacks/volumes with thin root orchestration;
- host-published cross-stack integration and its portability tradeoff.

**Evidence/source:** Compose/MediaMTX/integration code, domain models, manifests, final handoff, Part 1 docs.

**Links:** README, demo, validation, all service READMEs, unified handoff.

**Runtime claims to validate:** any performance/recovery/playback figures and portability wording.

**Do not duplicate:** quick start, full demo, exhaustive endpoints, or raw logs.

**Validation:** both Mermaid/links; confirm prohibited architecture terms are absent or explicitly negated.

### 11.3 `docs/demo-guide.md` — create

**Purpose:** deterministic reviewer walkthrough and future Loom-script foundation.

**Exact outline:**

1. `# End-to-end demo guide`
2. `## What this demonstrates`
3. `## Before you start` — resources/browser/env/media/codec/build wait.
4. `## Start and verify the stacks`
5. `## 1. Create and start a simulated RTSP camera`
6. `## 2. Register the camera in VMS`
7. `## 3. Verify browser live view`
8. `## 4. Enable recording and play history`
9. `## 5. Register the VMS stream in Analytics`
10. `## 6. Configure a directional line`
11. `## 7. Generate and inspect a real event`
12. `## 8. Wait for C5 indexing`
13. `## 9. Run text search`
14. `## 10. Run image search`
15. `## 11. Open event detail and recording navigation`
16. `## Stop the stacks`
17. `## Troubleshooting the demonstrated path`
18. `## What the bundled sample does not demonstrate`

Each numbered section uses **Action**, **Expected state/result**, **Wait/retry**, and **If it does not happen**. Troubleshooting is adjacent to the action; the final section only consolidates common issues.

**Evidence/source:** actual UI labels, public routes, root scripts, sample media, C4/C5 behavior, accepted evidence.

**Links:** README, relevant architecture/validation sections, service troubleshooting, UI/Swagger URLs.

**Commands/URLs to verify:** every item in Section 18.

**Runtime claims:** each expected state, finalized short recording, crop/frame, index increment, searches, recording navigation.

**Do not duplicate:** design rationale, full test totals, API catalog, or private acceptance-media details.

**Validation:** perform with uniquely named demo entities when legal event media is available; never fabricate an event.

### 11.4 `docs/validation.md` — create

**Purpose:** evidence register separating final Part 1 acceptance from Part 2 documentation checks.

**Exact outline:**

1. `# Validation evidence`
2. `## How to read this evidence` — observations are not SLAs; identify evidence date/source.
3. `## Automated test matrix`.
4. `## Live RTSP and VMS validation`.
5. `## Browser/WebRTC validation`.
6. `## Recording and playback validation`.
7. `## Real analytics-event validation`.
8. `## Real C4-to-C5 validation`.
9. `## Failure isolation, outage, and recovery`.
10. `## Persistence and rebuild validation`.
11. `## Root orchestration and fresh-clone evidence`.
12. `## Part 2 documentation validation`.
13. `## Known untested areas`.
14. `## Evidence sources`.

Use tables with area, method/tier, accepted result, evidence source, caveat. Do not paste logs. Prefer final unified handoff over older handoffs.

**Links:** README, architecture, demo, unified handoff, component handoff only for useful deeper evidence.

**Commands/URLs:** Sections 22–24.

**Runtime claims:** anything phrased as rerun during Part 2. Otherwise say “recorded in final Part 1 acceptance.”

**Do not duplicate:** raw output, every test file, handoff prose, unsupported production language.

**Validation:** totals reconcile; four skips visible; `real_component4` clearly 10/0/0; tested/untested boundary clear.

### 11.5 Service README corrections — modify narrowly

`services/vms/README.md`:

- Change title to “Live View & Recording (Components 2 + 3).”
- Replace the claim C1 lives “in its own clone” with `services/rtsp-simulator/` plus independent `genea-simulator` Compose wording.
- Preserve historically accurate “Component 2 → 3” migration wording elsewhere.

`services/video-analytics/README.md`:

- Replace VMS-clone wording: VMS is another monorepo service directory/Compose project; analytics Compose commands do not operate it.
- Link `../../docs/engineering-handoffs/component-4-video-analytics.md` instead of nonexistent `HANDOFF_component_4.md`.

`services/semantic-search/README.md`:

- Replace both `HANDOFF_component_5.md` references with `../../docs/engineering-handoffs/component-5-semantic-search.md`.

**Evidence:** current tree/project names. **Validation:** focused diff/stale-reference/link checks. Do not add root-style narrative.

### 11.6 `docs/engineering-handoffs/HANDOFF_reviewer_documentation.md` — create last

**Purpose:** actual implementation record for review/Part 3 readiness.

**Exact outline:**

1. Objective/scope.
2. Branch, starting/final SHA, commits.
3. Files created/moved/modified/unchanged.
4. README structure and reviewer journey.
5. Diagrams and what they depict.
6. Demo scope/media policy.
7. Validation scope/evidence reconciliation.
8. Service README corrections.
9. Commands and exact results.
10. URLs/states verified.
11. Link/Mermaid validation.
12. Secret/path/artifact/Git hygiene.
13. Non-doc changes (expected: none).
14. Deviations/reasons.
15. Limitations/open questions.
16. Part 3 readiness/remaining ownership.

Use actual diff/results, not intentions. Link primary docs, archived plan, unified handoff. Re-run checks after adding the handoff.

### 11.7 Per-file completion matrix

This matrix makes the implementation contract explicit; section outlines remain authoritative above.

| Path / operation | Evidence to use | Links to create/check | Commands, URLs, or claims to verify | Content not to duplicate | Required validation |
|---|---|---|---|---|---|
| `README.md` / modify | assignment, current code/config/scripts, final handoff | all three reviewer docs, four service READMEs, engineering evidence | root lifecycle, all public URLs/ports, accepted summary | deep rationale, raw evidence, full demo | five-minute read, Mermaid, links, live URLs, safety |
| `docs/architecture.md` / create | code/config boundaries, Part 1 architecture, final evidence | README, demo, validation, service docs/handoffs | every protocol/owner, measured recovery/performance scope | setup walkthrough, API catalog, logs | diagram/link/terminology and source review |
| `docs/demo-guide.md` / create | UI labels/routes/scripts and accepted live flow | README, architecture, validation, service help | every action/state/wait/URL; rerun scope | design rationale, full test table, private media | zero-state review and legal-media live run when available |
| `docs/validation.md` / create | final unified handoff plus actual Part 2 checks | README, architecture, demo, evidence handoffs | arithmetic, test tiers, live/outage/persistence claims | raw logs and repeated component prose | evidence reconciliation and exact caveats |
| `services/vms/README.md` / modify | tree and Compose project names | existing links plus corrected monorepo references | no runtime URL requires new testing | root reviewer story | focused diff/link/stale-word scan |
| `services/video-analytics/README.md` / modify | tree, Compose, actual handoff path | corrected C4 handoff link | no runtime claim changes | root/architecture prose | focused diff/link/stale-word scan |
| `services/semantic-search/README.md` / modify | actual C5 handoff path | both corrected C5 links | no runtime claim changes | root/validation prose | focused diff/link scan |
| Plan / move to `docs/engineering/plans/` | this approved artifact | final handoff link back to plan | Git rename and content identity | no reviewer summary copy | Git diff/status |
| `HANDOFF_reviewer_documentation.md` / create | actual diff, SHAs, validation output | new docs, archived plan, unified handoff | every result/URL/SHA it records | reviewer docs and raw logs | final link/safety/diff/status rerun |

## 12. Assignment → Implementation mapping

Use this concise README table; expand only through links:

| Assignment request | Implementation | Evidence/caveat |
|---|---|---|
| Stream live video from IP camera/webcam with open-source protocols | VMS accepts RTSP sources; C1 turns uploaded/mounted video into RTSP through FFmpeg + MediaMTX; VMS MediaMTX ingests/redistributes. | No credentialed physical IP-camera or direct-webcam end-to-end acceptance. |
| Browser live player | VMS MediaMTX WHEP/WebRTC to browser UI. | Chromium moving-pixel evidence; other browsers untested. |
| Store and play back | MediaMTX records enabled sources; VMS lists UTC timespans; browser plays direct from MediaMTX. | Single-host storage, no byte ranges, configured retention. |
| Setup/dependencies/docs | Root quick start, architecture/demo/validation, service READMEs, Swagger, Compose/scripts. | Part 2 output. |
| Code quality/testing/failures | Layered tests, smoke, real media, isolation, outage/recovery, persistence. | Exact counts/caveats; no production-ready claim. |
| Scalability/increased load | Current per-camera/persistence/search design documented with future scale-out. | Future direction is not implemented. |
| Network outages | VMS source state; C4 reconnect; C5 degraded local search and auto resync. | Selected cases, not broad chaos testing. |
| Optional AI inference | YOLO11n + ByteTrack line crossing → metadata/crop/frame. | Extension; no re-ID/general accuracy benchmark. |
| Optional enhanced search | SigLIP text/image similarity over event crop/frame plus filters. | Event-only exact NumPy index; not vector DB/RAG. |
| Deployment scripts/config | Four Compose stacks plus root start/status/smoke/stop. | Single-host reference deployment. |

Keep core rows before extensions.

## 13. Architecture diagram and flow plan

### 13.1 README diagram

Use one conservative GitHub-compatible `flowchart LR` with small subgraphs and a legend. Required conceptual flow:

```text
File / RTSP source --source--> C1 FFmpeg --publish RTSP--> C1 MediaMTX
C1 MediaMTX ==RTSP media :8554==> VMS MediaMTX
VMS API -.private path control :9997.-> VMS MediaMTX
VMS MediaMTX ==WHEP/WebRTC :8889==> Browser
VMS MediaMTX ==redistributed RTSP :8555==> C4 GStreamer
VMS MediaMTX ==recording==> recording volume ==playback :9996==> Browser
C4 GStreamer --> YOLO11n --> ByteTrack --> line crossing --> event DB + crop/frame
event API :8100 --> C5 polling/reconciliation + SigLIP --> C5 SQLite + NumPy
C5 --UI/API :8200--> Browser
Browser -.control/API :8080/:8090/:8100/:8200.-> service control nodes
```

Use solid/emphasized arrows for media/recording and dashed arrows for control/API. Use simple ASCII IDs/quoted labels; no HTML labels, icons, experimental syntax, or every port/table/env/test. Never connect C5 to C4 storage, show C4 owning VMS, or imply shared state/network.

### 13.2 Optional architecture diagram

At most one complementary `flowchart TB` may show four deployable boundaries, named volumes, host-published edges, and what degrades/survives if a stack stops. It must answer ownership/failure questions rather than repeat the processing pipeline. Replace it with a table if crowded.

### 13.3 Flow explanations

- **Media:** explain host versus container URLs. C1's host URL uses `localhost`; VMS/C4 use `host.docker.internal`. VMS MediaMTX branches to browser, recording/playback, and C4. FastAPI never proxies media.
- **Control/API:** all FastAPI services expose `/`, `/health`, `/docs`, `/openapi.json`; VMS alone uses private 9997; C4 recording lookup is read-only/optional; C5 uses only C4 public APIs.
- **Recording:** VMS SQLite desired state, MediaMTX writes to `genea-vms-recordings`, finalized UTC timespans, direct 9996 playback, retained volume; do not equate UI `RECORDING` with byte proof.
- **Analytics:** RTSP/TCP H.264 decode, latest-frame drop, target FPS, shared detector, per-worker tracker, normalized line, actual direction, durable artifacts, reconnect/fatal-error distinction.
- **Semantic:** overlap-safe polling/reconciliation, crop/frame embedding, immutable exact snapshot, max representation score, filters, local detail, C4 authority/C5 derived state/degraded mode.
- **Persistence/isolation:** identify each named volume/owner; no shared SQLite/files/lifecycle.

## 14. Technology and repository presentation

Use one compact stack table, versions only when helpful:

| Responsibility | Stack |
|---|---|
| APIs/UI hosting | Python 3.12, FastAPI, Uvicorn, vanilla HTML/CSS/JS |
| Simulation | FFmpeg, MediaMTX 1.20.1 |
| VMS media | MediaMTX 1.20.1, RTSP/TCP, WHEP/WebRTC, recording/playback |
| Analytics decode | GStreamer 1.24 RTSP/H.264 pipeline |
| Detection/tracking | Ultralytics YOLO11n, ByteTrack |
| Event persistence | SQLite + JPEG crop/frame |
| Retrieval | pinned SigLIP, Transformers/PyTorch, exact NumPy cosine search |
| Packaging | Docker/Compose, four independent projects, root shell wrappers |

README repository tree only:

```text
services/rtsp-simulator/    # Component 1
services/vms/               # Components 2 and 3
services/video-analytics/   # Component 4
services/semantic-search/   # Component 5
scripts/                    # root lifecycle wrappers
docs/                       # reviewer docs and engineering evidence
```

Link each service README. Do not list caches, private media, generated state, or package internals.

## 15. Prerequisites and environment setup

### 15.1 Required

- Git.
- Docker Desktop or compatible Docker Engine with Compose v2.
- Internet for first image/dependency/model build downloads.
- Modern WebRTC browser; Chromium is the validated browser.
- Adequate disk/RAM for four images and ML models. Conservatively repeat existing C5 guidance of at least 3 GiB Docker memory; do not invent a total-system minimum.
- Free ports: TCP 8080, 8090, 8100, 8200, 8554, 8555, 8889, 9996 and UDP 8189.

Optional verification/development tools: `curl`, `ffprobe`, `pytest`, Mermaid renderer. Containers supply normal runtime FFmpeg/GStreamer; host tools are unnecessary for the UI path.

### 15.2 Required local environment

Only C4 requires a service-local env for root startup:

```bash
cp services/video-analytics/.env.example services/video-analytics/.env
```

Explain that the example `CURSOR_SIGNING_KEY` is local-development-only and must be replaced outside local use. Never read/show/commit the ignored actual `.env`. C5 needs no service-local secret file for the default path.

Normal startup uses defaults. The full demo uses an inline short recording segment:

```bash
RECORDING_SEGMENT_DURATION=5s ./scripts/start-all.sh
```

Keep the five-minute production-like default out of the deterministic demo wait, but document that it remains the committed default.

## 16. Exact quick-start, lifecycle, URL, and port content

### 16.1 Quick start

```bash
git clone https://github.com/Atharvavp/Genea_VMS.git
cd Genea_VMS
cp services/video-analytics/.env.example services/video-analytics/.env
./scripts/start-all.sh
./scripts/status.sh
./scripts/smoke-test.sh
```

The clone URL is verified origin configuration, but Part 2 must not claim the remote already contains Part 2 or is publicly ready.

Shutdown/restart:

```bash
./scripts/stop-all.sh
./scripts/start-all.sh
./scripts/status.sh
```

`stop-all.sh` preserves volumes. Do not add `down -v` to reviewer instructions.

### 16.2 Dashboard/API table

| Service | Dashboard | Swagger | Health |
|---|---|---|---|
| C1 Simulator | `http://localhost:8080/` | `http://localhost:8080/docs` | `http://localhost:8080/health` |
| C2/C3 VMS | `http://localhost:8090/` | `http://localhost:8090/docs` | `http://localhost:8090/health` |
| C4 Analytics | `http://localhost:8100/` | `http://localhost:8100/docs` | `http://localhost:8100/health` |
| C5 Search | `http://localhost:8200/` | `http://localhost:8200/docs` | `http://localhost:8200/health` |

OpenAPI JSON is `/openapi.json` on each HTTP port. Link Swagger instead of manually documenting every endpoint.

### 16.3 Protocol/port table

| Port | Protocol | Owner/use | Note |
|---:|---|---|---|
| 8080 | HTTP | C1 UI/API | host-published |
| 8554 | RTSP/TCP | C1 camera routes | `/simulator/<stream_path>` |
| 8090 | HTTP | VMS UI/API | host-published |
| 8555 | RTSP/TCP | VMS redistribution | `/vms_<camera-id>` |
| 8889 | HTTP/WebRTC | WHEP live delivery | browser-facing |
| 8189 | UDP | WebRTC ICE media | browser-facing |
| 9996 | HTTP | playback | finalized media |
| 9997 | HTTP | VMS MediaMTX control | private/unpublished |
| 8100 | HTTP | C4 UI/API/images | host-published |
| 8200 | HTTP | C5 UI/API | host-published |

Selected demo/troubleshooting endpoints:

- C1 host: `rtsp://localhost:8554/simulator/<stream_path>`.
- VMS input: `rtsp://host.docker.internal:8554/simulator/<stream_path>`.
- C4 input: `rtsp://host.docker.internal:8555/vms_<camera-id>`.
- C4 events/crop/frame: `/api/events`, `/api/events/<event-id>/crop`, `/api/events/<event-id>/frame` on 8100.
- C5 readiness: `http://localhost:8200/api/index/status`.

Do not list dynamic WHEP/playback URLs as static; the UIs/API responses generate them.

## 17. Demo-guide exact flow and media policy

### 17.1 Media policy

Offer two explicit modes:

- **Core streaming/recording:** select committed `sample-320x240.mp4`. It proves media transport/live/recording/playback and cannot create object events.
- **Complete C1→C5:** upload a reviewer-owned, legally usable H.264 MP4 with a supported person or vehicle visibly crossing the frame.

The private ignored acceptance clip under `test_videos/` must not be named as a shipped fixture in reviewer-facing docs. Never download random copyrighted media or insert fake events.

### 17.2 Full deterministic walkthrough

Every numbered guide section must show **Action**, **Expected state/result**, **Wait/retry**, and **If it does not happen**.

1. **Prepare/start:** create C4 `.env`; run `RECORDING_SEGMENT_DURATION=5s ./scripts/start-all.sh`, status, smoke. Expect four ready apps and 38 smoke passes. First build may take minutes; status is readiness authority.
2. **C1:** open 8080, **Add Camera**, unique name/path, choose mounted sample or upload legal event media, H.264, loop, save, **Start**. Expect `RUNNING` and RTSP URL. If `STARTING/ERROR`, use card error/C1 logs.
3. **Optional probe:** copy host RTSP URL; when host `ffprobe` exists run `ffprobe -v error -show_streams rtsp://localhost:8554/simulator/<stream_path>`. Normal UI path does not require it.
4. **VMS register:** open 8090, **Add Camera**, unique name and container URL, Enabled + **Record continuously**, save; retain displayed `cam_...` ID.
5. **Live:** wait for source `ONLINE` and player `LIVE`; use **Focus** to show moving pixels. Allow several health/player polls; measured seconds are not an SLA. Troubleshoot source state separately from player state.
6. **Recording:** wait for `RECORDING`, then at least one 5-second segment finalization; **Recordings**, UTC date, select/play a finalized timespan. If empty, wait/reopen and confirm source/recording.
7. **C4 register:** open 8100, **Add camera**, copied VMS ID, name, `rtsp://host.docker.internal:8555/vms_<camera-id>`, defaults 5 FPS/0.25/person+vehicle/Enabled. Expect `RUNNING`; `RECONNECTING` means retry, not success.
8. **Line:** **Configure**, wait for snapshot, drag a line across actual object trajectory, name it, select `BOTH`, keep enabled, **Save line**. No snapshot means fix source/codec first.
9. **Event:** allow crossing, open **Events**, refresh, select a real event, verify metadata/full frame plus crop thumbnail. Redraw across tracked center trajectory if no event. Color bars cannot pass this step.
10. **C5 readiness:** open 8200; wait for C4 available and `searchable_events` to include/increase for the event. UI polls at 10 seconds; embedding time varies, so promise no exact time.
11. **Text:** query the observed class/description (`car` only for an actual car), optionally filter camera/class, inspect ranks. Scores are cosine similarities, not probabilities; no arbitrary rank-one promise.
12. **Image:** save real crop from C4 crop endpoint; select C5 Image, upload JPEG, search. Verify source appears. Same-crop rank 1/1.0 is accepted evidence, not a universal claim under changed data.
13. **Detail/recording:** open C5 drawer, inspect crop/frame/metadata, use recording navigation if a timespan covers the event. Not-found/upstream-unavailable is explicit, not event loss.
14. **Stop:** `./scripts/stop-all.sh`; state named-volume persistence.

README condenses optional probe and adjacent C5 actions to 12 steps; full guide keeps the above granularity.

### 17.3 Zero/reused state

- Must work with no existing cameras/events/IDs.
- On reused volumes, use unique names/paths; reuse only intentionally recognized entities.
- Never require volume deletion, SQLite edits, private acceptance artifacts, or fake events.
- Optional UI deletion of demo cameras is permissible; root shutdown is the required cleanup.

## 18. Reliability, scalability, limitations, and troubleshooting

### 18.1 Current reliability/outage behavior

Document only measured/current scope:

- C1 stop/start changes its route without taking other stacks down.
- VMS reports source versus player state separately and persists desired camera/recording state.
- C4 reconnects with backoff; fatal model/config/storage errors remain explicit.
- C4 loss does not stop core streaming/recording.
- C5 keeps local exact search/detail during C4 outage while indexing/images/recording proxy degrade; synchronization resumes automatically.
- Projects stop independently and own persistent volumes.
- Container/daemon-loss persistence was observed, not guaranteed against host/disk loss.

### 18.2 Scalability

Use two visibly separated subsections.

**Current:** single-host Compose; per-camera C1 FFmpeg; MediaMTX media plane; C4 per-camera workers/latest-frame dropping/configurable 1–10 FPS/shared CPU detector/per-camera trackers; single-node SQLite/JPEG without C4 retention; C5 asynchronous embeddings/local persistent exact NumPy index; host-published integration; four-camera C4 and C5 scale test tiers do not establish large-fleet capacity.

**Future, not implemented:** camera partitioning; GPU inference/bounded batching/replicas; external metadata DB and object storage/retention; queue/event bus and independent index/search replicas; ANN/distributed vector service only when measurements demand it; metrics/logs/traces/alerts and load/soak/chaos; orchestration/discovery/TLS/auth/secrets/network policy.

### 18.3 Known limitations

- Single-host local SQLite/files; no authentication/TLS.
- H.264 is validated practical path; H.265 publish/record does not imply browser/C4 portability.
- No credentialed physical IP camera, direct webcam adapter, amd64 host, or native Linux host-gateway acceptance.
- Chromium only for browser E2E.
- C4 one line/camera, listed classes, session-local tracking, no re-ID or event retention.
- Final unified live event exercised vehicle; person is implemented/tested but not in that final live scenario.
- C5 event search only; fixed traffic corpus is not general retrieval accuracy.
- Four quality skips require a gitignored user-owned cache; final real-C4 suite had no skips.
- Disabled-camera VMS history unavailable; playback no byte ranges; no deleted-camera orphan cleanup.
- No broad load/soak/chaos, multi-node failover, backup/restore, security, or production certification.

### 18.4 Troubleshooting scope

Cover only: Docker/Compose unavailable; missing C4 `.env`; port conflicts; first build network/resources; C1 error/route; VMS offline versus player retry; recording finalization/UTC/retention; C4 wrong VMS URL/non-H.264/no snapshot/line placement; C5 degraded/pending/proxy; host-gateway platform caveat.

Point to service-scoped log commands and READMEs. Do not create an operations manual.

## 19. Exact files to create, modify, and protect

**Create:**

- `docs/architecture.md`
- `docs/demo-guide.md`
- `docs/validation.md`
- `docs/engineering-handoffs/HANDOFF_reviewer_documentation.md`

**Move:** `PLAN_reviewer_documentation.md` to `docs/engineering/plans/PLAN_reviewer_documentation.md`.

**Modify:**

- `README.md`
- `services/vms/README.md`
- `services/video-analytics/README.md`
- `services/semantic-search/README.md`

**Do not modify:**

- any application/static/source/test/migration/model code;
- Compose, Dockerfile, MediaMTX, dependency, env-example, root script, `.gitignore`;
- C1 README absent a newly verified factual defect and user-visible plan deviation;
- existing Part 1 PRD/plan/handoffs;
- sample media;
- ignored env, venv/cache, DB/model, recordings/uploads/logs, `test_videos/`.

No documentation-support/runtime change is planned. If docs cannot remain truthful without one, stop and report exact file/change, reason docs alone fail, runtime impact, targeted tests, and rollback; await approval.

## 20. Implementation phases and phase gates

### Phase 0 — Baseline and evidence freeze

1. Run Section 3 preconditions.
2. Create `docs/reviewer-submission` from exact local SHA.
3. Move/commit this plan under engineering plans.
4. Record stale-origin facts for handoff only, not README.
5. Confirm ignored local state remains untouched.

**Gate:** ancestry correct; only plan move; no user changes at risk.

### Phase 1 — Documentation inventory and verification

1. Repeat tracked inventory/focused route/Compose/script scans.
2. Run `bash -n` on root scripts without editing them.
3. Parse all Compose files and reconfirm projects/services/volumes/ports without printing secrets.
4. Reconfirm placeholder README and exact stale README lines.
5. Reconcile final counts/limitations.

**Gate:** material conflict stops for user review; minor verified drift is incorporated and logged as a deviation.

### Phase 2 — Root README

Write exact Section 11.1 structure, assignment mapping, component distinction, quick start/endpoints/demo/choices/evidence/limits, and primary Mermaid.

**Gate:** core streaming precedes extensions; commands/URLs match code; diagram renders; no unsupported claim.

### Phase 3 — Architecture

Create Section 11.2, decision/tradeoff table, current/future scale split, and optional readable boundary diagram.

**Gate:** media/control and ownership/isolation are correct; no shared-state/vector-DB/RAG/re-ID misrepresentation.

### Phase 4 — Demo guide

Create Section 11.3 using exact UI labels/URL forms and explicit bundled-versus-user-media policy.

**Gate:** zero-state reviewer needs no hidden data/IDs/private files/fake events.

### Phase 5 — Validation document

Create Section 11.4, reconcile evidence, and disclose untested areas.

**Gate:** arithmetic correct; skips visible; every result has scope/source.

### Phase 6 — Minimal service README corrections

Apply only Section 11.5 edits.

**Gate:** no behavior/narrative rewrite; corrected links resolve.

### Phase 7 — Documentation validation

Run Sections 21–24. Use unique demo names on existing volumes; do not delete user data. Fix docs errors only and record actual results.

**Gate:** every acceptance check passes or is explicitly blocking; no hidden best-effort status.

### Phase 8 — Final Part 2 handoff

Create handoff from actual results, rerun links/safety/diff/status, commit, and report branch/final SHA. Do not push.

**Gate:** clean tracked worktree, planned commits only, no runtime artifacts, ready for user review.

## 21. Command, Compose, and URL verification

Run from repo root. Protect the ignored Analytics env; never overwrite/print it.

### 21.1 Static and Compose

```bash
bash -n scripts/start-all.sh scripts/status.sh scripts/smoke-test.sh scripts/stop-all.sh

for service_dir in \
  services/rtsp-simulator \
  services/vms \
  services/video-analytics \
  services/semantic-search
do
  docker compose -f "$service_dir/docker-compose.yml" config --quiet
  docker compose -f "$service_dir/docker-compose.yml" config --services
  docker compose -f "$service_dir/docker-compose.yml" config --volumes
done
```

Expected names are Section 6.2. Analytics config needs ignored `.env`; create it only with documented copy if absent.

### 21.2 Root lifecycle

```bash
test -f services/video-analytics/.env || \
  cp services/video-analytics/.env.example services/video-analytics/.env

./scripts/stop-all.sh
RECORDING_SEGMENT_DURATION=5s ./scripts/start-all.sh
./scripts/status.sh
./scripts/smoke-test.sh
./scripts/stop-all.sh
./scripts/start-all.sh
./scripts/status.sh
```

This validates stop/start/status/smoke while preserving volumes, then restores normal default configuration/running state observed at planning. If the user's desired terminal state is stopped, end with stop and record it. Never use `down -v`.

Expected smoke is 38/0. If it differs, investigate drift and update evidence; do not copy the old number blindly.

### 21.3 HTTP

```bash
for port in 8080 8090 8100 8200; do
  for path in / /health /docs /openapi.json; do
    curl -fsS -o /dev/null "http://localhost:${port}${path}"
  done
done

curl -fsS http://localhost:8200/api/index/status
```

Run status first and record HTTP/health meaning, particularly C5 upstream state. Never copy local camera/event payloads or secrets into docs.

### 21.4 Demo accuracy

Run Section 17 using a legally usable event clip when available. If none is available to Claude, do not download/fabricate: verify bundled core streaming/recording, mark the new real-event walkthrough as not rerun, and cite final accepted Part 1 C4/C5 evidence. Record this in handoff.

## 22. Link and Mermaid validation

### 22.1 Links

Use a Python 3 standard-library ephemeral checker (or equivalent installed tool) to extract Markdown destinations, ignore anchors/HTTP/mail, strip fragments, resolve relative paths from each source file, and fail for missing files. Cover:

```text
README.md
docs/architecture.md
docs/demo-guide.md
docs/validation.md
docs/engineering-handoffs/HANDOFF_reviewer_documentation.md
services/rtsp-simulator/README.md
services/vms/README.md
services/video-analytics/README.md
services/semantic-search/README.md
```

Do not commit a checker/dependency/CI workflow. Manually inspect heading-anchor links in GitHub-compatible preview.

### 22.2 Mermaid

For each Mermaid fence:

1. Extract to a `mktemp -d` directory.
2. Render with installed Mermaid CLI or ephemeral `npx --yes @mermaid-js/mermaid-cli`.
3. Inspect SVG/PNG for clipped labels, edge crossings, contrast, and flow readability.
4. Preview GitHub-compatible Markdown.
5. Remove only the temporary directory.

If network/tool policy blocks CLI, use GitHub-compatible preview plus Mermaid Live syntax validation and record the fallback. A fence-presence check alone is insufficient.

## 23. Safety, artifact, terminology, and Git validation

### 23.1 Stale wording

```bash
rg -n 'HANDOFF_component|own clone|VMS clone|separate clone' \
  services/*/README.md README.md docs/architecture.md docs/demo-guide.md docs/validation.md
```

Expected no stale link/clone claim; accurately scoped historical version wording may remain.

### 23.2 Private media and local paths

Scan reviewer surfaces, excluding this archived plan which must record the policy:

```bash
rg -n 'test_videos/|traffic_1080p_video_demo\.mp4|/Users/|/home/[^ )]+|[A-Za-z]:\\\\' \
  README.md docs/architecture.md docs/demo-guide.md docs/validation.md services/*/README.md
```

Expected no private fixture/path or developer-local absolute path. Correctly explained container paths such as `/data/...` are allowed.

### 23.3 Secrets and runtime artifacts

```bash
git diff --cached --name-only
git ls-files | rg '(^|/)(\.env|__pycache__|test_videos)(/|$)|\.(db|sqlite|log|pyc)$' || true
git status --short --ignored
rg -n -i '(BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|AKIA[0-9A-Z]{16}|password\s*[:=]\s*[^<])' \
  README.md docs/architecture.md docs/demo-guide.md docs/validation.md \
  docs/engineering-handoffs/HANDOFF_reviewer_documentation.md services/*/README.md || true
```

Review staged names manually. `.env.example` may be tracked; actual `.env`, cache/model/upload/recording/DB/log/private-video artifacts must not be.

### 23.4 Terminology/claims

Search for and correct:

- “five services” or “four components”;
- shared DB/network claims unless explicitly negated;
- RAG/vector DB/re-identification/production-ready except explicit negation;
- “all tests passed” instead of exact totals/skips;
- any-codec/browser/camera claims;
- “instant,” absolute recovery/latency, or future scale features in present tense.

### 23.5 Final Git checks

```bash
git diff --check
git diff --stat 52029245cf084b6e26578814ce5bca3480787782...HEAD
git diff --name-status 52029245cf084b6e26578814ce5bca3480787782...HEAD
git log --oneline --decorate 52029245cf084b6e26578814ce5bca3480787782..HEAD
git status --short --branch
```

Changed paths must be exactly Section 19. Tracked worktree clean after final commit; ignored developer state may remain.

## 24. Rollback and failure strategy

- Before commit, correct mistaken documentation with a scoped patch; do not destructively reset/checkout user work.
- Phase commits should be independently revertible with `git revert <commit>`.
- Restore only a disproven README sentence/link in a corrective commit.
- Simplify a failing Mermaid or replace only the optional second diagram with a table; add no pipeline.
- If commands conflict with docs, document verified behavior; do not fix runtime without approval.
- Lifecycle checks retain volumes and must restore the prior running/stopped state; never delete data as rollback.
- If an accidental runtime change occurs, stop/disclose its exact diff and ask for direction.

Block and ask the user if:

- local `main` lacks required Part 1 ancestry;
- another change overlaps a planned file;
- docs branch exists with divergent work;
- code contradicts a frozen requirement and docs cannot reconcile it;
- any runtime/support change seems required;
- final counts cannot reconcile without inference;
- a secret/private/runtime artifact is staged;
- primary Mermaid cannot render;
- a reviewer command requires destructive loss/undocumented credentials;
- only copyrighted/private media or fabricated rows could demonstrate events.

No new event clip is not a blocker: publish honest bring-your-own-media instructions and accepted historical event evidence.

## 25. Acceptance gates

Part 2 is complete only when:

1. Branch is `docs/reviewer-submission` from exact Part 1 SHA.
2. Four primary reviewer docs exist and retain distinct roles.
3. README leads with streaming/recording and is understandable in about five minutes.
4. Five logical components versus four deployables is unmistakable.
5. Assignment map covers capabilities, deliverables, testing/reliability, scale, and extensions with caveats.
6. Media, recording, analytics, semantic, control/API flows and ownership are accurate.
7. At least one and at most two Mermaid diagrams render.
8. Quick start uses actual root workflow/C4 env.
9. dashboard, health, Swagger/OpenAPI, port, RTSP, selected API URLs verify.
10. Demo is zero-state-capable and independent of hidden/private/fake data; rerun scope is disclosed.
11. Validation preserves exact totals/skips and distinguishes Part 1 evidence from Part 2 checks.
12. Reliability/scale separates measured current behavior from unimplemented future work.
13. Security/platform/browser/camera/codec/retention/retrieval/test limitations are present.
14. Only listed service README facts/links changed.
15. Link, Mermaid, command, URL, safety, terminology, diff, and status gates pass.
16. No application/runtime/config/test/media change exists.
17. Final Part 2 handoff records actual results.
18. Tracked worktree is clean, unpushed, and ready for review.

## 26. Claude final handoff requirements

The final response and handoff must state:

- branch, start/final SHA, commits;
- files created/moved/modified;
- no non-doc changes, or exact approved exception;
- README structure/reviewer journey;
- diagrams and validation method;
- demo/media policy and actual rerun scope;
- evidence categories/exact totals;
- service README corrections;
- root commands/exact results;
- dashboard/health/Swagger URLs checked;
- link/Mermaid/safety/Git results;
- deviations/reasons;
- limitations/open questions;
- Part 3 readiness without doing Part 3.

Do not call work complete while a required failure is only noted.

## 27. Part 3 boundary

Do not add work/phases for screenshots, screenshot automation, Loom, pushing/merging/publishing, public-visibility/final-remote review, fresh clone from remote, release/tag, package, or email. Docs should accept later screenshots via normal Markdown without visible TODO placeholders.

## 28. Open questions requiring user input

There are no blocking Part 2 questions.

Deferred to Part 3 and non-blocking:

1. Screenshot selection/placement.
2. Push/merge timing, public visibility, and final remote certification.
3. Whether the user will later supply a redistributable event-generating fixture; until then use bring-your-own legal H.264 media.
4. Loom scope/order and submission packaging.

If no answers arrive during Part 2, implement this exact documentation-only plan.
