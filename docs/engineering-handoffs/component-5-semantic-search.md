# Standalone Semantic Image Retrieval & Historical Event Search (Component 5) — Engineering Handoff

> **Historical document — preserved as audit evidence, not re-validated here.**
> This handoff records the implementation and validation of Semantic Search (Component 5) as it was
> carried out in its own branch and working clone. Every command, count, measurement
> and acceptance result below was executed **there**, before the unified-submission
> refactor, and is reproduced unchanged. It is **not** a claim about the unified
> repository.
>
> The implementation it describes now lives at `services/semantic-search/`, imported byte-identically
> from `feat/image_retrival` at `6df06855be8c29d1f87d1509fb26a9222d7de6fb`.
> Validation actually executed against the unified repository is recorded separately in
> [HANDOFF_unified_submission.md](HANDOFF_unified_submission.md).

---

**Repository:** `Genea_VMS` (semantic-search clone, `image_retrival/Genea_VMS`)
**Component:** Standalone Semantic Image Retrieval & Historical Event Search (Component 5)
**Branch:** `feat/image_retrival`
**Base commit at start:** `bdd4b72987da6aa0c44b73934646793eea9748bb` (`Initial commit`, one file: `README.md`)
**Status:** Implemented and verified end-to-end in Docker against the running Component 4
**Date of handoff:** 2026-09-07
**Plan implemented:** `PLAN_component_5.md` (P0 only)

> **How to read this.** §§1–9 describe what was built. §§10–13 are the
> verification record — what was actually executed, with its output. §§14–17 are
> the honest parts: deviations with evidence, limitations, what remains
> unverified, and the environment state created during acceptance. A reviewer
> hunting for weaknesses should start at §14.

---

## Table of contents

1. [What was built](#1-what-was-built)
2. [Architecture as implemented](#2-architecture-as-implemented)
3. [Repository layout](#3-repository-layout)
4. [Model and runtime as resolved](#4-model-and-runtime-as-resolved)
5. [Persistence and index behaviour](#5-persistence-and-index-behaviour)
6. [Search semantics](#6-search-semantics)
7. [Component 4 integration](#7-component-4-integration)
8. [State ownership and concurrency](#8-state-ownership-and-concurrency)
9. [Security posture](#9-security-posture)
10. [Verification record](#10-verification-record)
11. [Real Component 4 acceptance](#11-real-component-4-acceptance)
12. [Scale measurements](#12-scale-measurements)
13. [Semantic evaluation](#13-semantic-evaluation)
14. [Deviations from the plan, with evidence](#14-deviations-from-the-plan-with-evidence)
15. [Known limitations](#15-known-limitations)
16. [Runtime validations still open](#16-runtime-validations-still-open)
17. [Environment state created during acceptance](#17-environment-state-created-during-acceptance)

---

## 1. What was built

A single-process FastAPI service plus a dependency-free dashboard that mirrors
Component 4's event metadata over public HTTP, embeds each event's crop and
frame with a CPU SigLIP model, stores the vectors in its own SQLite database,
and answers text and image searches from an immutable in-memory NumPy snapshot.

Roughly **5,100 lines of application Python**, **860 lines of frontend**
(HTML + CSS + JS, no build step), **4,600 lines of tests**, and **1,100 lines of
operational scripts**. Runtime image 2.91 GB; test image 4.86 GB.

**Every isolation constraint in the plan holds and was verified at runtime:**

| Constraint | How it is enforced | Verified by |
| --- | --- | --- |
| No Component 2–4 code imported | Nothing in `app/` imports it; the image has no other component's source | `test_static_ui`, image contents |
| No Component 4 SQLite or event-file access | Only `semantic-search-data:/data` is mounted; the only SQLite opened is `/data/semantic.db` | Compose file, `test_the_only_sqlite_use_is_component_5s_own_store` |
| No VMS / MediaMTX / RTSP / WebRTC | No such string exists in executable code; ports 8090/8554/8555/9996/9997 are never contacted | `test_no_component_2_to_4_integration_exists_anywhere_in_the_app` (12 tokens) |
| No Docker socket | Not mounted, not referenced | Compose file |
| No writes to Component 4 | The adapter implements three GETs and nothing else | `Component4Client`, adapter tests |
| Outbound HTTP only from the adapter | One module builds requests | `test_the_outbound_http_surface_is_only_the_component4_adapter` |
| Failure isolation | Stopping Component 5 leaves Components 1–4 untouched | §11.5 |

## 2. Architecture as implemented

```
Browser  http://localhost:8200
  vanilla JS: text/image search | filters | result grid | detail drawer
        │ JSON / JPEG, same origin
┌───────┴───────────────────────────────────────────────────────────────┐
│ semantic-search container — one Uvicorn process, one worker, :8200    │
│                                                                       │
│ FastAPI event loop                                                    │
│   ├── /api/search/text|image  ─► SearchService ─► SearchSnapshot      │
│   ├── /api/events/{id}                (local SQLite only)             │
│   ├── /api/events/{id}/{crop,frame}   ─┐                              │
│   ├── /api/events/{id}/recording      ─┤                              │
│   ├── DiscoveryService  (backfill, 10 s overlap poll)  ├─ HTTP GET ──►│
│   ├── ReconciliationService (6 h full pass)            │              │
│   └── IndexerService (claims ≤4 events, crop then frame)┘             │
│                                                                       │
│ InferenceCoordinator ── one asyncio.Lock ──► one SigLIP CPU model     │
│   every call via asyncio.to_thread; foreground search has priority    │
│                                                                       │
│ SnapshotStore ── immutable NumPy matrices, atomic reference swap      │
│ /data/semantic.db (WAL, synchronous=FULL)   /data/semantic.lock       │
└───────────────────────────────────────────────────────────────────────┘
                             │ public HTTP only
                             ▼   Component 4 :8100
```

One Uvicorn worker is mandatory: a second process would index the same volume
concurrently and is refused by the `flock` on `/data/semantic.lock`.

## 3. Repository layout

```
app/
  main.py             app factory, ordered lifespan, bounded shutdown, /health
  config.py           immutable Settings; fixed upstream base; data-root guard
  logging_config.py   key/value records, rate limiter, reserved-key guard
  domain/models.py    ids, UTC ms timestamps, enums, records, API models
  api/                errors.py  search.py  index.py  events.py
  integrations/       component4.py — the only outbound HTTP in the service
  embeddings/         manifest.py (frozen identity + digests)  runtime.py (SigLIP)
  persistence/        database.py (pragmas, schema, ProcessLock)  repositories.py
                      schema.sql (version 1)
  retrieval/          snapshot.py (immutable index, atomic swap)  ranking.py
  security/images.py  the single entry point for every image byte
  services/           discovery.py indexer.py reconciliation.py inference.py
                      search.py
  static/             index.html  app.js  styles.css  favicon.svg
scripts/              fetch_model.py validate_runtime.py reindex.py
                      benchmark_index.py capture_eval_set.py
                      validate_real_component4.py
tests/                conftest + fakes/ (component4_server.py, factories.py)
                      unit/ integration/ real_model/ real_component4/ scale/ e2e/
requirements/         runtime.in/.lock  test.in/.lock  torch-cpu.txt (hash-locked)
```

`app/static/favicon.svg` and `requirements/*.in` are the only additions to the
planned tree; see §14.6.

## 4. Model and runtime as resolved

Read out of the built image with `scripts/validate_runtime.py`, **with the
container on `--network none`**:

```
ok    platform machine=aarch64 system=Linux python=3.12.3
ok    import fastapi==0.116.1   uvicorn==0.35.0   pydantic==2.13.5
      pydantic_settings==2.10.1   httpx==0.28.1
ok    import numpy==2.5.3   PIL==11.3.0   transformers==4.57.6
      tokenizers==0.22.2   huggingface_hub==0.36.2
ok    import safetensors==0.7.0   sentencepiece==0.2.1   multipart==0.0.20
ok    torch torch=2.7.1+cpu torchvision=0.22.1 cuda=False
ok    model-manifest dir=/opt/models/siglip files=7
      model_id=siglip-base-p16-224@7fd15f06-tf4.57.6-prep1
      revision=7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed
      sha256=2c63cb7d1f2e95ba501893cbb8faeb4ea9a3af295498d35097126228659c2af8
ok    offline-inference dim=768 load_ms=618 image_ms=233 text_ms=82 offline=1
```

**Every version pinned in the plan resolved exactly as written**, with no
substitution.

### Model artifacts

All seven files were verified against `huggingface.co` **before** they were
frozen into `app/embeddings/manifest.py`, and are verified again at build time
by `scripts/fetch_model.py` and at every load. Each digest below matched the
plan's manifest byte-for-byte.

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `model.safetensors` | 812,672,320 | `2c63cb7d1f2e95ba501893cbb8faeb4ea9a3af295498d35097126228659c2af8` |
| `config.json` | 432 | `cd85b3d28829722820bcb89a2cfbb4160e55fd359249a3044da724166a8d9688` |
| `preprocessor_config.json` | 368 | `d11ccb80f15d358a11bdb070e92e2d889005874b7db15823d5f10d9b2533b14a` |
| `tokenizer_config.json` | 711 | `d6423dae508cc3a129d22ea443841c111832a1a73125b8f25ea8736951698bcb` |
| `tokenizer.json` | 2,399,357 | `c6e405cb7c670d56636a9402c81023a55bc6c3c53d89cf02b92f5c5005bfe920` |
| `spiece.model` | 798,330 | `1e5036bed065526c3c212dfbe288752391797c4bb1a284aa18c9a0b23fcaf8ec` |
| `special_tokens_map.json` | 409 | `2b6a1ff67a27e0df9ac0c7d93250fc0d87431c7b366b3d5669217104f9088a26` |

The checkpoint is public: **no Hugging Face credential is used at build time or
runtime**, and none is accepted.

### Measured CPU latency (inside the image, `linux/arm64`)

| Operation | Measured |
| --- | ---: |
| Model load (verified manifest + weights) | 0.6–1.8 s |
| Warm single image | 199–233 ms |
| Batch of four | 802 ms (200 ms/image) |
| Four text queries | 82–235 ms |
| Full pipeline incl. fetch + decode + commit | 230 ms/image |

The plan's Docker spike recorded 255.7 ms warm and 249.1 ms/image; this build is
slightly faster on the same host.

## 5. Persistence and index behaviour

SQLite schema version 1, `journal_mode=WAL`, `synchronous=FULL`,
`foreign_keys=ON`, `busy_timeout=10000`, `trusted_schema=OFF`. Four tables:
`system_state`, `events`, `representations`, `sync_state`, with the five indexes
the plan specifies.

* Timestamps are stored as exactly `YYYY-MM-DDTHH:MM:SS.mmmZ`; a naive timestamp
  is rejected, never assumed to be UTC.
* An embedding is exactly `768 × 4 = 3072` contiguous little-endian float32
  bytes, with its dimension, dtype, L2 norm and SHA-256 stored alongside it. A
  row is `indexed` only when all of those are present and valid.
* `index_revision` is incremented **in the same transaction** that adds or
  removes an indexed representation.
* Overall event state (`searchable`/`complete`/`partial`/`failed`) is computed
  from representation rows, never stored, so it cannot drift.
* A vector that fails validation on load — wrong length, wrong dtype, checksum
  mismatch, non-finite, or a norm outside `1 ± 1e-3` — is excluded from the
  snapshot, reset to `pending`, and re-embedded. One bad row never fails a load.

The `SearchSnapshot` is a frozen dataclass of read-only, C-contiguous matrices
built in deterministic `(event_id, kind)` order. A complete replacement is built
off-lock and exchanged under one short lock, so a search that already took a
reference keeps a consistent view. Verified by
`test_publishing_swaps_atomically_and_never_mutates_the_old_snapshot`.

## 6. Search semantics

* Filters (camera, `from` inclusive / `to` exclusive, category, class,
  direction) are applied **before** any vector is scored.
* A contradictory category/class pair returns an empty result and is never
  rewritten.
* Crop and frame are scored separately, then collapse to
  `score = max(crop, frame)` over whichever exist — **one result per event,
  always**.
* `min_score` and the final top-K are applied after that aggregation.
* Ties break by score DESC, `crossed_at` DESC, `event_id` ASC. Because the
  snapshot is built in ascending-id order, the event index *is* the id rank, so
  the tie-break is a single `np.lexsort` key.
* Scores are rounded to 6 decimals for presentation; ranking uses full float32.
* Query text is NFKC-normalised, whitespace-collapsed, bounded to 1–256 code
  points, and rejected if control characters remain. It is then handed to the
  frozen tokenizer with fixed 64-token padding. **No prefix, template,
  rewriting, translation, expansion, synonym list or LLM exists in the path.**
* Uploads accept only JPEG/PNG decided **by magic bytes** — the declared
  content-type and filename are never consulted — bounded to 8 MiB, 4096 px per
  edge and 16,777,216 pixels, with animated/multi-frame input refused and
  Pillow's decompression-bomb warning treated as an error. EXIF orientation is
  applied before embedding.

## 7. Component 4 integration

`app/integrations/component4.py` implements exactly three operations:
`list_events`, `fetch_artifact`, `fetch_recording`. Every outbound URL is built
from the configured base plus a locally validated event id; redirects are never
followed; the timeout budget is connect 2 s, read 10 s (JSON) / 15 s (images),
write 2 s, pool 2 s.

Failure classification, which the indexer depends on:

| Upstream condition | Class | Effect |
| --- | --- | --- |
| 410 | `UpstreamGone` | permanent for that representation |
| 404 for a specific event | `UpstreamEventNotFound` | permanent |
| non-JPEG, oversize, undecodable | `UpstreamContractError` | permanent (retrying cannot help) |
| 408, 429, 5xx | `UpstreamRejected` | retryable |
| timeout / transport | `UpstreamTimeout` / `UpstreamUnavailable` | retryable |
| malformed page envelope | `UpstreamContractError` | pauses the pass; no cursor advance |
| `400 invalid_cursor` | `InvalidCursor` | clears **only** that pass's cursor and restarts it |

Each item in a page is validated independently: five malformed items among two
valid ones yield two events and `malformed=5`
(`test_one_malformed_item_does_not_discard_its_valid_peers`). The declared
`crop_url`/`frame_url` are checked against the expected path but are never used
to build a request. A playback URL that is not a credential-free http(s) URL
with a host, under 2048 characters and free of control characters, is downgraded
to `UNAVAILABLE` / `unsafe_playback_url` rather than forwarded — verified with
nine hostile values including `file://` and `rtsp://`.

## 8. State ownership and concurrency

| State | Owner | Protection |
| --- | --- | --- |
| Event metadata and vectors | SQLite repositories | one in-process write lock, `BEGIN IMMEDIATE`, short transactions |
| Discovery cursors, high water, upstream state | `DiscoveryService` | one background task |
| Representation work | `IndexerService` | durable DB states; ≤4 events claimed at a time |
| The SigLIP model | `InferenceCoordinator` | one `asyncio.Lock`; every call via `asyncio.to_thread` |
| Active search matrix | `SnapshotStore` | immutable snapshot + atomic reference swap |
| HTTP client | FastAPI lifespan | one `httpx.AsyncClient` |
| Volume ownership | `ProcessLock` | non-blocking `fcntl.flock` on `/data/semantic.lock` |

Fairness is enforced, not merely documented: foreground waiters are bounded at
16 with a 10 s acquisition timeout (saturation → `429 inference_queue_full`),
and the indexer raises `BackgroundDeferred` and gives its claims back **before**
taking the model if any search is waiting. `test_a_search_never_waits_behind_
more_than_the_running_batch` asserts a search completes while a background batch
is in flight.

Startup follows the plan's order: settings → data root → process lock → SQLite
`quick_check` (quarantine on corruption) → schema and model identity → model
assets → model load → recover interrupted `indexing` rows → validate every
stored vector → publish the first snapshot → start the three background tasks.
A model or identity failure leaves HTTP alive and reports the service unusable
rather than crash-looping.

## 9. Security posture

No authentication; trusted local network only. Within that: non-root uid/gid
10002, `cap_drop: ALL`, `no-new-privileges`, no Docker socket, one own volume.
No CORS — same-origin UI and API under a CSP of `'self'` plus `blob:` for the
upload preview, with `object-src 'none'`, `base-uri 'none'`,
`frame-ancestors 'none'`. `X-Content-Type-Options`, `Referrer-Policy` and
`X-Request-ID` on every response; `no-store` on HTML, search and status.

Every dependency is hash-locked and installed with `--require-hashes`; the model
is checksum-verified at build and at load; the runtime image has no compiler, no
git, no curl, no test dependency and no download path. No endpoint accepts a
URL, a hostname or a filesystem path. The reindex is an offline CLI that
requires the volume lock — there is no destructive HTTP endpoint.

Nothing echoes rejected input: `test_a_hostile_upload_is_refused_by_content_
not_by_name` asserts the filename does not appear in the response, and
`test_an_unhandled_exception_leaks_nothing` asserts neither a secret nor a model
path appears in a 500.

---

## 10. Verification record

Everything below was executed on this machine against this code. Every number is
a measurement.

**Host and toolchain**

| Item | Value |
| --- | --- |
| Host | macOS 15 (Darwin 25.6.0), `arm64` (Apple Silicon) |
| Docker | server 29.7.2, `linux/arm64`; Compose v5.5.0 |
| Base image | `ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517` |
| Runtime image | `genea-semantic-search-semantic-search:latest`, 2.91 GB |
| Tests image | `genea-semantic-search-tests:latest`, 4.86 GB (adds Playwright + Chromium) |
| Branch / base commit | `feat/image_retrival` / `bdd4b72` |

### 10.1 Automated test results

Every tier was run inside the built image.

| Tier | Selection | Tests | Result |
| --- | --- | ---: | --- |
| unit | `-m unit` | 161 | **passed**, 3.6 s |
| integration | `-m integration` | 64 | **passed**, 2.1 s |
| real model | `-m real_model` | 17 | **passed**, 29.5 s |
| scale | `-m scale` | 4 | **passed**, 9.2 s |
| browser | `-m e2e` | 13 | **passed**, 4.5 s |
| real Component 4 | `-m real_component4` | 10 | **passed**, 13.8 s |

Total: **269 automated tests, all passing.**

The unit tier uses no network, no model and no Component 4. The integration tier
uses real SQLite, real repositories, the real discovery/indexer/reconciliation
services and real HTTP plumbing against an in-process fake Component 4; only the
embedding model is substituted, by a *real* deterministic embedding function
(colour → fixed projection) so cross-modal retrieval genuinely works.

### 10.2 What the real-model tier proves

Inside the final image, with the hub forced offline:

1. all seven manifest assets verify by size and SHA-256;
2. the model loads CPU-only with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`
   (and the whole check also passes with the container on `--network none`);
3. text and image towers both return finite, unit-norm, 768-D float32 vectors;
4. repeated inference is stable (cosine > 0.9999) and batching does not change a
   vector (`atol=1e-4`);
5. every fixture is closer to itself than to any other fixture;
6. **all four colour queries rank their matching swatch first**;
7. a query's vector does not depend on what else was in its batch (fixed 64-token
   padding);
8. three inferences create and modify **no** file under `/opt/models`,
   `/opt/venv/lib` or `/srv/semantic/app` — no runtime download;
9. degenerate 1×1 and 3-pixel-tall images embed correctly (see §14.1).

### 10.3 What the fake-Component-4 integration tier proves

Backfill over three pages; one page held in memory at a time; resume from a
persisted cursor after a restart; an invalid cursor restarting a traversal with
**no duplicate rows**; overlap polling finding a new event without a restart;
recovery of an event committed 120 s behind the high water; three repeated polls
changing nothing (`index_revision` unchanged); a permanently gone crop removing
an event from search while a gone *frame* leaves a searchable crop-only event; a
transient 503 retried rather than abandoned while its peer indexes; one garbage
artifact not blocking its three peers; restart at a commit boundary recovering
interrupted `indexing` rows and never double-indexing; newest-first batches plus
every fourth oldest-first reaching both ends of the history within four batches;
and an outage leaving every local vector and the active snapshot untouched.

Recovery tier: a crash between the vector commit and the snapshot swap repaired
by reconciliation; an event 3 days behind the high water rediscovered by the full
pass; a renamed camera refreshed while an event the traversal did not return is
**still local and still searchable**; a corrupted vector excluded, rescheduled
and re-embedded; a corrupt database quarantined and rebuilt with the quarantined
copy intact; a store recorded under another model refusing to open; and the
reindex CLI refusing to run while the service holds the lock.

### 10.4 Browser suite

13 Playwright scenarios against real Chromium and a real in-process Component 5.
Any uncaught script error fails the test. They prove: local-readiness status;
one card per event in descending score order; a camera name of
`<img src=x onerror="window.__xss=1">` rendering as **text** with `window.__xss`
undefined and no element created; camera and class filters narrowing the
candidate count; a minimum score emptying the result set; the drawer showing
crop, frame and the crop/frame score breakdown; all three recording states, with
the AVAILABLE case navigating to **exactly** the URL Component 4 returned
(asserted by intercepting the request); an invalid upload reported with search
still usable; image search; a Component 4 outage showing image placeholders
while every card keeps its metadata and search keeps working, then recovering;
a stale response failing to overwrite a newer search; a keyboard-only flow; and
**no horizontal overflow at 390 px**.

### 10.5 Boundary checks

```
grep -rn -iE "sqlite|/recordings|9997|rtsp://|rtsps://|docker.sock" \
  app docker-compose.yml docker-compose.test.yml
```

returns **only** Component 5's own SQLite modules and eight lines of explanatory
prose in docstrings. There is no `rtsp://`, no `9997`, no `docker.sock` and no
`/recordings` anywhere in the service. `git diff --check` is clean.

---

## 11. Real Component 4 acceptance

Executed against the live Component 4 (`genea-analytics`, port 8100) holding
**287 real events** produced from real footage, with the Component 1 simulator,
the VMS and its MediaMTX also running.

### 11.1 Backfill and indexing of the entire live corpus

Started with an empty volume. Every one of Component 4's 287 events was
discovered and fully indexed:

```json
{
  "known_events": 287, "searchable_events": 287, "complete_events": 287,
  "partial_events": 0, "failed_events": 0,
  "crop_indexed": 287, "frame_indexed": 287,
  "pending_representations": 0, "retryable_representations": 0,
  "permanent_error_representations": 0,
  "index_revision": 574, "backfill_complete": true,
  "upstream_state": "available", "search": "ok"
}
```

**574 vectors, zero failures, zero permanent errors.** Metadata backfill of all
three pages completed in seconds; embedding took roughly 5 minutes on this host.

### 11.2 `scripts/validate_real_component4.py` — all checks passed

```
PASS  the model identity is the frozen one
PASS  embeddings are 768-D float32
PASS  the historical backfill completed
PASS  events were discovered :: 287 known
PASS  events are searchable :: 287 searchable
PASS  query 'person' returned results :: 5 results in 137.5ms; top=person@0.0544
PASS  query 'a person walking' returned results :: 5 results in 88.0ms; top=person@0.1292
PASS  query 'bus' returned results :: 5 results in 72.1ms; top=person@0.0947
PASS  query 'street scene with a bus' returned results :: 5 results in 77.5ms; top=person@0.0981
PASS  query 'car' returned results :: 5 results in 72.2ms; top=car@0.0844
PASS  query 'a white vehicle' returned results :: 5 results in 78.1ms; top=car@0.0913
PASS  query 'truck' returned results :: 5 results in 69.8ms; top=truck@0.0947
PASS  (every query) returned one card per event
cameras: [('acam_50e18733','Traffic line',196), ('acam_f7b4eede','cam_stream',49),
          ('acam_edd70707','cam_stream_vehicle',22), ('acam_f49b6eb3','cam_stream',20)]
classes: ['bus','car','person','truck']  directions: ['A_TO_B','B_TO_A']
PASS  a camera filter restricts candidates :: 196 == 196
PASS  every filtered result is from that camera
PASS  a class filter (bus) restricts candidates
PASS  a contradictory filter returns empty, not an error
PASS  the crop proxy served a JPEG :: 200, 2007 bytes
PASS  the proxy preserved immutable caching
PASS  a known crop ranks its own event first :: score=1.0000
PASS  local detail is served :: index_state=complete
PASS  recording lookup returns 200 with a known state :: status=UNAVAILABLE reason=vms_rejected_request
all checks passed
```

Live text search: **70–140 ms** end to end. Live image search: **254 ms**.

Two honest notes. The `bus` queries ranked a *person* event first on this
corpus — the crops are small objects from 640×480 traffic footage, and the
handoff for Component 4 records that its detector labels can themselves be
wrong. And the recording lookup returned `UNAVAILABLE / vms_rejected_request`:
that is Component 4's own answer with the VMS in its current state, passed
through unchanged, which is precisely the required behaviour.

### 11.3 Restart persistence and idempotency

`docker compose restart semantic-search` with the full live index:

| | before | after |
| --- | --- | --- |
| known / searchable events | 287 / 287 | 287 / 287 |
| `index_revision` | 574 | **574** (nothing re-embedded) |
| top-5 ids for "a white vehicle" | `1e43d997, be34e0fe, 70eeef6d, aad37192, a2bd80a2` | **identical** |
| top-5 scores | `0.091329, 0.089299, 0.088267, 0.086076, 0.085572` | **identical to 6 dp** |

Startup log: `index_ready recovered_interrupted=0 index_revision=574
searchable_events=287`.

### 11.4 Component 4 outage and recovery

Component 4 was stopped with `docker compose stop analytics` (and started again
immediately afterwards).

| Check | Observed |
| --- | --- |
| `/health` | **HTTP 200**, `{"status":"degraded","search":"ok","model":"ok","database":"ok","index":"ok","upstream":"unavailable","indexing":"paused"}` |
| Text search | 3 results over all 287 candidates, unchanged ids |
| Image search | HTTP 200, 3 results, 239.7 ms |
| Facets | 4 cameras, 287 searchable |
| Local event detail | HTTP 200 |
| Crop proxy | HTTP 503 `upstream_unavailable`, with no upstream hostname in the body |
| Recording | HTTP **200** with `{"status":"UNAVAILABLE","reason":"component4_unreachable"}` |
| Local vectors | 287 → 287, none deleted or invalidated |

**Component 5 was also restarted while Component 4 was still down**, and came
back fully searchable over all 287 events — local search does not depend on the
upstream even at startup.

After `docker compose start analytics`, the upstream state returned to
`available` **automatically in ~35 s** with no restart and no page reload
(Component 4 itself took ~25 s to become ready), `/health` returned to `ok`, and
the crop proxy returned 200 again.

### 11.5 Component 5 shutdown isolation

With Component 5 stopped:

| Endpoint | Before C5 stopped | After C5 stopped |
| --- | --- | --- |
| Component 4 `/health` | `{"status":"ok",…}` | `{"status":"ok",…}` |
| Component 4 `/api/events` | 200 | 200 |
| Component 4 crop | — | 200 |
| VMS `/api/cameras` | 200 | 200 |
| VMS `/api/recordings` | — | 404 (bogus test params) |
| Simulator `/api/cameras` | 200 | 200 |
| VMS MediaMTX playback | 400 (bogus params) | 400 |

Nothing changed. Stopping Component 5 has no observable effect on analytics,
live view or recording.

### 11.6 Live pipeline tier (`real_component4`, real model)

Discovery → real artifact fetch → real SigLIP embedding → SQLite → snapshot →
search, with nothing faked:

```
real pipeline: events=24 searchable=24 complete=24 model_load=1.8s
               index=11.1s over 6 batches for 48 images (230 ms/image incl. fetch)
  'person'             -> car    score=-0.0141
  'a bus on the street'-> car    score=0.0686
  'a car'              -> car    score=0.0954
  'a truck'            -> truck  score=0.0957
  overlap poll: 24 -> 49 known events
```

The same tier asserts that a real crop used as an image query ranks **its own
event first with score > 0.99**, that real metadata filters restrict candidates
exactly, and that reopening the same volume reproduces byte-identical crop and
frame matrices at the same revision without re-embedding anything.

---

## 12. Scale measurements

1k / 5k / 10k synthetic events, two 768-D vectors each, measured inside the test
image on this host. 200 queries per measurement.

| Events | Vectors | SQLite | Snapshot vectors | Load+validate | Snapshot build | Incremental publish | Search p50/p95/p99 | Filtered (10%) p50/p99 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 2,000 | 9.1 MB | 6.1 MB | 26.8 ms | 9.2 ms | 26.4 ms | 0.310 / 0.325 / 0.357 ms | 0.074 / 0.101 ms |
| 5,000 | 10,000 | 45.4 MB | 30.7 MB | 77.5 ms | 40.2 ms | 134.8 ms | 1.369 / 1.414 / 1.472 ms | 0.234 / 0.285 ms |
| 10,000 | 20,000 | 90.9 MB | 61.4 MB | 176.5 ms | 149.4 ms | 311.5 ms | **3.016 / 3.143 / 3.573 ms** | 0.426 / 0.564 ms |

* Vector insertion: 0.3 s / 1.0 s / 2.1 s for 2k / 10k / 20k vectors.
* Snapshot memory is exactly the raw vectors: 61.4 MB at 10k events, no
  multiple. Process RSS rose 130.2 → 144.4 MB building the 10k index.
* The running service with the live 287-event index used **282 MiB** RSS.

The acceptance gate — interactive, sub-second exact search at 10k events — is
met with **three orders of magnitude of headroom**: 3.0 ms p50. Query embedding
(70–250 ms of model time) dominates a search, not the matrix product. This
confirms the plan's decision that FAISS would buy nothing at this scale.

---

## 13. Semantic evaluation

Corpus: **40 user-owned events** captured from the live Component 4 by
`scripts/capture_eval_set.py` — 10 each of person, car, bus and truck. The
images live in the gitignored `tests/assets/cache/`; only
`tests/assets/semantic_eval_manifest.json` (identities, digests, labels,
expected queries) is committed. **No upstream image is redistributed.**

Macro Recall@5 over 12 plain-language queries:

| Representation | macro Recall@5 |
| --- | ---: |
| crop only | **1.000** |
| frame only | 0.833 |
| **max fusion (what the service uses)** | **1.000** |

Self-retrieval: 36/40 images rank themselves first; the other 4 lose **only to a
byte-identical duplicate** — verified by SHA-256, cosine exactly 1.000. Component
4 emits distinct events whose crop images are the same bytes, and identical
inputs produce identical vectors (asserted over 3 duplicate groups).

> **This is not a general accuracy claim.** The corpus is small, from one
> deployment, and labelled by Component 4's own detector, which the Component 4
> handoff records can itself be wrong. Frame-only recall being lower than crop is
> expected: a small object in a 640×480 frame is easily dominated by its
> background, which is exactly why the service fuses the two.

---

## 14. Deviations from the plan, with evidence

Each is the smallest change that preserves the plan's invariant while matching
what the runtime actually does. None alters a service boundary, P0 semantics,
the schema, an external contract, or the security posture.

### 14.1 The processor must be told the channel layout

**Plan:** §6.2 freezes `AutoProcessor(..., use_fast=False, local_files_only=True)`
and says nothing more about the call.

**Observed:** a real image search returned **HTTP 500** during live acceptance.
Reproduced directly against the pinned Transformers 4.57.6:

```
1x1  no hint -> FAIL ValueError: mean must have 1 elements if it is an iterable, got 3
1x1  channels_last -> (1, 3, 224, 224)
5x3  no hint -> (1, 3, 224, 224)   [with "The channel dimension is ambiguous" warning]
5x3  channels_last -> (1, 3, 224, 224)
```

Transformers infers the channel axis from the array shape. For a 1×1 image the
inference **raises**; for a 3-pixel-tall image it silently decides the data is
channels-first and embeds **transposed pixels** — the quieter and worse failure.
Component 4 crops of distant objects genuinely are this small: the live corpus
contains crops of ~2 KB.

**Change:** `EmbeddingRuntime.embed_images` passes
`input_data_format="channels_last"` on every call.

**Proof it is safe:** for a normal image the two paths are **bit-identical**
(`max abs diff 0.0`), so the vector space is unchanged, `preprocessor_id` stays
`prep1`, and **no reindex is required**. For the 3-pixel-tall image the two
differ (cosine 0.944), confirming the old path was producing a wrong vector.

**Regression tests:** `test_degenerate_small_images_are_embedded_from_the_
correct_axes` (including that a rotated image does *not* embed the same) and
`test_a_batch_of_mixed_sizes_embeds_every_row`.

### 14.2 The indexer catches every inference failure, not three classes

**Observed:** the ValueError in §14.1 is not `EmbeddingError`, `ModelUnavailable`
or `RuntimeError`, so it escaped the batch handler. The claimed rows stayed in
`indexing` and would have been invisible to every later batch until the process
restarted.

**Change:** both the batch path and the per-image retry path catch `Exception`
and treat it as retryable (`inference_failed`). The plan's guarantee — "if a
batch inference fails, retry each valid image individually so one bad event
cannot block its peers" — now holds for *any* failure, not an enumerated few.

**Regression test:** `test_an_unexpected_inference_failure_never_strands_claimed_
rows` injects exactly that ValueError and asserts no row is left `indexing` and
the work is still claimable afterwards.

### 14.3 Control characters are rejected before the URL is trimmed

**Observed:** a unit test showed `COMPONENT4_API_BASE_URL="http://c4:8100\n"`
was accepted, because the validator stripped whitespace *before* checking for
control characters. **Change:** the control-character check now runs on the raw
value; only spaces are trimmed afterwards. Covered by
`test_unsafe_component4_urls_are_rejected`.

### 14.4 An `AVAILABLE` recording with an unsafe URL is downgraded

The plan lists `unsafe_playback_url` among the safe reasons but does not say
which side produces it. Implemented: if Component 4 says `AVAILABLE` but its
playback URL is not a credential-free http(s) URL with a host, under 2048
characters and free of control characters, Component 5 returns
`UNAVAILABLE / unsafe_playback_url` rather than handing a browser something it
refuses to vouch for. Nine hostile values are covered by
`test_an_unsafe_playback_url_is_downgraded_never_forwarded`.

### 14.5 The boundary tests read code, not prose

The isolation modules necessarily *name* the things they refuse to touch —
`component4.py`'s docstring says "no way to reach the VMS, MediaMTX, or a
recording file". A naive substring scan flags exactly the code that enforces the
rule. The tests therefore strip comments and docstrings with `tokenize` before
scanning, which is the distinction §17.7 of the plan already anticipates. The
scan covers 12 forbidden tokens including `faiss`, `qdrant` and `openai`.

### 14.6 Small additions to the planned tree

* `app/static/favicon.svg` — the placeholder `href="data:,"` favicon violated the
  frozen CSP (`img-src 'self' blob:`) and produced a console error in Chromium.
  A same-origin SVG keeps the CSP exactly as specified.
* `requirements/runtime.in` and `test.in` — the plan names `.in` and `.lock`
  files; both were produced, with the locks compiled from them inside
  `ubuntu:24.04` on `linux/aarch64` with Python 3.12.3.
* `tests/fakes/factories.py` also holds `FakeEmbeddingRuntime` rather than a new
  module, keeping the planned `tests/fakes/` file list intact.
* `SnapshotRebuilder` lives in `app/services/indexer.py`; the plan's tree has no
  separate home for the shared rebuild used by startup, the indexer and
  reconciliation.

### 14.7 `psutil` moved to the test lock

The plan lists psutil 7.2.2 under the test baseline; it is used only by the
scale tier, so it is in `requirements/test.lock` and absent from the runtime
image.

---

## 15. Known limitations

Every one of these is observed behaviour, not a suspicion.

**Retrieval quality**

* Model-dependent similarity, **not** verified attribute classification. On the
  live 287-event corpus the query `bus` ranked a *person* event first; the crops
  are small objects from 640×480 footage. Component 4's own class labels can
  also be wrong, so neither side is ground truth.
* English-focused text tower. No facial recognition, biometrics, identity
  tracking or cross-camera re-identification.
* `min_score` has no default and no calibrated meaning: cosine distributions
  shift between queries. The UI says so explicitly.

**Throughput**

* CPU indexing is the limit: ~230 ms per image including fetch and decode, so
  10k events (20k images) is a multi-hour first backfill. Search stays
  interactive throughout — the indexer yields the model to any waiting search.
* Batch-of-four peak RSS was ~1.35 GiB in the plan's spike; the running service
  with a 287-event index used 282 MiB. 3 GiB of Docker memory is the
  recommendation.

**Data and operations**

* Component 4 can emit distinct events whose crop images are byte-identical, so
  a result page can legitimately show several cards that look the same.
* Only Component 4 events are searchable; no recording-frame or video search.
* A Component 4 outage prevents new indexing and uncached image/recording
  access; existing search is unaffected.
* Single host, one SQLite store, one process. A second process is refused by the
  volume lock; there is no leader election or HA.
* **No retention policy.** Nothing deletes an event or a vector.
* No authentication and no metrics endpoint. Anyone who can reach port 8200 can
  search every indexed event and view its images.
* A model, revision, preprocessing, dimension or dtype change requires the
  explicit offline reindex; there is no background generation swap.
* The image is 2.91 GB, dominated by CPU Torch and the 812 MB checkpoint.

**Testing**

* The integration tier substitutes the embedding model with a deterministic
  colour-projection function. The real model is exercised by the `real_model`
  and `real_component4` tiers.
* The browser suite runs Chromium only.
* No load, soak or chaos testing beyond the outage, restart and scale runs
  recorded here.

---

## 16. Runtime validations still open

Nothing below is claimed as verified.

1. **A newly created Component 4 event indexed live.** Not demonstrated. All
   simulator, VMS and analytics cameras had been removed at the end of Component
   4's own acceptance, and the only remaining sample clip is the SMPTE
   colour-bar pattern in which YOLO correctly detects nothing — so no new event
   could be produced without sourcing new footage. The *code path* is proven
   twice: `test_overlap_polling_finds_a_new_event_without_a_restart` against the
   fake, and, against the **live** Component 4 with the real model, an overlap
   poll that discovered 25 events the process had never seen (24 → 49) with no
   restart. What remains unproven is only the wall-clock case of an event
   created *after* Component 5 started.
2. **amd64.** Everything here was built and run on `linux/arm64`. The locks
   carry hashes for both architectures and `torch-cpu.txt` pins the x86_64
   wheels, but **no amd64 build or test run was executed.**
3. **Linux `host-gateway`.** Verified on Docker Desktop for macOS only.
4. **10k real events.** Scale figures are synthetic vectors; the largest real
   corpus indexed was 287 events. Backfill throughput at 10k is extrapolated
   from the measured 230 ms/image, not observed.
5. **Sustained load and soak.** The longest continuous run was minutes. Memory
   was sampled, not profiled; no thermal or throttling behaviour was observed.
6. **Concurrent search load.** Foreground/background fairness is proven by unit
   tests and by search remaining interactive during the live backfill, but no
   many-client concurrency test was run, and the 16-waiter saturation path was
   exercised only against a synthetic slow runtime.
7. **Disk exhaustion, a read-only volume, and real SQLite corruption.**
   Corruption recovery is proven against a deliberately corrupted file; a
   genuinely full or remounted volume was not exercised.
8. **A stopped-service backup and restore drill.** Documented in `README.md` §7;
   not performed.
9. **Browsers other than Chromium**, and the reachability of a returned
   `playback_url` from a machine other than the Docker host.
10. **An `AVAILABLE` recording end to end.** The live VMS answered
    `vms_rejected_request` throughout, so Component 4 never returned
    `AVAILABLE` during acceptance. The `AVAILABLE` path is covered against the
    fake, including the browser opening the exact returned URL.

---

## 17. Environment state created during acceptance

**None that persists.** Component 5 talked to Component 4 over its public HTTP
API only, and every request was a `GET`. No camera, event, recording or file was
created, changed or deleted in Components 1–4, and no file outside this
repository was modified.

Two reversible actions were taken on other components and both were undone:

* `docker compose stop analytics` / `start analytics` in the Component 4 clone,
  for the outage test in §11.4. Component 4 is running and healthy.
* Component 5 itself was stopped and started several times.

State this component owns:

* the `semantic-search-data` Docker volume, holding `/data/semantic.db` with 287
  events and 574 vectors. Remove with `docker compose down --volumes` **in this
  directory only** — the explicit project name `genea-semantic-search` makes that
  safe for the VMS and analytics stacks.
* `tests/assets/cache/` — 80 JPEGs (16 MB) captured from the live Component 4 for
  the semantic evaluation. It is **gitignored and not committed**; delete it
  freely, and recreate it with `scripts/capture_eval_set.py`.

`git status --short` shows only this component's own new files;
`git diff --check` is clean; 90 files would be added, none of them from the
image cache.

---

*The user-facing guide is [README.md](../../services/semantic-search/README.md). The upstream contract consumed
here is described by `HANDOFF_component_4.md` in the analytics clone; Component 5
consumes it through public HTTP only.*
