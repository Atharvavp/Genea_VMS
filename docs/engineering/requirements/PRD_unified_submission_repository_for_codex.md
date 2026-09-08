# Unified Submission Repository PRD — Branch Consolidation, Monorepo Refactor & Reviewer-Ready Packaging

**Document type:** Product Requirements Document for Codex Plan Mode  
**Target:** Final submission repository unification / Part 1  
**Planning agent:** Codex  
**Implementation agent after plan review:** Claude Code  
**Date:** 2026-09-08  
**Status:** Product requirements frozen; repository investigation and implementation planning still required

---

## 1. Purpose and required workflow

This PRD defines the requirements for consolidating the independently implemented Genea assignment components into one clean, reviewer-friendly default repository structure without changing the verified behavior of the applications.

This is deliberately a **requirements document, not an implementation plan**.

Required workflow:

1. Codex reads this PRD completely.
2. Codex inspects the actual Git repository, all relevant branches, branch ancestry, HEADs, working-tree state, Compose files, Dockerfiles, environment files, test layouts, scripts, generated artifacts, and current `main`.
3. Codex reads the final engineering handoffs for Components 1–5 completely.
4. Codex verifies which branch/commit is the final source of truth for each deployable service.
5. Codex confirms the exact current runtime and networking contracts before proposing any relocation.
6. Codex verifies how Docker Compose project names, build contexts, bind mounts, relative paths, volumes, ports, and host networking behave after relocation.
7. Codex creates one implementation-ready `PLAN_unified_submission.md`.
8. The plan is reviewed before Claude Code implementation.
9. Claude implements the approved plan phase-by-phase.
10. Claude performs full validation against the unified repository.
11. Only after the unified branch passes the required gates should it be merged into `main`.
12. Final reviewer-facing documentation and submission proof belong to later phases and must not be mixed into this refactor unless required to make the unified repository operable.

The plan must classify material claims as one of:

- **VERIFIED FACT**
- **FROZEN REQUIREMENT**
- **CODEX IMPLEMENTATION DECISION**
- **ASSUMPTION**
- **RUNTIME VALIDATION REQUIRED**
- **OPEN QUESTION REQUIRING USER INPUT**

Codex must not silently turn assumptions into facts.

---

## 2. Product problem

Components 1–5 were built incrementally and verified independently, but the implementation is currently distributed across feature branches / working clones.

That development structure was useful during implementation because each component could evolve and be validated in isolation. It is not the desired submission structure.

A reviewer should not need to:

- inspect multiple branches;
- know which branch supersedes which;
- clone the same repository several times;
- reconstruct component dependencies manually;
- infer ports or startup ordering from handoff files;
- understand development-only branch organization before running the system.

The final repository must therefore consolidate the verified implementations into a single clean monorepo while preserving the original runtime boundaries.

The target conceptual system remains:

```text
Component 1
RTSP Simulator
    │
    │ RTSP
    ▼
Components 2 + 3
VMS / MediaMTX
    ├── WebRTC live view
    ├── RTSP redistribution
    └── recording / playback
            │
            │ RTSP + read-only API
            ▼
Component 4
Video Analytics
    ├── GStreamer
    ├── YOLO
    ├── ByteTrack
    ├── directional events
    ├── crop.jpg
    └── frame.jpg
            │
            │ public read-only HTTP
            ▼
Component 5
Semantic Search
    ├── SigLIP
    ├── SQLite
    ├── NumPy vector search
    └── text/image event retrieval
```

The final repository must make this architecture easier to understand and operate without making the applications more tightly coupled.

---

## 3. Core invariant

> **This work is a repository unification and submission-packaging refactor, not a functional redesign.**

The unified repository must preserve the verified behavior of the component implementations.

Permitted changes include only those required to relocate, integrate, operate, validate, and package the existing services cleanly.

Examples of acceptable change surfaces:

- repository paths;
- Compose build contexts;
- Dockerfile `COPY` paths if relocation requires them;
- relative file references;
- Compose project names;
- environment file paths;
- bind-mount paths;
- test paths;
- script paths;
- import-independent execution paths;
- root orchestration scripts;
- root-level configuration that is genuinely shared;
- root-level ignore rules;
- validation tooling for the unified repository;
- engineering handoff document relocation;
- non-functional repository hygiene.

Examples of forbidden redesign unless a verified relocation defect makes a minimal change unavoidable:

- API contract redesign;
- database schema redesign;
- event model redesign;
- camera model redesign;
- recording semantics change;
- MediaMTX behavior change;
- WebRTC behavior change;
- inference scheduling redesign;
- tracking redesign;
- line-crossing redesign;
- semantic ranking redesign;
- embedding model change;
- vector-index technology change;
- service ownership changes;
- shared database introduction;
- shared filesystem introduction;
- direct cross-component imports;
- replacing public HTTP contracts with internal imports;
- replacing existing host networking with a new common application network merely for neatness;
- introducing Kubernetes, service discovery, reverse proxies, gateways, or message brokers;
- adding auth, RBAC, HA, observability stacks, or production infrastructure.

When relocation exposes a real bug, Codex must document it explicitly and propose the smallest possible fix.

---

## 4. Frozen source-of-truth mapping

The final unified implementation must be sourced from the following final component branches.

| Final service | Component scope | Source branch |
|---|---|---|
| RTSP Simulator | Component 1 | `feat/rtsp_simulation` |
| VMS | Components 2 + 3 | `feat/vms-recording` |
| Video Analytics | Component 4 | `feat/video_analytics` |
| Semantic Search | Component 5 | `feat/image_retrival` |

### Important rule for Components 2 and 3

`feat/vms-recording` is the final source of truth for the VMS service.

Component 3 extends Component 2. The final repository must therefore contain **one VMS service**, not separate live-view and recording services.

Do not merge `feat/vms` independently into the final tree and then layer `feat/vms-recording` on top unless Git inspection proves that this is required for correctness. Prefer the final Component 3 branch snapshot as the VMS implementation source.

The original feature branches must remain available as development history.

---

## 5. Required final branch workflow

Do not perform consolidation directly on `main`.

Frozen workflow:

```text
existing branches
      ↓
refactor/unified-submission
      ↓
full validation
      ↓
review
      ↓
merge to main
```

Requirements:

- create `refactor/unified-submission`;
- record the exact source commit SHA for every imported service;
- perform all structural work there;
- do not rewrite or delete original feature branches;
- do not force-push or rewrite useful existing history;
- merge to `main` only after acceptance gates pass;
- final `main` becomes the reviewer-facing default branch.

Codex must inspect current branch ancestry before deciding whether the unification should use Git merges, tree extraction, `git archive`, `git checkout <branch> -- <paths>`, temporary worktrees, `rsync`, or another conservative technique.

Preserving the feature-branch history is more important than preserving perfect per-file `git log --follow` continuity after files are relocated.

---

## 6. Target repository structure

The preferred final structure is:

```text
Genea_VMS/
│
├── README.md                         # reviewer-facing, completed in Part 2
├── .gitignore
├── .env.example                     # shared/root values only, if actually needed
│
├── services/
│   ├── rtsp-simulator/
│   │   ├── docker-compose.yml
│   │   ├── .env.example
│   │   ├── README.md
│   │   └── ...
│   │
│   ├── vms/
│   │   ├── docker-compose.yml
│   │   ├── .env.example
│   │   ├── README.md
│   │   └── ...
│   │
│   ├── video-analytics/
│   │   ├── docker-compose.yml
│   │   ├── .env.example
│   │   ├── README.md
│   │   └── ...
│   │
│   └── semantic-search/
│       ├── docker-compose.yml
│       ├── .env.example
│       ├── README.md
│       └── ...
│
├── scripts/
│   ├── start-all.sh
│   ├── stop-all.sh
│   ├── status.sh
│   └── reset-all.sh                 # only if implemented safely and clearly
│
└── docs/
    └── engineering-handoffs/
        ├── component-1.md
        ├── component-2.md
        ├── component-3.md
        ├── component-4.md
        └── component-5.md
```

Codex may refine names if repository inspection shows a concrete reason, but the final structure must remain:

- shallow;
- obvious;
- service-oriented;
- easy to navigate;
- independent by component;
- reviewer-friendly.

Avoid excessive nested directories.

---

## 7. Deployable-service boundaries

The unified repository has four deployable stacks.

### 7.1 RTSP Simulator

Owns:

- simulator API/UI;
- SQLite simulator state;
- source files/uploads;
- FFmpeg publishers;
- simulator MediaMTX;
- simulator RTSP endpoint.

It remains an independent RTSP source and must not import or depend on VMS code.

### 7.2 VMS

Owns:

- camera registration;
- desired state;
- VMS SQLite state;
- VMS-owned MediaMTX;
- RTSP pulling;
- WebRTC/WHEP delivery;
- recording;
- recording discovery;
- historical playback;
- retention configuration.

Components 2 and 3 live together here.

### 7.3 Video Analytics

Owns:

- analytics camera configuration;
- GStreamer workers;
- YOLO inference;
- ByteTrack tracking;
- crossing logic;
- analytics SQLite state;
- event persistence;
- event crop/frame artifacts;
- analytics UI/API.

It consumes VMS outputs through existing external contracts only.

### 7.4 Semantic Search

Owns:

- C4 event discovery;
- SigLIP inference;
- semantic SQLite state;
- persisted embeddings;
- in-memory NumPy search snapshot;
- text/image retrieval;
- search UI/API.

It consumes Component 4 through the existing public HTTP API only.

---

## 8. Independent Compose requirement

Every final service must preserve its own Compose stack.

A reviewer must be able to run any service from its own directory using the service-local Compose definition.

Conceptually:

```bash
cd services/rtsp-simulator
docker compose up --build
```

```bash
cd services/vms
docker compose up --build
```

```bash
cd services/video-analytics
docker compose up --build
```

```bash
cd services/semantic-search
docker compose up --build
```

Requirements:

- each stack retains an explicit and collision-safe Compose project identity where needed;
- one stack's `docker compose down` must not remove containers from another stack;
- one stack's `docker compose down -v` must not delete another stack's volumes;
- no shared project name may accidentally cause orphan cleanup across services;
- no root orchestration requirement may make service-local operation stop working;
- existing service-local test commands should remain available.

A single giant root Compose file is **not required** and should not be introduced merely for aesthetics.

---

## 9. Root orchestration requirement

The unified repository should provide a convenience path for running the complete system.

Preferred interface:

```bash
./scripts/start-all.sh
./scripts/status.sh
./scripts/stop-all.sh
```

A destructive reset helper may exist only if it is explicit:

```bash
./scripts/reset-all.sh
```

### Start behavior

`start-all.sh` should:

1. validate obvious prerequisites;
2. start stacks in dependency order;
3. avoid changing service-local configuration unnecessarily;
4. wait for meaningful readiness where practical;
5. fail clearly when a dependency fails;
6. print the URLs/ports of started services;
7. remain idempotent when run twice;
8. never silently wipe existing volumes.

Dependency order:

```text
RTSP Simulator
      ↓
VMS
      ↓
Video Analytics
      ↓
Semantic Search
```

The script may start downstream services even when they can technically run in degraded mode, but Codex must choose and document the most reviewer-friendly behavior.

### Stop behavior

`stop-all.sh` should:

- stop in reverse dependency order;
- preserve data by default;
- not delete volumes;
- not stop unrelated Docker projects;
- be safe to rerun.

### Status behavior

`status.sh` should ideally show:

- container/Compose state;
- health endpoint state where available;
- service URLs;
- degraded/unavailable upstream state distinctly where meaningful.

Keep scripts small. They are convenience wrappers, not a new orchestration platform.

---

## 10. Existing networking contracts must be preserved

This refactor must not redesign networking merely because all code now lives in one repository.

Preserve the tested cross-stack communication contracts.

Examples include:

- VMS consuming simulator RTSP through the existing published host endpoint;
- analytics consuming VMS RTSP through the existing VMS-published RTSP endpoint;
- analytics calling the existing VMS API where already implemented;
- semantic search calling Component 4 over its existing public HTTP endpoint.

Do not replace these with:

- direct Python imports;
- shared containers;
- shared databases;
- shared Compose application network;
- private service-name DNS;
- Unix sockets;
- new proxies/gateways;

unless repository inspection demonstrates a concrete blocker and the user approves the change.

### `host.docker.internal`

Existing usage of `host.docker.internal` must be preserved if that is what the verified implementation depends on.

Codex must inspect Linux compatibility already present in Compose files, such as `host-gateway` mappings, and preserve them if used.

---

## 11. Frozen public ports

The final submission should preserve the existing demonstrated public ports unless a real collision is found during repository inspection.

| Service / capability | Default host port |
|---|---:|
| Simulator UI/API | `8080` |
| Simulator RTSP | `8554` |
| VMS UI/API | `8090` |
| VMS RTSP | `8555` |
| VMS WebRTC/WHEP HTTP | `8889` |
| VMS ICE UDP | `8189` |
| VMS playback | `9996` |
| Analytics UI/API | `8100` |
| Semantic Search UI/API | `8200` |

Internal-only ports must remain internal where currently designed.

Do not introduce dynamic port allocation or a root port abstraction for submission convenience.

Service-level environment overrides may remain as already supported.

---

## 12. Persistence and volume isolation

Each component must retain its own persistent state.

Frozen invariant:

> **No shared application database and no shared application data volume across independent services.**

Expected ownership:

```text
RTSP Simulator
  simulator DB
  source uploads / sample media state

VMS
  VMS DB
  recordings

Video Analytics
  analytics DB
  event images

Semantic Search
  semantic DB
  vectors / local derived state
```

Requirements:

- use distinct named volumes / bind mounts;
- relocation must not accidentally point two services at the same `/data`;
- no component may mount another component's private data merely because the repository is unified;
- existing failure-isolation guarantees must remain true;
- persistence must survive normal `docker compose down` / `up`;
- reset behavior must be explicit and service-scoped or clearly global.

---

## 13. Configuration policy

Do not create one giant root configuration surface.

### Service-local configuration

Each service should retain its own `.env.example` and service-local settings.

Those remain the source of truth for service-specific configuration.

### Root configuration

A root `.env.example` may contain only settings that are genuinely needed to coordinate or simplify full-stack operation.

Examples of potentially valid root-level values:

- externally published ports if root scripts consume them;
- upstream base URLs if wrapper scripts need them;
- optional host bind paths if shared by the full-demo workflow.

Do not duplicate every service variable at root.

Avoid two conflicting sources of truth.

Codex must map every existing environment variable that depends on path, hostname, port, or build context and determine whether relocation requires adjustment.

---

## 14. Relocation rules

Repository relocation is allowed to change file paths but must not change semantics.

Codex must inspect and plan updates for:

- Docker `build.context`;
- Dockerfile paths;
- `COPY` directives;
- Compose `env_file`;
- Compose `volumes`;
- bind mounts;
- relative config file references;
- sample-media paths;
- model-download/build script paths;
- test fixture paths;
- shell script working directories;
- Playwright paths;
- lock-file paths;
- generated-file paths;
- handoff / README links;
- static asset paths;
- third-party notices;
- path assumptions in tests;
- Python package installation paths.

Do not use absolute developer-machine paths in the final repository.

---

## 15. Git and provenance requirements

Before moving code, Codex must create a provenance table containing:

- component;
- source branch;
- source HEAD SHA;
- source working-tree cleanliness;
- source directory/tree summary;
- source test command;
- source Compose project name;
- source public ports;
- source persistent volumes.

The implementation plan must preserve enough information that a reviewer can determine where each final service came from.

Suggested final record:

```text
services/rtsp-simulator/    ← feat/rtsp_simulation @ <sha>
services/vms/               ← feat/vms-recording @ <sha>
services/video-analytics/   ← feat/video_analytics @ <sha>
services/semantic-search/   ← feat/image_retrival @ <sha>
```

Do not copy generated runtime data from development clones.

Do not copy `.git` directories from temporary worktrees/clones.

---

## 16. Engineering handoff preservation

The detailed Component 1–5 engineering handoffs are valuable audit evidence.

They should be preserved under:

```text
docs/engineering-handoffs/
```

Preferred names:

```text
component-1-rtsp-simulator.md
component-2-vms-live-view.md
component-3-recording-playback.md
component-4-video-analytics.md
component-5-semantic-search.md
```

Requirements:

- preserve technical content;
- update only links/paths that are factually broken after relocation;
- do not rewrite validation history as if it happened in the unified repository;
- clearly distinguish historical component validation from new unified-repository validation.

PRDs and implementation plans are not required to be reviewer-facing in the root.

Codex should recommend whether to include them under a deeper engineering docs directory or leave them out of the default submission tree.

---

## 17. Existing functional contracts that must not change

### 17.1 Simulator

Preserve:

- FastAPI/UI behavior;
- camera CRUD/lifecycle;
- FFmpeg publishing;
- simulator MediaMTX;
- RTSP/TCP publishing;
- SQLite persistence;
- upload/local-source behavior;
- currently verified validation and security semantics.

### 17.2 VMS

Preserve:

- camera CRUD;
- enable/disable;
- health derivation;
- MediaMTX reconciliation;
- stable MediaMTX path behavior;
- WebRTC/WHEP browser delivery;
- recording preference;
- MediaMTX-native recording;
- playback;
- retention;
- recording/history state separation;
- private MediaMTX Control API;
- browser compatibility behavior already documented.

### 17.3 Analytics

Preserve:

- standalone service;
- H.264 RTSP/TCP ingest;
- GStreamer → appsink;
- latest-frame/drop behavior;
- shared CPU detector;
- ByteTrack per worker/session;
- directional line-crossing;
- event dedupe;
- atomic event durability;
- VMS read-only boundaries;
- one-Uvicorn-worker assumption;
- current CPU/failure semantics.

### 17.4 Semantic Search

Preserve:

- standalone service;
- Component 4 public HTTP only;
- exact frozen SigLIP model/revision;
- offline runtime model behavior;
- SQLite durable vectors;
- immutable NumPy search snapshot;
- crop + frame event representation;
- event-level max-score aggregation;
- text search;
- image search;
- metadata filters;
- polling/backfill/reconciliation;
- C4 outage search continuity;
- one-process lock behavior;
- existing security constraints.

---

## 18. Failure-isolation requirements

The repository refactor must preserve the ability for services to fail independently.

Required acceptance examples:

- stopping Simulator must not stop VMS, analytics, or semantic-search containers;
- a missing simulator source may make a VMS camera offline but must not crash VMS;
- stopping VMS must not corrupt analytics or semantic-search local state;
- stopping analytics must not affect VMS live view or recording;
- stopping semantic search must not affect Components 1–4;
- C4 outage must still allow C5 local semantic search over already indexed data;
- one stack's `docker compose down` must not stop another stack;
- one stack's volume removal must not remove another stack's state.

---

## 19. Root-level smoke validation

Add a lightweight unified-repository validation layer.

This is not a replacement for component test suites.

Codex must design the smallest useful smoke/acceptance mechanism that proves the four relocated stacks can coexist.

Possible checks:

- root structure exists;
- service Compose files parse;
- Compose project names are distinct;
- required host ports do not collide;
- each service health endpoint responds as expected;
- simulator RTSP is reachable when configured;
- VMS can ingest simulator RTSP;
- VMS RTSP output is reachable by analytics;
- analytics API is reachable;
- semantic search can reach analytics API;
- stopping one optional downstream service leaves upstream health intact.

The smoke test must not depend on hidden local state.

---

## 20. Existing test suites must be preserved

Relocation must not weaken the existing validation.

Requirements:

- preserve unit tests;
- preserve integration tests;
- preserve E2E tests;
- preserve real-model tests;
- preserve scale tests;
- preserve browser tests;
- preserve runtime validation scripts;
- preserve security/static checks;
- update only paths/invocation assumptions required by relocation.

Forbidden:

- deleting tests because paths became inconvenient;
- loosening assertions to make relocation pass;
- adding broad skips;
- replacing real integration tests with mocks;
- silently dropping difficult test tiers.

If a test cannot run after relocation, Codex must explain why and classify the issue before implementation proceeds.

---

## 21. Required clean-state behavior

The unified repository must not depend on developer-machine residue.

Codex must plan a clean-state verification that considers:

- no existing application containers;
- no old Compose projects;
- no old component volumes where practical;
- no local SQLite DB;
- no generated recordings;
- no analytics events;
- no semantic index;
- no Python virtual environment assumption;
- no local source-code sibling clones;
- no absolute `/Users/...` path;
- no pre-existing model cache relied upon at runtime.

Component 5 build-time model acquisition may legitimately use the network according to its existing design; runtime remains offline with respect to model acquisition.

---

## 22. Repository hygiene requirements

The final unified branch must not contain accidental development residue.

Codex must inspect for:

- `.env`;
- credentials;
- API tokens;
- Hugging Face tokens;
- passwords;
- secret files;
- `.DS_Store`;
- `__pycache__`;
- `.pytest_cache`;
- `.mypy_cache`;
- `.ruff_cache`;
- `.venv`;
- test output;
- screenshots not intentionally included;
- Playwright traces/videos;
- generated SQLite DBs;
- recordings;
- event image stores;
- semantic caches;
- downloaded model cache outside the intentional Docker build mechanism;
- logs;
- temporary benchmark artifacts;
- temporary Docker exports;
- local absolute paths;
- editor files.

Required final checks include at minimum:

```bash
git status --short
git diff --check
git ls-files
```

plus targeted secret/path scanning.

Codex must choose commands appropriate to the repository and document them.

---

## 23. Security boundaries

This refactor must not weaken existing security properties.

Preserve where currently implemented:

- non-root containers;
- capability drops;
- no-new-privileges;
- private internal APIs;
- sanitized RTSP credentials;
- no Docker socket;
- bounded image upload;
- no arbitrary URL fetch;
- no cross-component private volume mounts;
- no runtime model credential requirement;
- safe static/frontend rendering;
- no stack traces or raw secrets in public errors.

Moving all source into one Git repository must not imply runtime trust-sharing between containers.

Repository colocation is not permission for code-level or data-level coupling.

---

## 24. Root reset semantics

If `scripts/reset-all.sh` is implemented:

- it must be clearly marked destructive;
- it must require an explicit confirmation flag or obvious invocation;
- it must delete only Genea assignment containers/volumes;
- it must not prune unrelated Docker data;
- it must not use broad commands such as `docker system prune`;
- it must print what will be removed;
- it should ideally support service-specific reset.

If safe semantics cannot be implemented simply, omit this script.

---

## 25. Startup and readiness semantics

Codex must inspect the real health endpoints before freezing root orchestration.

Do not invent readiness probes.

The plan must define:

- what "started" means for each stack;
- what "ready" means for each stack;
- expected degraded states;
- startup timeout;
- how errors are surfaced;
- whether downstream startup waits for upstream readiness;
- which health endpoints return 200 vs 503 during degraded operation.

Important distinction:

- container running;
- local application ready;
- upstream dependency available;
- media source online;

are not always the same state.

Root orchestration must not collapse them into one misleading binary.

---

## 26. Compose project naming

Because multiple stacks exist under one repository, explicit Compose project names are required wherever directory-derived names could collide or become ambiguous.

Codex must verify current names and freeze unique project names.

Preferred conceptual names:

```text
genea-simulator
genea-vms
genea-analytics
genea-semantic-search
```

The exact names may follow existing verified files.

Requirement:

> `docker compose down --remove-orphans` inside one service must never remove another service's containers.

This must be tested.

---

## 27. No hidden monorepo coupling

A monorepo is an organizational container, not a runtime dependency.

Forbidden new coupling includes:

- analytics importing VMS Python modules;
- semantic search importing analytics models;
- services reading sibling SQLite databases;
- services reading sibling `/data` directories;
- direct filesystem reads of another service's generated artifacts;
- shared Python environment across runtime containers;
- shared application process;
- shared Uvicorn instance;
- shared MediaMTX merely because it reduces service count.

The existing external contracts remain the integration boundaries.

---

## 28. Compatibility requirements

The current verified target is Apple Silicon / Docker Desktop.

The refactor must preserve that.

Codex must also avoid making Linux support worse where existing Compose files already account for `host.docker.internal` / `host-gateway`.

Do not claim amd64 validation unless it is actually run.

Do not claim Linux-host validation unless it is actually run.

These can remain documented runtime validations.

---

## 29. Explicit non-goals

Do not add as part of this work:

- new features;
- UI redesign;
- root dashboard;
- unified frontend;
- reverse proxy;
- API gateway;
- Nginx/Traefik/Caddy;
- shared authentication;
- single sign-on;
- shared database;
- shared Redis;
- message broker;
- Kubernetes;
- Helm;
- Terraform;
- cloud deployment;
- CI/CD pipeline unless already present and only relocation repair is required;
- centralized logging;
- Prometheus/Grafana;
- OpenTelemetry;
- new AI models;
- GPU acceleration;
- video transcoding;
- cross-camera re-ID;
- RAG/LLM;
- semantic-search changes;
- recording redesign;
- schema modernization;
- Python monorepo tooling;
- Node workspace tooling;
- dependency unification across services.

Do not turn this into a platform-engineering exercise.

---

## 30. Codex repository investigation requirements

Before writing the implementation plan, Codex must inspect and report:

### Git

- current branch;
- current `main`;
- `git status`;
- all relevant branches;
- HEAD SHA for each;
- ancestry relationships;
- whether branches share a common base;
- whether C3 contains C2 as expected;
- whether any relevant work is uncommitted.

### Repository trees

For every source branch:

- top-level files;
- Compose files;
- Dockerfiles;
- environment files;
- source tree;
- test tree;
- scripts;
- documentation;
- volumes/bind paths;
- generated artifacts accidentally tracked.

### Compose

For every stack:

- Compose project name;
- services;
- container names if any;
- networks;
- volumes;
- ports;
- `extra_hosts`;
- healthchecks;
- build contexts;
- dependencies;
- `env_file`;
- relative bind mounts;
- privileged/capability settings.

### Runtime contracts

Verify:

- simulator published RTSP URL;
- VMS expected simulator URL;
- VMS published RTSP URL/path;
- analytics expected VMS RTSP URL;
- analytics VMS API base if used;
- semantic-search C4 API base;
- all browser URLs;
- all Swagger URLs.

### Tests

Map exact commands for:

- C1 tests;
- C2/C3 tests;
- C4 tests;
- C5 tests;
- browser E2E;
- real-model tests;
- scale tests;
- integration tests.

### Disk/build impact

Check:

- large Docker build contexts;
- model artifacts;
- sample media;
- recordings;
- caches;
- ignored data;
- whether copying trees into `services/` accidentally bloats build context.

---

## 31. Required Codex decision: unification mechanism

Codex must compare the practical consolidation approaches and freeze one.

Possible strategies include:

### A. Merge branches then relocate

Potential benefit:

- history preserved more directly.

Potential risk:

- conflicting top-level trees from independently developed branches;
- C4/C5 branches started from minimal base;
- difficult merge conflicts;
- unnecessary contamination.

### B. Import verified branch snapshots into service directories

Potential benefit:

- explicit source-to-target mapping;
- low risk of semantic conflict;
- clean final tree.

Potential trade-off:

- per-file history less direct in unified branch.

### C. Git subtree / history-preserving import

Potential benefit:

- better history.

Potential risk:

- more complex than needed for a take-home submission.

Codex must choose the safest method based on actual Git history, not preference.

The user preference is:

> preserve original feature branches as historical evidence; prioritize a clean final repository over perfect relocated-file history.

---

## 32. Required Codex planning output

Create **one** self-contained:

```text
PLAN_unified_submission.md
```

It must include at minimum:

1. executive summary;
2. verified repository path;
3. verified current branch/HEAD/status;
4. all relevant branch HEADs;
5. source-of-truth hierarchy;
6. documents read;
7. verified branch ancestry;
8. provenance table;
9. existing repository/tree comparison;
10. existing Compose comparison;
11. existing port map;
12. existing volume/data map;
13. existing networking contract map;
14. existing environment/config map;
15. existing test-command map;
16. exact target repository tree;
17. exact unification mechanism and rationale;
18. exact source branch → target directory mapping;
19. exact files/directories imported from each branch;
20. exact files deliberately excluded;
21. path relocation changes;
22. Dockerfile/build-context changes;
23. Compose changes;
24. Compose project-name strategy;
25. root orchestration scripts;
26. startup/readiness logic;
27. shutdown behavior;
28. optional reset behavior;
29. root `.env.example` decision;
30. service `.env.example` preservation;
31. volume/persistence isolation;
32. networking invariants;
33. public/internal port invariants;
34. source-code isolation invariants;
35. Git/history handling;
36. engineering-handoff relocation;
37. `.gitignore` strategy;
38. repository-hygiene cleanup;
39. secrets/local-path scan;
40. component-specific test preservation;
41. new root smoke validation;
42. clean-state validation;
43. failure-isolation validation;
44. exact Docker commands;
45. exact test commands;
46. ordered implementation phases;
47. exact files touched per phase;
48. success gate after each phase;
49. rollback/recovery strategy;
50. risks;
51. known limitations;
52. runtime validations still required;
53. unresolved questions;
54. final consistency checklist;
55. merge-to-main gate.

The plan must be detailed enough that Claude Code can execute it phase-by-phase without making architectural decisions.

---

## 33. Expected implementation phases for Codex to refine

Codex should refine these high-level phases into exact file-level work.

### Phase 0 — Investigation only

- inspect Git;
- inspect branch ancestry;
- inspect trees;
- inspect Compose;
- inspect tests;
- record source SHAs;
- confirm source-of-truth branches;
- choose consolidation mechanism.

**No code relocation yet.**

### Phase 1 — Create unified branch and skeleton

- create `refactor/unified-submission`;
- create target directory structure;
- establish root ignore rules;
- create docs handoff directory;
- do not yet modify application semantics.

### Phase 2 — Import RTSP Simulator

- import C1;
- repair only relocation paths;
- preserve Compose;
- run C1 tests;
- run simulator stack;
- verify RTSP.

### Phase 3 — Import VMS

- import final C3/VMS tree;
- repair relocation paths;
- preserve Compose;
- run C2/C3 test tiers;
- verify simulator → VMS;
- verify live;
- verify recording/playback.

### Phase 4 — Import Video Analytics

- import C4;
- repair relocation paths;
- preserve isolation;
- run C4 validation;
- verify VMS RTSP → analytics;
- verify VMS unaffected by analytics lifecycle.

### Phase 5 — Import Semantic Search

- import C5;
- repair relocation paths;
- preserve exact model/index behavior;
- run C5 validation;
- verify C4 → C5;
- verify C5 outage isolation.

### Phase 6 — Root orchestration

- add start/stop/status helpers;
- freeze explicit project names if needed;
- add safe readiness;
- keep service-local Compose paths valid.

### Phase 7 — Repository hygiene

- move engineering handoffs;
- clean generated files;
- update `.gitignore`;
- remove local paths;
- verify no secrets;
- verify build contexts.

### Phase 8 — Unified smoke/E2E

- start all from unified tree;
- validate component boundaries;
- validate ports;
- validate data;
- validate failure isolation;
- validate persistence/restart.

### Phase 9 — Clean-state validation

- fresh directory / clean clone simulation;
- rebuild;
- recreate app state;
- prove no sibling clones required;
- prove no undocumented absolute paths.

### Phase 10 — Final merge gate

- all required tests;
- `git diff --check`;
- clean `git status`;
- no secrets;
- plan-vs-implementation consistency;
- produce `HANDOFF_unified_submission.md`;
- only then recommend merge to `main`.

Codex must replace these high-level phases with exact commands, files, failure handling, and success gates.

---

## 34. Required validation matrix

The implementation plan must include a matrix similar to:

| Validation | Simulator | VMS | Analytics | Semantic Search |
|---|---:|---:|---:|---:|
| Service-local Compose parses | ✓ | ✓ | ✓ | ✓ |
| Service starts independently | ✓ | ✓ | ✓ | ✓ |
| Unit tests | ✓ | ✓ | ✓ | ✓ |
| Integration tests | ✓ | ✓ | ✓ | ✓ |
| Browser E2E where present | ✓ | ✓ | ✓ | ✓ |
| Persistent data isolated | ✓ | ✓ | ✓ | ✓ |
| Unique Compose project | ✓ | ✓ | ✓ | ✓ |
| No cross-private-volume access | ✓ | ✓ | ✓ | ✓ |
| Full-stack integration | source | live/record | consume | consume |
| Stop isolation | ✓ | ✓ | ✓ | ✓ |

Codex must fill this with exact commands and expected results.

---

## 35. Manual acceptance requirements

Before merge to `main`, the unified branch should prove at least:

1. clone/check out unified branch into a new directory;
2. no sibling component clones are required;
3. start Simulator from `services/rtsp-simulator`;
4. create/start a virtual H.264 camera;
5. verify RTSP endpoint;
6. start VMS from `services/vms`;
7. register simulator URL;
8. camera becomes online;
9. browser WebRTC plays;
10. enable recording;
11. recording state becomes active;
12. historical playback works;
13. start Analytics from `services/video-analytics`;
14. configure the VMS RTSP camera;
15. decoded frames arrive;
16. inference runs;
17. event path is operational;
18. stop Analytics; VMS live/recording remains healthy;
19. restart Analytics; recovery works;
20. start Semantic Search from `services/semantic-search`;
21. C4 events are discovered/indexed;
22. text search works;
23. image search works;
24. stop C4; existing C5 indexed search still works;
25. restart C4; C5 recovers upstream automatically;
26. stop C5; C1–C4 remain healthy;
27. stop all through root script;
28. start all through root script;
29. persistent state survives expected restart;
30. one stack's `down --remove-orphans` does not remove peer stacks;
31. one stack's `down -v` does not remove peer volumes;
32. no untracked runtime artifacts appear in Git-controlled paths unexpectedly.

Actual commands/results belong in the final unified handoff, not in this PRD.

---

## 36. Acceptance gates

This refactor is complete only when all of the following hold:

- one unified branch contains all final service implementations;
- the final service count is four;
- C2 + C3 exist as one VMS service;
- every service remains independently runnable;
- every service retains independent persistence;
- every service retains a safe Compose identity;
- original feature branches remain intact;
- no verified application contract is intentionally changed;
- no new direct cross-service imports exist;
- no new private cross-volume reads exist;
- no new shared application DB exists;
- existing networking contracts still work;
- existing public ports still work;
- service-local tests still pass;
- required integration/E2E tiers still pass;
- root orchestration works;
- root orchestration is optional, not required for local service operation;
- stopping one stack does not unintentionally stop another;
- clean-clone operation requires no sibling clones;
- no developer absolute paths remain;
- no credentials/secrets are committed;
- generated runtime data is ignored;
- repository status is clean;
- `git diff --check` passes;
- Codex plan and Claude implementation are consistent;
- `HANDOFF_unified_submission.md` records actual validation evidence;
- user reviews the unified branch before merge;
- only then is it merged to `main`.

---

## 37. Failure conditions that must block merge

Do not merge to `main` if any of these remain unexplained:

- one component only works from its original clone;
- one Compose file references paths outside the unified repo unexpectedly;
- Compose project names collide;
- `down --remove-orphans` affects another stack;
- service volumes overlap;
- service-local tests are skipped or removed;
- VMS loses recording behavior after relocation;
- analytics gains direct dependency on VMS internals;
- semantic search gains direct dependency on analytics internals;
- ports change without requirement;
- model runtime begins downloading unexpectedly;
- C5 no longer works during C4 outage;
- generated DBs/media/model caches are tracked accidentally;
- root script depends on local machine paths;
- root script wipes data by default;
- clean clone cannot reproduce the system;
- hidden manual setup is required and not represented in the plan.

---

## 38. Risks Codex must explicitly evaluate

At minimum:

### Git/tree collision risk

Multiple branches use similar repository roots but represent different applications.

### C2/C3 supersession risk

Importing both `feat/vms` and `feat/vms-recording` independently could duplicate or regress the VMS.

### Compose naming risk

Directory relocation can change automatically derived Compose project names.

### Relative-path risk

Compose/Docker/tests may depend on old root-relative paths.

### Build-context risk

Moving projects under `services/` may unintentionally include the whole monorepo in Docker contexts.

### Volume-name risk

Automatically derived volume names can change with project name and cause apparent state loss.

### Host-network risk

`host.docker.internal` behavior must remain compatible with the verified topology.

### Test-path risk

Tests may assume execution from the original repository root.

### Model-build risk

Semantic-search build context/model acquisition must continue to work after relocation.

### Sample-media risk

Simulator sample-media/bind mounts may break if relative paths change.

### Documentation-provenance risk

Historical handoffs must not be rewritten to imply unified-repo validation that did not happen.

### Scope-creep risk

The apparent opportunity to "clean up architecture" must be resisted.

---

## 39. Rollback strategy

Codex must include a rollback plan.

Because source branches remain intact, rollback should be straightforward.

Requirements:

- no original branch is deleted;
- record exact SHAs;
- perform relocation in reviewable commits;
- preferably one logical service import per commit/phase;
- do not mix massive formatting changes with relocation;
- do not rewrite application code unless needed;
- if one component fails after import, revert that phase without affecting previously validated imported services.

The plan should recommend commit boundaries that make rollback obvious.

---

## 40. Commit strategy recommendation

Codex should refine, but a desirable pattern is:

```text
chore: scaffold unified submission layout
refactor: import rtsp simulator service
refactor: import vms live and recording service
refactor: import video analytics service
refactor: import semantic search service
chore: add unified stack lifecycle scripts
test: add unified repository smoke validation
docs: preserve component engineering handoffs
chore: clean repository submission artifacts
```

Avoid one giant unreviewable commit if possible.

Do not split commits so finely that every path edit becomes noise.

---

## 41. Final handoff requirement

Claude Code must produce:

```text
HANDOFF_unified_submission.md
```

only after actual validation.

It must include:

- branch;
- final HEAD;
- source SHAs for all services;
- final repository tree;
- what was moved;
- what changed because of relocation;
- what did not change;
- Compose project names;
- ports;
- volumes;
- networking contracts;
- root scripts;
- exact validation commands;
- test results;
- manual E2E results;
- failure-isolation results;
- clean-clone results;
- deviations from Codex plan;
- known limitations;
- remaining runtime validations;
- confirmation that original feature branches remain intact;
- merge-to-main readiness.

Do not claim validation that was not executed.

---

## 42. Known acceptable limitations

These are acceptable if honestly documented:

- root orchestration is shell-based rather than one master Compose file;
- each component still has its own Compose project;
- component service dependencies are configured through published host ports;
- Apple Silicon / Docker Desktop remains the primary validated host;
- amd64 may remain unverified;
- native Linux host may remain partially unverified;
- first semantic-search build may be large/slow due to the model;
- some validation tiers may be intentionally expensive;
- root scripts are convenience wrappers rather than production orchestration;
- original feature branches remain as historical implementation evidence;
- relocated files may not preserve perfect line-by-line Git history in `main`.

These limitations are preferable to introducing unnecessary architecture changes.

---

## 43. P1 / later submission work — explicitly outside this PRD

After Part 1 succeeds, later work will cover:

- polished root reviewer README;
- final architecture diagrams;
- reviewer demo guide;
- validation summary;
- screenshots;
- Loom walkthrough;
- GitHub presentation;
- submission email.

Do not prematurely expand this PRD into those tasks.

Part 1 must first produce a stable unified implementation base.

---

## 44. Final instruction to Codex

**Produce the plan, not application code.**

Do not treat monorepo consolidation as permission to redesign the applications.

Inspect the repository before freezing paths, branch ancestry, Compose names, volumes, or migration commands.

Prefer the smallest set of changes that gives the reviewer:

```text
one repository
one default branch
four clear services
independent Compose stacks
one optional full-stack launcher
preserved runtime boundaries
preserved tests
preserved failure isolation
clean source provenance
reproducible operation
```

The strongest result is not the most sophisticated monorepo.

The strongest result is the one where the exact implementations that were already verified independently are relocated into a clear submission structure and still behave the same way afterwards.

A successful Codex plan must be precise enough for Claude Code to execute phase-by-phase without needing to decide:

- which branch is authoritative;
- which component belongs where;
- whether C2 and C3 are separate;
- whether networking should be redesigned;
- whether Compose stacks should be merged;
- whether databases should be shared;
- whether ports should change;
- whether tests can be weakened;
- whether original branches can be discarded.

Those decisions are already frozen by this PRD.

Codex's job is to inspect the real repository, identify every relocation consequence, and produce a fail-safe implementation plan.
