# Genea Semantic Search — Component 5

Standalone semantic image retrieval and historical event search over the events
produced by Component 4 (Video Analytics & Event Detection).

Ask for **"a white vehicle"** or upload a photo, and get back the Component 4
events whose images look most like it — filtered by camera, time, category,
class and direction. Search keeps working when Component 4 is down.

```
Browser :8200
  text / image search, filters, result grid, detail drawer
        │ same origin
┌───────┴──────────────────────────────────────────────────────────┐
│ semantic-search container — one Uvicorn process, one worker      │
│                                                                  │
│   FastAPI                                                        │
│     ├── /api/search/text, /api/search/image   (local only)       │
│     ├── /api/events/{id}                      (local only)       │
│     ├── /api/events/{id}/{crop,frame}  ──proxy──►  Component 4   │
│     ├── /api/events/{id}/recording     ──ask───►  Component 4    │
│     ├── discovery + reconciliation     ──read──►  Component 4    │
│     └── indexer (bounded, crop first)  ──read──►  Component 4    │
│                                                                  │
│   one SigLIP CPU model · immutable NumPy snapshot · SQLite /data │
└──────────────────────────────────────────────────────────────────┘
                             │ public HTTP only
                             ▼   Component 4 :8100
```

Component 5 is **entirely downstream and read-only**. It never touches
Component 4's database or event files, never reaches the VMS, MediaMTX, RTSP or
WebRTC, never holds the Docker socket, and never creates, changes or deletes a
Component 4 event. Stopping it has no effect on analytics, live view or
recording.

---

## 1. Requirements

* Docker with Compose v2 (built and verified on Docker 29.7.2, `linux/arm64`).
* A reachable Component 4 at `COMPONENT4_API_BASE_URL` (default
  `http://host.docker.internal:8100`).
* **3 GiB** of memory available to Docker is recommended; 2 GiB is the practical
  minimum. Peak RSS during batch-of-four indexing was measured at ~1.35 GiB.
* ~3 GB of disk for the image (dominated by CPU PyTorch and the 812 MB
  checkpoint) plus ~9 MB of index per 1,000 events.
* Network access **at build time only**, to fetch wheels and the model. The
  runtime needs no internet and no Hugging Face credential.

## 2. Run it

```bash
cd services/semantic-search   # every command below runs from this directory
cp .env.example .env          # optional; every value has a working default
docker compose build semantic-search
docker compose up -d semantic-search
open http://localhost:8200
```

The first start creates the volume, backfills every Component 4 event, and
indexes them. Search is answerable immediately — an empty index returns an empty
result, not an error — and results appear as events become searchable. Watch
progress in the dashboard's status bar or with:

```bash
curl -s http://localhost:8200/api/index/status | jq
```

Stop with `docker compose stop semantic-search`. Remove everything this
component owns, including its index, with `docker compose down --volumes` **in
this directory only** — the explicit Compose project name `genea-semantic-search`
keeps that from touching the VMS or analytics stacks.

## 3. Configuration

Every setting is read once at startup. No secret is needed.

| Variable | Default | Notes |
| --- | --- | --- |
| `SEMANTIC_HTTP_PORT` | `8200` | Host port |
| `COMPONENT4_API_BASE_URL` | `http://host.docker.internal:8100` | The only upstream. `scheme://host[:port]`, no credentials, path, query or fragment |
| `SEMANTIC_DATA_DIR` / `SEMANTIC_DB_PATH` | `/data` / `/data/semantic.db` | The DB must be strictly beneath the data root |
| `SEMANTIC_TORCH_THREADS` | `2` | 1–8 |
| `SEMANTIC_INDEX_BATCH_SIZE` | `4` | 1–8; images per model call |
| `SEMANTIC_POLL_INTERVAL_SECONDS` | `10` | 2–300 |
| `SEMANTIC_OVERLAP_SECONDS` | `300` | 60–3600; how far each poll reaches back |
| `SEMANTIC_FULL_RECONCILE_SECONDS` | `21600` | 300–86400 |
| `SEMANTIC_MAX_RETRY_SECONDS` | `300` | Retry ceiling for one representation |
| `SEMANTIC_UPLOAD_MAX_BYTES` | `8388608` | 1–16 MiB |
| `SEMANTIC_UPLOAD_MAX_PIXELS` | `16777216` | Plus a 4096 px edge cap |
| `SEMANTIC_UPSTREAM_IMAGE_MAX_BYTES` | `16777216` | Cap on a proxied artifact |
| `SEMANTIC_SHUTDOWN_TIMEOUT_SECONDS` | `15` | 5–60 |
| `SEMANTIC_LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR |

The model, its revision, the embedding dimension, the dtype and the
preprocessing are **not** configurable: they are code constants in
`app/embeddings/manifest.py`, because changing any of them changes the vector
space (see §7).

## 4. The model and the index

| | |
| --- | --- |
| Model | `google/siglip-base-patch16-224` |
| Revision | `7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed` |
| Weights SHA-256 | `2c63cb7d1f2e95ba501893cbb8faeb4ea9a3af295498d35097126228659c2af8` (812,672,320 bytes) |
| Identity | `siglip-base-p16-224@7fd15f06-tf4.57.6-prep1` |
| Embedding | 768-D, little-endian `float32`, explicitly L2-normalised |
| Similarity | Cosine (a dot product of unit vectors) |
| Device | CPU |

The checkpoint is downloaded **only in the build stage**, with all seven files
verified by size and SHA-256 before the directory is promoted. At runtime
`HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` are set, so a missing or altered
file fails model readiness instead of triggering a download.

**Two vectors per event.** The detection crop and the full frame are embedded
separately. An event becomes searchable when its **crop** is indexed; the frame
is optional, and an event whose frame is permanently gone stays searchable from
its crop alone.

**One score per event:** `score = max(crop_cosine, frame_cosine)` over whichever
representations exist. An event therefore appears exactly once in a result list,
never twice. The per-representation scores are returned alongside it.

**Order of operations:** metadata filters are applied *before* any vector is
scored; the threshold and the final top-K are applied *after* the two
representations collapse to one event score. Ties break by score, then
`crossed_at` descending, then event id ascending, so repeated searches are
identical.

Search is **exact**: every candidate is scored with NumPy. There is no
approximate index, no FAISS, no vector service, and no query rewriting, prompt
prefix, translation, synonym expansion or LLM anywhere in the path.

> **Scores are cosine similarities, not probabilities.** They have no fixed
> meaning across queries and no calibrated threshold. `min_score` is optional
> and has no default for exactly that reason.

## 5. Keeping up with Component 4

* **Backfill** — on an empty store, every page of `GET /api/events` is walked
  with Component 4's opaque cursors. The cursor is persisted only after its
  page's metadata has committed, so a restart re-reads one page rather than
  skipping it. Every ten pages the traversal pauses to check the head, so a new
  event is not stuck behind a long backfill.
* **Polling** — every 10 s, a complete traversal from
  `high_water − 300 s`. The overlap means an event committed slightly out of
  timestamp order is still seen.
* **Full reconciliation** — every 6 h, an unbounded traversal that rediscovers
  anything outside the overlap window, refreshes camera names, revalidates every
  stored vector, and rebuilds the snapshot if it has drifted from the durable
  revision. It **never deletes** a local event just because one traversal did
  not return it.

All three are idempotent: an event is keyed by its Component 4 id and a vector
by `(event_id, kind, model_id)`, so rediscovery can never duplicate a result or
undo committed work.

## 6. When Component 4 is down

| Still works | Degrades |
| --- | --- |
| Text and image search | Discovery and new indexing (paused, retried) |
| Filters and facets | Crop/frame images not already in browser cache |
| Local event detail | Recording lookup → `UNAVAILABLE` |
| `/health` → **HTTP 200**, `status: degraded` | |

No stored vector is deleted or invalidated by an outage. When Component 4
returns, the next bounded retry resumes polling and pending work automatically —
no restart, no page reload. This was verified by stopping Component 4 with a
fully indexed store; see
[component-5-semantic-search.md](../../docs/engineering-handoffs/component-5-semantic-search.md) §11.

## 7. Reindexing and model changes

There is deliberately **no HTTP endpoint** that resets the index. Changing the
model, revision, preprocessing, dimension or dtype changes the vector space, and
startup refuses to open a store recorded under a different identity rather than
mixing spaces. To rebuild:

```bash
docker compose stop semantic-search
docker compose run --rm semantic-search \
  /opt/venv/bin/python -m scripts.reindex --confirm-reset-derived-index
docker compose up -d semantic-search
```

Without `--confirm-reset-derived-index` it only prints an integrity report. It
refuses to run while the service holds the volume lock. The old database is
**moved** into `/data/quarantine/<timestamp>/` and is never deleted
automatically. Everything it discards is derived data: Component 4 remains the
authoritative source and the index is rebuilt from it.

**Backup** is a file copy of the stopped service's volume; there is nothing else
to preserve.

## 8. Public API

| Route | Purpose |
| --- | --- |
| `POST /api/search/text` | `{query, top_k, min_score, filters}` |
| `POST /api/search/image` | `multipart/form-data` field `image`; filters as query parameters |
| `GET /api/events/{id}` | Local metadata and index state |
| `GET /api/events/{id}/crop` \| `/frame` | Proxied JPEG from Component 4 |
| `GET /api/events/{id}/recording` | Delegated to Component 4, unchanged |
| `GET /api/index/status` | Counters, model identity, freshness |
| `GET /api/index/facets` | Filter values from local metadata |
| `GET /health` | Local readiness vs upstream freshness |
| `GET /docs` | OpenAPI |

Errors use Component 4's envelope
(`{"error": {code, message, details, request_id}}`) and every response carries
`X-Request-ID`. Stable codes: `invalid_query`, `invalid_time_range`,
`event_not_found`, `event_artifact_gone`, `image_too_large`,
`unsupported_image_type`, `validation_error`, `invalid_image`,
`inference_queue_full`, `upstream_invalid_response`, `search_unavailable`,
`upstream_unavailable`, `upstream_timeout`, `internal_error`.

```bash
curl -s -X POST http://localhost:8200/api/search/text \
  -H 'content-type: application/json' \
  -d '{"query":"a white vehicle","top_k":5,
       "filters":{"object_category":"vehicle"}}' | jq

curl -s -F "image=@query.jpg" \
  "http://localhost:8200/api/search/image?top_k=5&object_class=bus" | jq
```

## 9. Security posture

**No authentication. Trusted local network only.** Anyone who can reach port
8200 can search every indexed event and view its images. Do not expose it.

Within that posture: non-root uid/gid 10002, `cap_drop: ALL`,
`no-new-privileges`, no Docker socket, no external volume, one own volume. No
CORS — the dashboard and the API are same-origin, under a CSP that allows only
`'self'` scripts, styles, connections and images (plus `blob:` for the local
upload preview) with `object-src 'none'`, `base-uri 'none'` and
`frame-ancestors 'none'`. Every dependency is hash-locked and installed with
`--require-hashes`; the model is checksum-verified at build and at load; the
runtime image contains no compiler, git, curl, test dependency or download path.

The image proxy is the only outbound fetch made for a user, and its destination
is always `{COMPONENT4_API_BASE_URL}/api/events/{locally known id}/{crop|frame}`
— no caller-supplied URL, hostname or path can influence it, and redirects are
never followed. Playback URLs are validated and passed through verbatim, never
rewritten, proxied or stored. Logs never contain a query, an upload filename,
image bytes, a vector, a playback URL, an upstream body or a path.

## 10. Validation

```bash
docker compose config
docker compose build semantic-search

docker compose -f docker-compose.test.yml build tests
docker compose -f docker-compose.test.yml run --rm tests pytest -q -m unit
docker compose -f docker-compose.test.yml run --rm tests pytest -q -m integration
docker compose -f docker-compose.test.yml run --rm tests pytest -q -m real_model
docker compose -f docker-compose.test.yml run --rm tests pytest -q -m scale
docker compose -f docker-compose.test.yml run --rm tests pytest -q -m e2e

COMPONENT4_API_BASE_URL=http://host.docker.internal:8100 \
docker compose -f docker-compose.test.yml run --rm tests pytest -q -m real_component4

docker run --rm --network none genea-semantic-search-semantic-search:latest \
  /opt/venv/bin/python scripts/validate_runtime.py \
  --platform --imports --torch --model-manifest --offline-inference

docker compose -f docker-compose.test.yml run --rm tests \
  /opt/venv/bin/python scripts/validate_real_component4.py \
  --c5 http://host.docker.internal:8200 --c4 http://host.docker.internal:8100
```

`scripts/benchmark_index.py` reports index and search measurements at 1k/5k/10k
events. `scripts/capture_eval_set.py` builds a **user-owned** evaluation corpus
from a live Component 4 into the gitignored `tests/assets/cache/`; the images
are never committed.

Results from the run that accompanied this implementation are in
[component-5-semantic-search.md](../../docs/engineering-handoffs/component-5-semantic-search.md).

## 11. Known limitations

* Retrieval is model-dependent similarity, **not** verified attribute
  classification. Component 4's own detector labels can be wrong, and semantic
  ranking must not be read as ground truth.
* The text tower is English-focused.
* No facial recognition, biometrics, identity tracking or cross-camera
  re-identification — none is planned.
* Only Component 4 events are searchable. There is no recording-frame or video
  search.
* Component 4 may emit distinct events whose crop images are byte-identical, so
  a result page can legitimately show several cards that look the same.
* CPU indexing is the throughput limit: roughly 230 ms per image including
  fetch, so ~10k events (20k images) is a multi-hour first backfill. Search is
  unaffected while it runs.
* Single host, single process, one SQLite store. A second process is refused by
  the volume lock; there is no leader election and no HA.
* No retention policy: nothing deletes an event or a vector.
* No authentication, no metrics endpoint, no GPU support in P0.
* A model change requires the explicit offline reindex in §7.

## 12. Layout

```
app/
  main.py             app factory, ordered lifespan, /health
  config.py           immutable validated settings
  domain/models.py    ids, timestamps, records, request/response types
  api/                errors.py search.py index.py events.py
  integrations/       component4.py — the only outbound HTTP
  embeddings/         manifest.py (frozen identity) runtime.py (SigLIP)
  persistence/        database.py repositories.py schema.sql
  retrieval/          snapshot.py (immutable index) ranking.py (exact search)
  security/images.py  every image byte enters here
  services/           discovery indexer reconciliation inference search
  static/             index.html app.js styles.css — no build step
scripts/              fetch_model validate_runtime reindex benchmark_index
                      capture_eval_set validate_real_component4
tests/                unit integration real_model real_component4 scale e2e
```
