"""Deterministic domain-object builders shared by the unit tier."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analytics.types import EventCandidate, WorkerConfig
from app.domain.models import (
    CameraRecord,
    CrossingDirection,
    EventRecord,
    LineDirection,
    LineRecord,
    ObjectCategory,
)

BASE_TIME = datetime(2026, 9, 7, 9, 15, 14, 123000, tzinfo=UTC)


def camera(
    camera_id: str = "acam_0123abcd",
    *,
    vms_camera_id: str = "cam_0123abcd",
    name: str = "Loading Bay",
    rtsp_url: str = "rtsp://host.docker.internal:8555/vms_cam_0123abcd",
    enabled: bool = True,
    inference_fps: float = 5.0,
    confidence_threshold: float = 0.25,
    classes: frozenset[ObjectCategory] | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> CameraRecord:
    return CameraRecord(
        id=camera_id,
        vms_camera_id=vms_camera_id,
        name=name,
        rtsp_url=rtsp_url,
        enabled=enabled,
        inference_fps=inference_fps,
        confidence_threshold=confidence_threshold,
        enabled_classes=classes
        or frozenset({ObjectCategory.PERSON, ObjectCategory.VEHICLE}),
        created_at=created_at or BASE_TIME,
        updated_at=updated_at or BASE_TIME,
    )


def line(
    line_id: str = "line_00000001",
    camera_id: str = "acam_0123abcd",
    *,
    name: str = "Entry line",
    a: tuple[float, float] = (0.2, 0.5),
    b: tuple[float, float] = (0.8, 0.5),
    direction: LineDirection = LineDirection.A_TO_B,
    enabled: bool = True,
) -> LineRecord:
    return LineRecord(
        id=line_id,
        camera_id=camera_id,
        name=name,
        x1=a[0],
        y1=a[1],
        x2=b[0],
        y2=b[1],
        direction=direction,
        enabled=enabled,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )


def worker_config(
    *,
    camera_record: CameraRecord | None = None,
    line_record: LineRecord | None = None,
    revision: int = 1,
    session_id: str = "ws_" + "1" * 32,
) -> WorkerConfig:
    record = camera_record or camera()
    return WorkerConfig(
        camera_id=record.id,
        vms_camera_id=record.vms_camera_id,
        camera_name=record.name,
        rtsp_url=record.rtsp_url,
        inference_fps=record.inference_fps,
        confidence_threshold=record.confidence_threshold,
        enabled_categories=record.enabled_classes,
        line=line_record,
        config_revision=revision,
        requested_session_id=session_id,
    )


def event(
    event_id: str = "evt_" + "0" * 32,
    *,
    camera_id: str = "acam_0123abcd",
    vms_camera_id: str = "cam_0123abcd",
    line_id: str = "line_00000001",
    session_id: str = "ws_" + "1" * 32,
    track_id: int = 7,
    direction: CrossingDirection = CrossingDirection.A_TO_B,
    object_class: str = "truck",
    category: ObjectCategory = ObjectCategory.VEHICLE,
    crossed_at: datetime | None = None,
    offset_ms: int = 0,
) -> EventRecord:
    moment = crossed_at or (BASE_TIME + timedelta(milliseconds=offset_ms))
    return EventRecord(
        id=event_id,
        camera_id=camera_id,
        vms_camera_id=vms_camera_id,
        camera_name="Loading Bay",
        line_id=line_id,
        line_name="Entry line",
        worker_session_id=session_id,
        track_id=track_id,
        object_category=category,
        object_class=object_class,
        direction=direction,
        confidence=0.91,
        crossed_at=moment,
        bbox_x1=0.10,
        bbox_y1=0.20,
        bbox_x2=0.30,
        bbox_y2=0.60,
        centroid_x=0.20,
        centroid_y=0.40,
        frame_width=1920,
        frame_height=1080,
        source_pts_ns=None,
        snapshot_path=f"events/{camera_id}/2026-09-07/{event_id}/frame.jpg",
        crop_path=f"events/{camera_id}/2026-09-07/{event_id}/crop.jpg",
        created_at=moment,
    )


def rgb_frame(width: int = 64, height: int = 48, value: int = 128):
    import numpy as np

    frame = np.full((height, width, 3), value, dtype=np.uint8)
    frame[: height // 2, : width // 2] = 240
    return frame


def candidate(
    *,
    camera_id: str = "acam_0123abcd",
    line_id: str = "line_00000001",
    session_id: str = "ws_" + "1" * 32,
    track_id: int = 7,
    direction: CrossingDirection = CrossingDirection.A_TO_B,
    bbox: tuple[float, float, float, float] = (4.0, 6.0, 40.0, 30.0),
    width: int = 64,
    height: int = 48,
    frame=None,
    crossed_at: datetime | None = None,
) -> EventCandidate:
    return EventCandidate(
        camera_id=camera_id,
        vms_camera_id="cam_0123abcd",
        camera_name="Loading Bay",
        line_id=line_id,
        line_name="Entry line",
        worker_session_id=session_id,
        track_id=track_id,
        object_category=ObjectCategory.VEHICLE,
        object_class="truck",
        direction=direction,
        confidence=0.91,
        crossed_at=crossed_at or BASE_TIME,
        bbox_pixels=bbox,
        centroid_normalized=(0.5, 0.5),
        frame_width=width,
        frame_height=height,
        source_pts_ns=None,
        rgb=frame if frame is not None else rgb_frame(width, height),
    )
