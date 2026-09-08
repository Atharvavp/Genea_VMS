"""Desired-state orchestration: mutations, reconciliation, health and isolation."""

from __future__ import annotations

import pytest

from app.domain.models import (
    CameraCreate,
    CameraHealthState,
    CameraUpdate,
    RecordingState,
)
from app.services.camera_manager import CameraManager, CameraNotFound
from tests.conftest import CREDENTIALED_URL, SIMULATOR_URL

OTHER_URL = "rtsp://host.docker.internal:8554/simulator/dock"


async def add(
    manager: CameraManager,
    name="Lobby",
    url=SIMULATOR_URL,
    enabled=True,
    recording_enabled=False,
):
    return await manager.create_camera(
        CameraCreate(
            name=name,
            rtsp_url=url,
            enabled=enabled,
            recording_enabled=recording_enabled,
        )
    )


# --- create -----------------------------------------------------------------


async def test_create_enabled_camera_configures_one_path(manager, fake_mediamtx):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    view = await add(manager)

    assert view.mediamtx_path == f"vms_{view.id}"
    assert view.webrtc_url == f"http://localhost:8889/vms_{view.id}/whep"
    assert fake_mediamtx.paths == {view.mediamtx_path: SIMULATOR_URL}
    assert view.health.state is CameraHealthState.ONLINE


async def test_create_accepts_an_offline_source_and_reports_it_offline(
    manager, fake_mediamtx
):
    view = await add(manager, url="rtsp://192.0.2.1:554/nothing")
    assert view.enabled is True
    assert view.health.state is CameraHealthState.OFFLINE
    assert view.mediamtx_path in fake_mediamtx.paths


async def test_create_disabled_camera_configures_nothing(manager, fake_mediamtx):
    view = await add(manager, enabled=False)
    assert fake_mediamtx.paths == {}
    assert view.health.state is CameraHealthState.UNKNOWN


async def test_create_never_returns_the_raw_credentialed_url(manager):
    view = await add(manager, url=CREDENTIALED_URL)
    assert view.has_credentials is True
    assert view.rtsp_url_display == "rtsp://admin:***@10.0.0.9:554/Streaming/Channels/101"
    assert "hunter2" not in view.model_dump_json()


async def test_create_sends_the_real_url_to_mediamtx(manager, fake_mediamtx):
    view = await add(manager, url=CREDENTIALED_URL)
    # MediaMTX is the one consumer that needs the credentials.
    assert fake_mediamtx.paths[view.mediamtx_path] == CREDENTIALED_URL


async def test_create_survives_mediamtx_being_down(manager, fake_mediamtx):
    fake_mediamtx.go_down()
    view = await add(manager)
    assert view.health.state is CameraHealthState.UNKNOWN
    assert view.health.mediamtx_available is False
    # The registration is kept: reconciliation applies it when MediaMTX is back.
    assert [item.id for item in await manager.list_cameras()] == [view.id]


async def test_create_keeps_a_camera_mediamtx_rejects_and_marks_it_offline(
    manager, fake_mediamtx
):
    fake_mediamtx.rejected_sources.add(SIMULATOR_URL)
    view = await add(manager)
    assert view.health.state is CameraHealthState.OFFLINE
    assert "invalid source" in view.health.last_error


# --- update -----------------------------------------------------------------


async def test_rename_does_not_touch_mediamtx(manager, fake_mediamtx):
    view = await add(manager)
    fake_mediamtx.calls.clear()

    renamed = await manager.update_camera(view.id, CameraUpdate(name="Front Door"))

    assert renamed.name == "Front Door"
    assert fake_mediamtx.calls == []  # no path churn, so no player restart
    assert fake_mediamtx.paths == {view.mediamtx_path: SIMULATOR_URL}


async def test_changing_the_url_replaces_the_same_path(manager, fake_mediamtx):
    view = await add(manager)
    updated = await manager.update_camera(view.id, CameraUpdate(rtsp_url=OTHER_URL))

    assert updated.mediamtx_path == view.mediamtx_path  # id-derived, so stable
    assert updated.webrtc_url == view.webrtc_url
    assert fake_mediamtx.paths == {view.mediamtx_path: OTHER_URL}
    assert fake_mediamtx.calls_for("ensure") == [view.mediamtx_path] * 2


async def test_omitting_the_url_keeps_the_stored_credentialed_source(
    manager, fake_mediamtx
):
    view = await add(manager, url=CREDENTIALED_URL)
    await manager.update_camera(view.id, CameraUpdate(name="Renamed"))
    assert fake_mediamtx.paths[view.mediamtx_path] == CREDENTIALED_URL


async def test_no_op_update_changes_nothing(manager, fake_mediamtx):
    view = await add(manager)
    fake_mediamtx.calls.clear()
    again = await manager.update_camera(view.id, CameraUpdate(name=view.name))
    assert again.updated_at == view.updated_at
    assert fake_mediamtx.calls == []


async def test_update_missing_camera_raises(manager):
    with pytest.raises(CameraNotFound):
        await manager.update_camera("cam_deadbeef", CameraUpdate(name="x"))


# --- enable / disable -------------------------------------------------------


async def test_disable_removes_the_path_and_enable_recreates_it(manager, fake_mediamtx):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    view = await add(manager)

    disabled = await manager.set_enabled(view.id, False)
    assert disabled.enabled is False
    assert fake_mediamtx.paths == {}
    assert disabled.health.state is CameraHealthState.UNKNOWN

    enabled = await manager.set_enabled(view.id, True)
    assert enabled.enabled is True
    assert fake_mediamtx.paths == {view.mediamtx_path: SIMULATOR_URL}
    assert enabled.health.state is CameraHealthState.ONLINE
    # Same id, same path, same WebRTC URL: the player reconnects, it is not new.
    assert enabled.webrtc_url == view.webrtc_url


async def test_disable_is_idempotent(manager, fake_mediamtx):
    view = await add(manager, enabled=False)
    fake_mediamtx.calls.clear()
    await manager.set_enabled(view.id, False)
    assert fake_mediamtx.calls == []


# --- delete -----------------------------------------------------------------


async def test_delete_removes_row_path_and_health(manager, fake_mediamtx):
    keep = await add(manager, name="Keep")
    drop = await add(manager, name="Drop", url=OTHER_URL)

    await manager.delete_camera(drop.id)

    assert [item.id for item in await manager.list_cameras()] == [keep.id]
    assert set(fake_mediamtx.paths) == {keep.mediamtx_path}
    with pytest.raises(CameraNotFound):
        await manager.get_camera(drop.id)


async def test_delete_while_mediamtx_is_down_still_removes_the_camera(
    manager, fake_mediamtx
):
    view = await add(manager)
    fake_mediamtx.go_down()

    await manager.delete_camera(view.id)
    assert await manager.list_cameras() == []

    # The orphaned path is cleaned up once MediaMTX answers again.
    fake_mediamtx.come_back()
    await manager.refresh_health()
    await manager.reconcile_all("test")
    assert fake_mediamtx.paths == {}


async def test_delete_missing_camera_raises(manager):
    with pytest.raises(CameraNotFound):
        await manager.delete_camera("cam_deadbeef")


# --- health -----------------------------------------------------------------


async def test_health_tracks_the_source_appearing_and_disappearing(
    manager, fake_mediamtx
):
    view = await add(manager)
    await manager.refresh_health()
    assert (await manager.get_health(view.id)).state is CameraHealthState.OFFLINE

    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    await manager.refresh_health()
    assert (await manager.get_health(view.id)).state is CameraHealthState.ONLINE

    fake_mediamtx.reachable_sources.clear()
    await manager.refresh_health()
    health = await manager.get_health(view.id)
    assert health.state is CameraHealthState.OFFLINE
    # No camera recreation: the path is still configured, waiting to reconnect.
    assert view.mediamtx_path in fake_mediamtx.paths


async def test_health_is_unknown_while_mediamtx_is_unreachable(manager, fake_mediamtx):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    view = await add(manager)
    await manager.refresh_health()

    fake_mediamtx.go_down()
    await manager.refresh_health()

    health = await manager.get_health(view.id)
    assert health.state is CameraHealthState.UNKNOWN
    assert health.mediamtx_available is False


async def test_one_offline_camera_does_not_affect_another(manager, fake_mediamtx):
    good = await add(manager, name="Good")
    bad = await add(manager, name="Bad", url="rtsp://192.0.2.1:554/nope")
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)

    await manager.refresh_health()

    assert (await manager.get_health(good.id)).state is CameraHealthState.ONLINE
    assert (await manager.get_health(bad.id)).state is CameraHealthState.OFFLINE


async def test_health_is_not_persisted(manager, repository, fake_mediamtx):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    view = await add(manager)
    await manager.refresh_health()

    # A fresh manager over the same database starts with no health knowledge.
    fresh = CameraManager(manager._settings, repository, fake_mediamtx)
    assert (await fresh.get_health(view.id)).state is CameraHealthState.UNKNOWN
    assert (await fresh.get_health(view.id)).checked_at is None


# --- reconciliation ---------------------------------------------------------


async def test_startup_reconcile_adds_enabled_and_drops_disabled(
    manager, fake_mediamtx
):
    enabled = await add(manager, name="On")
    disabled = await add(manager, name="Off", url=OTHER_URL, enabled=False)
    fake_mediamtx.configs.clear()

    await manager.reconcile_all("startup")

    assert set(fake_mediamtx.paths) == {enabled.mediamtx_path}
    assert disabled.mediamtx_path not in fake_mediamtx.paths


async def test_reconcile_deletes_stale_managed_paths_only(manager, fake_mediamtx):
    view = await add(manager)
    fake_mediamtx.plant_path("vms_cam_ffffffff", "rtsp://gone/old")  # ours, orphaned
    fake_mediamtx.plant_path("simulator_lobby", "rtsp://other/x")  # not ours

    await manager.reconcile_all("test")

    assert set(fake_mediamtx.paths) == {view.mediamtx_path, "simulator_lobby"}


async def test_reconcile_when_mediamtx_is_down_keeps_desired_state(
    manager, fake_mediamtx
):
    view = await add(manager)
    fake_mediamtx.go_down()

    await manager.reconcile_all("test")

    assert (await manager.get_health(view.id)).state is CameraHealthState.UNKNOWN
    fake_mediamtx.come_back()
    await manager.reconcile_all("test")
    assert set(fake_mediamtx.paths) == {view.mediamtx_path}


async def test_health_poll_does_not_reconcile_when_nothing_drifted(
    manager, fake_mediamtx
):
    """The 2 s poll is a read, not a reconcile."""
    await add(manager)
    await manager.reconcile_all("startup")
    fake_mediamtx.calls.clear()

    await manager.refresh_health()

    assert fake_mediamtx.calls == [("list_paths", "")]
    assert manager._pending_reconcile is None


async def test_health_poll_detects_a_mediamtx_restart_and_asks_for_a_reconcile(
    manager, fake_mediamtx
):
    view = await add(manager)
    fake_mediamtx.configs.clear()  # MediaMTX restarted and lost its paths

    await manager.refresh_health()
    assert manager._pending_reconcile is not None

    await manager.reconcile_all(manager._pending_reconcile)
    assert set(fake_mediamtx.paths) == {view.mediamtx_path}


async def test_health_poll_detects_an_orphaned_managed_path(manager, fake_mediamtx):
    fake_mediamtx.plant_path("vms_cam_ffffffff", "rtsp://gone/old")
    await manager.refresh_health()
    assert manager._pending_reconcile is not None


async def test_mediamtx_recovery_triggers_a_reconcile(manager, fake_mediamtx):
    view = await add(manager)
    fake_mediamtx.go_down()
    await manager.refresh_health()
    manager._pending_reconcile = None

    fake_mediamtx.come_back(wipe_paths=True)
    await manager.refresh_health()

    assert manager._pending_reconcile == "mediamtx-recovered"
    await manager.reconcile_all("mediamtx-recovered")
    assert set(fake_mediamtx.paths) == {view.mediamtx_path}


async def test_drift_reconciles_are_rate_limited(manager, fake_mediamtx, settings):
    settings.reconcile_min_interval_seconds = 3600.0
    await add(manager)
    await manager.reconcile_all("startup")

    fake_mediamtx.configs.clear()
    await manager.refresh_health()

    # Drift is real, but a reconcile just ran: it waits rather than spinning.
    assert manager._pending_reconcile is None


async def test_recovery_reconcile_ignores_the_rate_limit(
    manager, fake_mediamtx, settings
):
    settings.reconcile_min_interval_seconds = 3600.0
    await add(manager)
    await manager.reconcile_all("startup")

    fake_mediamtx.go_down()
    await manager.refresh_health()
    fake_mediamtx.come_back(wipe_paths=True)
    await manager.refresh_health()

    assert manager._pending_reconcile == "mediamtx-recovered"


async def test_reconcile_reflects_a_url_change_made_while_mediamtx_was_down(
    manager, fake_mediamtx
):
    view = await add(manager)
    fake_mediamtx.go_down()
    await manager.update_camera(view.id, CameraUpdate(rtsp_url=OTHER_URL))

    fake_mediamtx.come_back(wipe_paths=True)
    await manager.reconcile_all("test")

    assert fake_mediamtx.paths == {view.mediamtx_path: OTHER_URL}


async def test_reconcile_continues_past_a_rejected_source(manager, fake_mediamtx):
    bad = await add(manager, name="Bad", url="rtsp://bad/one")
    good = await add(manager, name="Good", url=SIMULATOR_URL)
    fake_mediamtx.rejected_sources.add("rtsp://bad/one")
    fake_mediamtx.configs.clear()

    await manager.reconcile_all("test")

    assert set(fake_mediamtx.paths) == {good.mediamtx_path}
    assert (await manager.get_health(bad.id)).state is CameraHealthState.OFFLINE


# --- recording --------------------------------------------------------------


async def test_recording_defaults_to_off_and_the_path_says_so(manager, fake_mediamtx):
    view = await add(manager)
    assert view.recording_enabled is False
    assert view.recording.state is RecordingState.DISABLED
    assert fake_mediamtx.records(view.mediamtx_path) is False


async def test_every_managed_path_carries_the_full_recording_block(
    manager, fake_mediamtx, settings
):
    """Even with recording off - a partial payload would hide existing history."""
    view = await add(manager)

    config = fake_mediamtx.recording_config_of(view.mediamtx_path)
    assert config is not None
    assert config.record_path == settings.record_path_template
    assert config.segment_duration == settings.recording_segment_duration
    assert config.delete_after == settings.record_delete_after_duration
    assert config.record_format == "fmp4"


async def test_recording_on_marks_the_path_and_infers_recording(manager, fake_mediamtx):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    view = await add(manager, recording_enabled=True)

    assert fake_mediamtx.records(view.mediamtx_path) is True
    assert view.recording.state is RecordingState.RECORDING


async def test_recording_waits_while_the_source_is_missing(manager, fake_mediamtx):
    """Requested, configured, but MediaMTX has nothing to write yet."""
    view = await add(manager, url="rtsp://192.0.2.1:554/nope", recording_enabled=True)
    await manager.refresh_health()

    status = (await manager.get_camera(view.id)).recording
    assert status.state is RecordingState.WAITING
    assert status.last_error


async def test_toggling_recording_keeps_the_live_path_configured(
    manager, fake_mediamtx
):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    view = await add(manager)

    await manager.update_camera(view.id, CameraUpdate(recording_enabled=True))
    assert fake_mediamtx.records(view.mediamtx_path) is True

    off = await manager.update_camera(view.id, CameraUpdate(recording_enabled=False))
    assert fake_mediamtx.records(view.mediamtx_path) is False
    # Live is configured identically either way, and the path is never dropped.
    assert fake_mediamtx.paths[view.mediamtx_path] == SIMULATOR_URL
    assert view.mediamtx_path not in fake_mediamtx.calls_for("delete")
    assert off.webrtc_url == view.webrtc_url


async def test_toggling_recording_is_a_no_op_when_unchanged(manager, fake_mediamtx):
    view = await add(manager, recording_enabled=True)
    fake_mediamtx.calls.clear()
    await manager.update_camera(view.id, CameraUpdate(recording_enabled=True))
    assert fake_mediamtx.calls == []


async def test_disable_keeps_the_preference_and_re_enable_resumes(
    manager, fake_mediamtx
):
    """No operator retoggle: the preference outlives the disable."""
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    view = await add(manager, recording_enabled=True)

    disabled = await manager.set_enabled(view.id, False)
    assert disabled.recording_enabled is True
    assert disabled.recording.state is RecordingState.DISABLED
    assert fake_mediamtx.paths == {}

    enabled = await manager.set_enabled(view.id, True)
    assert enabled.recording_enabled is True
    assert enabled.recording.state is RecordingState.RECORDING
    assert fake_mediamtx.records(view.mediamtx_path) is True


async def test_a_source_loss_and_return_resumes_recording_by_itself(
    manager, fake_mediamtx
):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    view = await add(manager, recording_enabled=True)
    await manager.refresh_health()
    assert (await manager.get_camera(view.id)).recording.state is RecordingState.RECORDING

    fake_mediamtx.reachable_sources.clear()
    await manager.refresh_health()
    assert (await manager.get_camera(view.id)).recording.state is RecordingState.WAITING
    # The path stays configured, so existing history stays listable.
    assert view.mediamtx_path in fake_mediamtx.paths

    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    await manager.refresh_health()
    assert (await manager.get_camera(view.id)).recording.state is RecordingState.RECORDING
    assert fake_mediamtx.records(view.mediamtx_path) is True


async def test_a_control_plane_failure_is_a_recording_error(manager, fake_mediamtx):
    fake_mediamtx.go_down()
    view = await add(manager, recording_enabled=True)

    status = (await manager.get_camera(view.id)).recording
    assert status.state is RecordingState.ERROR
    assert status.last_error


async def test_a_recording_error_clears_once_the_config_is_applied(
    manager, fake_mediamtx
):
    fake_mediamtx.go_down()
    view = await add(manager, recording_enabled=True)
    assert (await manager.get_camera(view.id)).recording.state is RecordingState.ERROR

    fake_mediamtx.come_back()
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    await manager.reconcile_all("mediamtx-recovered")

    assert (await manager.get_camera(view.id)).recording.state is RecordingState.RECORDING


async def test_recording_state_never_reflects_a_playback_failure(
    manager, fake_mediamtx
):
    """Historical playback is a separate axis and never reaches this state."""
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    view = await add(manager, recording_enabled=True)
    await manager.refresh_health()

    fake_mediamtx.playback_up = False

    assert (await manager.get_camera(view.id)).recording.state is RecordingState.RECORDING


async def test_reconcile_restores_recording_after_a_mediamtx_restart(
    manager, fake_mediamtx
):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    view = await add(manager, recording_enabled=True)

    fake_mediamtx.come_back(wipe_paths=True)  # MediaMTX lost its dynamic paths
    await manager.refresh_health()
    await manager.reconcile_all("drift")

    assert fake_mediamtx.records(view.mediamtx_path) is True
    assert (
        fake_mediamtx.recording_config_of(view.mediamtx_path).record_path
        == manager._settings.record_path_template
    )


async def test_a_backend_restart_leaves_a_matching_path_alone(
    manager, repository, settings, fake_mediamtx
):
    """A healthy recorder must not be interrupted just because the VMS restarted."""
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    view = await add(manager, recording_enabled=True)

    fresh = CameraManager(settings, repository, fake_mediamtx)
    fake_mediamtx.calls.clear()
    await fresh.reconcile_all("startup")

    # Config already matches, so no replacement: live and recording continue.
    assert fake_mediamtx.calls_for("ensure") == []
    assert fake_mediamtx.records(view.mediamtx_path) is True


async def test_a_backend_restart_reapplies_a_path_that_drifted(
    manager, repository, settings, fake_mediamtx
):
    view = await add(manager, recording_enabled=True)
    # Something reset the recording config the way a partial replace would.
    fake_mediamtx.plant_path(view.mediamtx_path, SIMULATOR_URL, record=False)

    fresh = CameraManager(settings, repository, fake_mediamtx)
    await fresh.reconcile_all("startup")

    assert fake_mediamtx.records(view.mediamtx_path) is True


async def test_one_camera_recording_failure_does_not_affect_another(
    manager, fake_mediamtx
):
    good = await add(manager, name="Good", recording_enabled=True)
    bad = await add(manager, name="Bad", url="rtsp://bad/one", recording_enabled=True)
    fake_mediamtx.rejected_sources.add("rtsp://bad/one")
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    fake_mediamtx.configs.clear()

    await manager.reconcile_all("test")

    assert fake_mediamtx.records(good.mediamtx_path) is True
    assert (await manager.get_camera(good.id)).recording.state is RecordingState.RECORDING
    assert (await manager.get_camera(bad.id)).recording.state is RecordingState.ERROR


async def test_recording_state_is_not_persisted(manager, repository, fake_mediamtx):
    """The preference persists; the inferred runtime state does not."""
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    view = await add(manager, recording_enabled=True)
    await manager.refresh_health()
    assert (await manager.get_camera(view.id)).recording.state is RecordingState.RECORDING

    fresh = CameraManager(manager._settings, repository, fake_mediamtx)
    reloaded = await fresh.get_camera(view.id)
    assert reloaded.recording_enabled is True  # desired state survives
    assert reloaded.recording.state is RecordingState.WAITING  # nothing observed yet


# --- report -----------------------------------------------------------------


async def test_health_report(manager, fake_mediamtx):
    fake_mediamtx.reachable_sources.add(SIMULATOR_URL)
    await add(manager)
    await add(manager, name="Off", url=OTHER_URL, enabled=False)
    await manager.refresh_health()

    report = await manager.health_report()
    assert report["status"] == "ok"
    assert report["database"] == "ok"
    assert report["mediamtx"]["reachable"] is True
    assert report["cameras"] == {"total": 2, "enabled": 1, "online": 1}


async def test_health_report_is_degraded_without_mediamtx(manager, fake_mediamtx):
    fake_mediamtx.go_down()
    await manager.refresh_health()
    report = await manager.health_report()
    assert report["status"] == "degraded"
    assert report["mediamtx"]["reachable"] is False


async def test_a_reconcile_requested_mid_pass_is_not_swallowed(manager, fake_mediamtx):
    """A camera that failed during a pass still gets another attempt."""
    view = await add(manager)
    # The poll loop clears the queue before it starts a pass; match that.
    manager._pending_reconcile = None

    original_apply = manager._apply

    async def failing_apply(record, **kwargs):
        # Model MediaMTX dropping out for this one camera mid-reconcile.
        manager._request_reconcile("apply-failed")
        await original_apply(record, **kwargs)

    manager._apply = failing_apply
    await manager.reconcile_all("startup")

    assert manager._pending_reconcile == "apply-failed"
    assert view.mediamtx_path in fake_mediamtx.paths


async def test_an_ongoing_outage_is_not_logged_every_poll(manager, fake_mediamtx, caplog):
    await add(manager)
    fake_mediamtx.go_down()

    with caplog.at_level("WARNING", logger="app.services.camera_manager"):
        await manager.refresh_health()
        first = len(caplog.records)
        await manager.refresh_health()
        await manager.refresh_health()
        assert len(caplog.records) == first, [r.message for r in caplog.records]
    assert first  # the first failure is reported
