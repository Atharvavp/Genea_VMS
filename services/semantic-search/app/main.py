"""Application factory, ordered startup, bounded shutdown, and /health.

No domain logic lives here. The lifespan performs exactly the ordered startup in
PLAN section 10.3 and the bounded shutdown in section 10.4:

  settings -> data root -> process lock -> SQLite (+ integrity, quarantine on
  corruption) -> schema/identity -> model assets -> model load -> interrupted
  work recovery -> vector validation -> first snapshot -> background tasks.

A failure at the model or database steps leaves HTTP alive and reports the
service unusable, rather than crash-looping a container whose dashboard could
still explain what is wrong.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import events as events_routes
from app.api import index as index_routes
from app.api import search as search_routes
from app.api.errors import REQUEST_ID_HEADER, install_error_handlers, resolve_request_id
from app.config import Settings
from app.domain.models import HealthResponse, UpstreamState, new_request_id
from app.embeddings.manifest import MANIFEST
from app.embeddings.runtime import EmbeddingRuntime
from app.integrations.component4 import Component4Client, build_client
from app.logging_config import configure_logging
from app.persistence.database import (
    Database,
    DatabaseCorrupt,
    ModelIdentityMismatch,
    ProcessLock,
    SchemaError,
    quarantine_database,
)
from app.persistence.repositories import (
    EventRepository,
    RepresentationRepository,
    SyncRepository,
)
from app.retrieval.snapshot import SnapshotStore
from app.services.discovery import DiscoveryService
from app.services.indexer import IndexerService, SnapshotRebuilder
from app.services.inference import InferenceCoordinator
from app.services.reconciliation import ReconciliationService
from app.services.search import SearchService

__all__ = ["create_app"]

logger = logging.getLogger("semantic.main")

STATIC_DIR = Path(__file__).parent / "static"

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    # blob: is needed only for the local preview of an image the operator is
    # about to upload; no remote image source is permitted.
    "img-src 'self' blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

_NO_STORE = {"Cache-Control": "no-store"}
_NO_STORE_PREFIXES = ("/api/search", "/api/index", "/health")


def create_app(
    settings: Settings | None = None,
    *,
    embedding_runtime: Any = None,
    http_client: Any = None,
    start_background: bool = True,
    acquire_process_lock: bool = True,
) -> FastAPI:
    resolved = settings or Settings()
    configure_logging(resolved.semantic_log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = app.state
        state.settings = resolved
        state.started_at = time.time()
        state.model_error = None
        state.database_error = None
        state.tasks = []
        state.stop_event = asyncio.Event()

        # 2. Only Component 5's own data root is created; no other component's
        #    directory is touched, read, or mounted.
        resolved.semantic_data_dir.mkdir(parents=True, exist_ok=True)

        # 3. Exactly one process may own this volume.
        lock = ProcessLock(resolved.lock_path)
        if acquire_process_lock:
            lock.acquire()
        state.process_lock = lock

        # 4-6. SQLite: integrity first, then schema and identity.
        database = Database(resolved.semantic_db_path)
        database.ensure_parent()
        try:
            if resolved.semantic_db_path.exists():
                await asyncio.to_thread(database.quick_check)
        except Exception as exc:  # noqa: BLE001 - includes sqlite3.DatabaseError
            logger.error("database_corrupt", extra={"error_type": type(exc).__name__})
            database.close()
            quarantine_database(resolved.semantic_db_path, resolved.quarantine_dir)
            database = Database(resolved.semantic_db_path)
            database.ensure_parent()
        try:
            system_state = await asyncio.to_thread(database.initialize)
            logger.info(
                "database_ready",
                extra={
                    "schema_version": system_state.schema_version,
                    "index_revision": system_state.index_revision,
                },
            )
        except (ModelIdentityMismatch, SchemaError) as exc:
            # Never silently mix vector spaces: search stays unavailable until an
            # operator runs the explicit offline reindex.
            state.database_error = type(exc).__name__
            logger.error(
                "store_incompatible",
                extra={"error_type": type(exc).__name__,
                       "action": "run scripts/reindex.py --confirm-reset-derived-index"},
            )

        state.database = database
        state.events = EventRepository(database)
        state.representations = RepresentationRepository(database)
        state.sync = SyncRepository(database)
        state.snapshot_store = SnapshotStore()

        # 7-8. Model assets, then the model itself.
        runtime = embedding_runtime
        if runtime is None:
            runtime = EmbeddingRuntime(
                model_dir=resolved.semantic_model_dir,
                torch_threads=resolved.semantic_torch_threads,
            )
        try:
            if state.database_error is None:
                await asyncio.to_thread(runtime.load)
                logger.info("model_ready", extra=dict(MANIFEST.describe()))
        except Exception as exc:  # noqa: BLE001 - includes ManifestError
            state.model_error = type(exc).__name__
            logger.error("model_unavailable", extra={"error_type": type(exc).__name__})
        state.embedding_runtime = runtime
        state.coordinator = InferenceCoordinator(runtime)

        client = http_client if http_client is not None else build_client()
        state.http_client = client
        state.component4 = Component4Client(
            base_url=resolved.component4_api_base_url,
            client=client,
            max_image_bytes=resolved.semantic_upstream_image_max_bytes,
        )

        rebuilder = SnapshotRebuilder(
            events=state.events,
            representations=state.representations,
            store=state.snapshot_store,
        )
        state.rebuilder = rebuilder
        state.search_service = SearchService(
            coordinator=state.coordinator,
            store=state.snapshot_store,
            settings=resolved,
        )

        if state.database_error is None:
            # 9. Work interrupted by a previous process is recoverable, not lost.
            recovered = await asyncio.to_thread(state.representations.recover_interrupted)
            # 10-11. Validate every stored vector and publish the first snapshot.
            revision = await rebuilder.rebuild_async()
            snapshot = state.snapshot_store.current()
            logger.info(
                "index_ready",
                extra={
                    "recovered_interrupted": recovered,
                    "index_revision": revision,
                    "searchable_events": snapshot.event_count,
                },
            )

        state.discovery = DiscoveryService(
            client=state.component4, events=state.events, sync=state.sync,
            settings=resolved,
        )
        state.indexer = IndexerService(
            client=state.component4, events=state.events,
            representations=state.representations, coordinator=state.coordinator,
            rebuilder=rebuilder, settings=resolved,
        )
        state.reconciliation = ReconciliationService(
            client=state.component4, events=state.events,
            representations=state.representations, sync=state.sync,
            store=state.snapshot_store, rebuilder=rebuilder, settings=resolved,
        )

        # 12. Background work. Search is already answerable without it.
        if start_background and state.database_error is None and state.model_error is None:
            state.tasks = [
                asyncio.create_task(state.discovery.run(state.stop_event),
                                    name="discovery"),
                asyncio.create_task(state.indexer.run(state.stop_event),
                                    name="indexer"),
                asyncio.create_task(state.reconciliation.run(state.stop_event),
                                    name="reconciliation"),
            ]
        logger.info(
            "semantic_search_started",
            extra={
                "model": "ok" if state.model_error is None else "unavailable",
                "database": "ok" if state.database_error is None else "incompatible",
                "background": bool(state.tasks),
            },
        )
        try:
            yield
        finally:
            # Bounded shutdown: signal, then join within the configured budget.
            state.stop_event.set()
            for task in state.tasks:
                task.cancel()
            if state.tasks:
                await asyncio.wait(
                    state.tasks, timeout=resolved.semantic_shutdown_timeout_seconds
                )
            await client.aclose()
            await asyncio.to_thread(database.close)
            release = getattr(runtime, "release", None)
            if callable(release):
                release()
            if acquire_process_lock:
                lock.release()
            logger.info("semantic_search_stopped")

    app = FastAPI(
        title="Genea Semantic Search",
        version="0.1.0",
        summary="Standalone semantic image retrieval and historical event search",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        inbound = request.headers.get(REQUEST_ID_HEADER)
        request.state.request_id = (
            inbound
            if inbound and len(inbound) <= 64 and inbound.isascii()
            else new_request_id()
        )
        request_id = resolve_request_id(request)
        started = time.monotonic()
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        if request.url.path.startswith(_NO_STORE_PREFIXES) or request.url.path == "/":
            response.headers.setdefault("Cache-Control", "no-store")
        logger.info(
            "http_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "route": request.scope["route"].path
                if request.scope.get("route")
                else request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )
        return response

    install_error_handlers(app)
    app.include_router(search_routes.router)
    app.include_router(index_routes.router)
    app.include_router(events_routes.router)

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> Response:
        state = request.app.state
        database_ok = state.database_error is None
        if database_ok:
            try:
                await asyncio.to_thread(state.database.check_readable)
            except Exception:  # noqa: BLE001
                database_ok = False
        model_ok = state.model_error is None and state.embedding_runtime.ready
        snapshot = state.snapshot_store.current()
        # An EMPTY but valid snapshot is ready: it answers searches correctly.
        index_ok = snapshot.model_id == MANIFEST.model_id
        search_ok = database_ok and model_ok and index_ok

        upstream = UpstreamState.UNKNOWN
        indexing = "stopped"
        if database_ok:
            try:
                sync = await asyncio.to_thread(state.sync.read)
                upstream = sync.upstream_state
            except Exception:  # noqa: BLE001
                upstream = UpstreamState.UNKNOWN
        if state.tasks:
            indexing = (
                "paused"
                if (state.indexer.paused_reason or upstream is UpstreamState.UNAVAILABLE)
                else "running"
            )

        # Degraded means "the upstream or indexing is behind", never "local
        # search is broken": a Component 4 outage must not fail this probe.
        status = "ok"
        if not search_ok:
            status = "unavailable"
        elif upstream is UpstreamState.UNAVAILABLE or indexing == "paused":
            status = "degraded"
        body = HealthResponse(
            status=status,
            search="ok" if search_ok else "unavailable",
            model="ok" if model_ok else "unavailable",
            database="ok" if database_ok else "unavailable",
            index="ok" if index_ok else "unavailable",
            upstream=str(upstream),
            indexing=indexing,
        )
        return JSONResponse(
            status_code=200 if search_ok else 503,
            content=body.model_dump(),
            headers=dict(_NO_STORE),
        )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(
                STATIC_DIR / "index.html",
                media_type="text/html; charset=utf-8",
                headers=dict(_NO_STORE),
            )

    return app
