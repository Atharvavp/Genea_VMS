"""Orchestration: SQLite is the desired state, MediaMTX is derived from it.

Division of labour:

* **SQLite** stores what the operator asked for (name, source URL, enabled).
  Nothing derived is written there.
* **MediaMTX** holds one pull-always path per enabled camera. It is treated as
  a cache of the database, rebuilt whenever the two disagree.
* **This process** keeps camera health in memory only, refreshed from MediaMTX
  on a short poll. It is never persisted: a stored health value is stale the
  moment the process stops, and would have to be distrusted at startup anyway.

Reconciliation is deliberately *not* run on every poll. It runs:

* at startup, once MediaMTX is reachable;
* on the single camera a mutation touched (create / update / enable / disable /
  delete);
* when the poll finds drift - an enabled camera whose path is missing, or a
  managed path that no longer belongs to anyone. That covers a MediaMTX restart
  without needing a second API call to detect one;
* when MediaMTX comes back after having been unreachable.

Drift-triggered reconciles are rate-limited so a permanently unfixable camera
cannot turn the poll into a reconcile loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from app.config import Settings
from app.domain.models import (
    CameraCreate,
    CameraHealth,
    CameraHealthState,
    CameraRecord,
    CameraUpdate,
    CameraView,
    new_camera_id,
    utcnow_iso,
)
from app.persistence.camera_repository import CameraRepository
from app.security.rtsp_url import sanitize_rtsp_url, sanitize_text, url_has_credentials
from app.services.mediamtx_client import (
    MediaMTXClient,
    MediaMTXError,
    MediaMTXUnavailable,
    PathRejected,
)

logger = logging.getLogger(__name__)

# MediaMTX 1.20.1 has no per-static-source lastError endpoint, so an offline
# source can only be reported generically.
OFFLINE_HINT = "MediaMTX cannot currently read this RTSP source."
DISABLED_HINT = "Camera is disabled; the VMS is not ingesting it."


class CameraNotFound(KeyError):
    """No camera with that id."""


class CameraManager:
    def __init__(
        self,
        settings: Settings,
        repository: CameraRepository,
        client: MediaMTXClient,
    ) -> None:
        self._settings = settings
        self._repo = repository
        self._client = client

        self._health: dict[str, CameraHealth] = {}
        self._mediamtx_available = False
        self._mediamtx_version: str | None = None
        self._last_reconcile_at = 0.0
        self._pending_reconcile: str | None = "startup"

        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._reconcile_lock = asyncio.Lock()
        self._loop_task: asyncio.Task | None = None

    # --- lifecycle ------------------------------------------------------

    async def initialize(self) -> None:
        """Prepare storage. Split from `startup` so tests can drive the poll."""
        self._settings.ensure_directories()
        await asyncio.to_thread(self._repo.initialize)

    async def startup(self) -> None:
        await self.initialize()
        # The poll loop performs the startup reconcile on its first pass, so a
        # MediaMTX that is not up yet delays nothing: the app serves the UI and
        # the API immediately and converges as soon as MediaMTX answers.
        self._loop_task = asyncio.create_task(self._run_loop(), name="vms-health-poll")
        logger.info("vms_started mediamtx_api=%s", self._settings.mediamtx_api_url)

    async def shutdown(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        await self._client.aclose()
        logger.info("vms_stopped")

    async def _run_loop(self) -> None:
        interval = max(0.5, self._settings.camera_health_poll_seconds)
        while True:
            try:
                if self._pending_reconcile is not None:
                    reason, self._pending_reconcile = self._pending_reconcile, None
                    await self.reconcile_all(reason)
                await self.refresh_health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a poll must never kill the loop
                logger.exception("health_poll_failed error=%s", exc)
            await asyncio.sleep(interval)

    # --- queries --------------------------------------------------------

    async def list_cameras(self) -> list[CameraView]:
        records = await asyncio.to_thread(self._repo.list)
        return [self._to_view(record) for record in records]

    async def get_camera(self, camera_id: str) -> CameraView:
        return self._to_view(await self._require(camera_id))

    async def get_health(self, camera_id: str) -> CameraHealth:
        record = await self._require(camera_id)
        return self._health_of(record)

    async def health_report(self) -> dict:
        try:
            total, enabled = await asyncio.to_thread(self._repo.count)
            database_ok = True
        except Exception as exc:  # surfaced through /health rather than raised
            logger.warning("health_database_error error=%s", sanitize_text(str(exc)))
            total, enabled, database_ok = 0, 0, False

        online = sum(
            1
            for state in self._health.values()
            if state.state is CameraHealthState.ONLINE
        )
        healthy = database_ok and self._mediamtx_available
        return {
            "status": "ok" if healthy else "degraded",
            "database": "ok" if database_ok else "error",
            "mediamtx": {
                "reachable": self._mediamtx_available,
                "version": self._mediamtx_version,
                "api_url": self._settings.mediamtx_api_url,
            },
            "cameras": {"total": total, "enabled": enabled, "online": online},
            "webrtc_base_url": self._settings.public_webrtc_base_url,
        }

    # --- mutations ------------------------------------------------------

    async def create_camera(self, payload: CameraCreate) -> CameraView:
        camera_id = new_camera_id()
        now = utcnow_iso()
        record = CameraRecord(
            id=camera_id,
            name=payload.name,
            rtsp_url=payload.rtsp_url,
            mediamtx_path=self._settings.mediamtx_path_for(camera_id),
            enabled=payload.enabled,
            created_at=now,
            updated_at=now,
        )
        async with self._locks[camera_id]:
            # Desired state first: a MediaMTX outage must not lose the camera.
            await asyncio.to_thread(self._repo.insert, record)
            logger.info(
                "camera_created camera_id=%s path=%s enabled=%s source=%s",
                record.id,
                record.mediamtx_path,
                record.enabled,
                sanitize_rtsp_url(record.rtsp_url),
            )
            await self._apply(record)
        return self._to_view(record)

    async def update_camera(self, camera_id: str, payload: CameraUpdate) -> CameraView:
        async with self._locks[camera_id]:
            record = await self._require(camera_id)
            provided = payload.provided_fields
            name = payload.name if "name" in provided else record.name
            rtsp_url = payload.rtsp_url if "rtsp_url" in provided else record.rtsp_url
            enabled = payload.enabled if "enabled" in provided else record.enabled

            source_changed = rtsp_url != record.rtsp_url
            enabled_changed = enabled != record.enabled
            if name == record.name and not source_changed and not enabled_changed:
                return self._to_view(record)

            updated = CameraRecord(
                id=record.id,
                name=name,
                rtsp_url=rtsp_url,
                # The path is derived from the id and never changes, so editing
                # a camera never invalidates its WebRTC URL.
                mediamtx_path=record.mediamtx_path,
                enabled=enabled,
                created_at=record.created_at,
                updated_at=utcnow_iso(),
            )
            await asyncio.to_thread(self._repo.update, updated)
            logger.info(
                "camera_updated camera_id=%s name_changed=%s source_changed=%s "
                "enabled=%s",
                updated.id,
                name != record.name,
                source_changed,
                updated.enabled,
            )
            # A rename touches nothing in MediaMTX, so unrelated players - and
            # this camera's own player - keep running.
            if source_changed or enabled_changed:
                await self._apply(updated)
            return self._to_view(updated)

    async def delete_camera(self, camera_id: str) -> None:
        async with self._locks[camera_id]:
            record = await self._require(camera_id)
            await asyncio.to_thread(self._repo.delete, camera_id)
            self._health.pop(camera_id, None)
            logger.info("camera_deleted camera_id=%s", camera_id)
            # Best effort: if MediaMTX is down the path is removed by the next
            # reconcile, which deletes managed paths with no enabled owner.
            try:
                await self._client.delete_path(record.mediamtx_path)
            except MediaMTXError as exc:
                self._note_mediamtx_error(exc)
                self._request_reconcile("delete-failed")
        self._locks.pop(camera_id, None)

    async def set_enabled(self, camera_id: str, enabled: bool) -> CameraView:
        return await self.update_camera(camera_id, CameraUpdate(enabled=enabled))

    # --- reconciliation -------------------------------------------------

    async def _apply(self, record: CameraRecord) -> None:
        """Bring MediaMTX in line with one camera's desired state."""
        try:
            if record.enabled:
                await self._client.ensure_path(record.mediamtx_path, record.rtsp_url)
                self._mediamtx_available = True
                # Ask once now so the response carries real state instead of
                # UNKNOWN; the poll refines it a moment later.
                runtime = await self._client.get_path(record.mediamtx_path)
                self._set_health(
                    record.id,
                    CameraHealthState.ONLINE
                    if runtime.available
                    else CameraHealthState.OFFLINE,
                    None if runtime.available else OFFLINE_HINT,
                )
            else:
                await self._client.delete_path(record.mediamtx_path)
                self._mediamtx_available = True
                self._set_health(record.id, CameraHealthState.UNKNOWN, DISABLED_HINT)
        except PathRejected as exc:
            # MediaMTX will not take this source at all (it parses URLs more
            # strictly than we do). The camera keeps its registration and shows
            # up as OFFLINE, which is the PRD's semantics for a source the VMS
            # cannot obtain.
            message = sanitize_text(str(exc), record.rtsp_url)
            logger.warning("camera_source_rejected camera_id=%s", record.id)
            self._mediamtx_available = True
            self._set_health(record.id, CameraHealthState.OFFLINE, message)
        except MediaMTXError as exc:
            # Keep the desired state; the reconcile loop will retry.
            self._note_mediamtx_error(exc, record.rtsp_url)
            self._set_health(
                record.id,
                CameraHealthState.UNKNOWN,
                sanitize_text(str(exc), record.rtsp_url),
            )
            self._request_reconcile("apply-failed")

    async def reconcile_all(self, reason: str) -> None:
        """Make MediaMTX's managed paths exactly match the enabled cameras."""
        async with self._reconcile_lock:
            pending_before = self._pending_reconcile
            records = await asyncio.to_thread(self._repo.list)
            desired = {r.mediamtx_path: r for r in records if r.enabled}

            try:
                configured = await self._client.list_configured_path_names()
            except MediaMTXError as exc:
                self._note_mediamtx_error(exc)
                self._mark_all_unknown(records, sanitize_text(str(exc)))
                # No retry is queued here: the poll marks MediaMTX unavailable
                # and asks for a reconcile the moment it answers again.
                return

            self._mediamtx_available = True
            stale = [
                name
                for name in configured
                if self._settings.is_managed_path(name) and name not in desired
            ]
            for name in stale:
                try:
                    await self._client.delete_path(name)
                except MediaMTXError as exc:
                    self._note_mediamtx_error(exc)

            for camera_id in [record.id for record in desired.values()]:
                # Per camera, and under the same lock a mutation would take, so
                # a reconcile can never overwrite a concurrent edit with the
                # state it read a moment earlier. One failure never stops the
                # rest: _apply records the problem as health and returns.
                async with self._locks[camera_id]:
                    current = await asyncio.to_thread(self._repo.get, camera_id)
                    if current is None or not current.enabled:
                        continue
                    await self._apply(current)

            self._last_reconcile_at = time.monotonic()
            # A completed pass satisfies whatever was queued when it started -
            # but not a request raised *during* it, which is about a camera this
            # pass already failed on.
            if self._pending_reconcile == pending_before:
                self._pending_reconcile = None
            logger.info(
                "reconciled reason=%s enabled=%d stale_removed=%d",
                reason,
                len(desired),
                len(stale),
            )

    def _request_reconcile(self, reason: str, force: bool = False) -> None:
        if self._pending_reconcile is not None:
            return
        if not force and time.monotonic() - self._last_reconcile_at < (
            self._settings.reconcile_min_interval_seconds
        ):
            return
        logger.info("reconcile_requested reason=%s", reason)
        self._pending_reconcile = reason

    # --- health ---------------------------------------------------------

    async def refresh_health(self) -> None:
        """Refresh every camera's health from one `/v3/paths/list` call.

        The same response doubles as a drift detector, so no extra call is
        needed to notice that MediaMTX was restarted and lost its paths.
        """
        records = await asyncio.to_thread(self._repo.list)
        try:
            paths = await self._client.list_paths()
        except MediaMTXError as exc:
            was_available = self._mediamtx_available
            self._note_mediamtx_error(exc)
            self._mark_all_unknown(records, sanitize_text(str(exc)))
            if was_available:
                logger.warning("mediamtx_unreachable")
            return

        recovered = not self._mediamtx_available
        self._mediamtx_available = True
        if recovered:
            logger.info("mediamtx_recovered")
        if self._mediamtx_version is None or recovered:
            # One extra call, only on the first successful poll and after a
            # restart - a restarted MediaMTX may be a different build.
            try:
                self._mediamtx_version = (await self._client.get_info()).version
            except MediaMTXError:
                self._mediamtx_version = None

        drift = False
        enabled_paths = set()
        for record in records:
            if not record.enabled:
                self._set_health(record.id, CameraHealthState.UNKNOWN, DISABLED_HINT)
                continue
            enabled_paths.add(record.mediamtx_path)
            runtime = paths.get(record.mediamtx_path)
            if runtime is None:
                # Configured here, absent there: MediaMTX lost it.
                drift = True
                self._set_health(
                    record.id,
                    CameraHealthState.UNKNOWN,
                    "MediaMTX has no path for this camera yet.",
                )
            elif runtime.available:
                self._set_health(record.id, CameraHealthState.ONLINE, None)
            else:
                self._set_health(record.id, CameraHealthState.OFFLINE, OFFLINE_HINT)

        orphans = [
            name
            for name in paths
            if self._settings.is_managed_path(name) and name not in enabled_paths
        ]
        known = {record.id for record in records}
        for camera_id in list(self._health):
            if camera_id not in known:
                self._health.pop(camera_id, None)

        if recovered:
            # Never rate-limited: MediaMTX may have restarted empty.
            self._request_reconcile("mediamtx-recovered", force=True)
        elif drift or orphans:
            self._request_reconcile("drift")

    def _set_health(
        self, camera_id: str, state: CameraHealthState, last_error: str | None
    ) -> None:
        self._health[camera_id] = CameraHealth(
            state=state,
            last_error=last_error,
            checked_at=utcnow_iso(),
            mediamtx_available=self._mediamtx_available,
        )

    def _mark_all_unknown(self, records: list[CameraRecord], message: str) -> None:
        self._mediamtx_available = False
        for record in records:
            self._health[record.id] = CameraHealth(
                state=CameraHealthState.UNKNOWN,
                last_error=message if record.enabled else DISABLED_HINT,
                checked_at=utcnow_iso(),
                mediamtx_available=False,
            )

    def _health_of(self, record: CameraRecord) -> CameraHealth:
        health = self._health.get(record.id)
        if health is not None:
            return health
        return CameraHealth(
            state=CameraHealthState.UNKNOWN,
            last_error=None if record.enabled else DISABLED_HINT,
            checked_at=None,
            mediamtx_available=self._mediamtx_available,
        )

    def _note_mediamtx_error(self, exc: Exception, *secrets: str) -> None:
        unreachable = isinstance(exc, MediaMTXUnavailable)
        # An ongoing outage is already known; logging it every couple of
        # seconds would bury everything else.
        repeat = unreachable and not self._mediamtx_available
        if unreachable:
            self._mediamtx_available = False
        logger.log(
            logging.DEBUG if repeat else logging.WARNING,
            "mediamtx_error error=%s",
            sanitize_text(str(exc), *secrets),
        )

    # --- helpers --------------------------------------------------------

    async def _require(self, camera_id: str) -> CameraRecord:
        record = await asyncio.to_thread(self._repo.get, camera_id)
        if record is None:
            raise CameraNotFound(camera_id)
        return record

    def _to_view(self, record: CameraRecord) -> CameraView:
        return CameraView(
            id=record.id,
            name=record.name,
            # The stored URL never leaves the process unmasked.
            rtsp_url_display=sanitize_rtsp_url(record.rtsp_url),
            has_credentials=url_has_credentials(record.rtsp_url),
            enabled=record.enabled,
            mediamtx_path=record.mediamtx_path,
            webrtc_url=self._settings.whep_url(record.mediamtx_path),
            health=self._health_of(record),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
