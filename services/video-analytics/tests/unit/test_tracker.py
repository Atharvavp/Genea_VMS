"""ByteTrack adapter contract (PLAN section 17)."""

from __future__ import annotations

import numpy as np
import pytest

from app.analytics.tracker import (
    HIGH_CONFIDENCE_FLOOR,
    MINIMUM_CONSECUTIVE_FRAMES,
    MINIMUM_IOU_THRESHOLD,
    ByteTrackAdapter,
    TrackerInvariantError,
)
from app.analytics.types import Detection
from app.domain.models import ObjectCategory

pytestmark = pytest.mark.unit

FRAME = np.zeros((48, 64, 3), dtype=np.uint8)


class _RecordingTracker:
    """Captures constructor and update arguments; returns scripted results."""

    instances: list["_RecordingTracker"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.updates: list[tuple] = []
        self.result = None
        self.reset_calls = 0
        _RecordingTracker.instances.append(self)

    def update(self, detections, timestamp=None):
        self.updates.append((detections, timestamp))
        return self.result if self.result is not None else detections

    def reset(self):
        self.reset_calls += 1


def _adapter(fps=5.0, confidence=0.25) -> ByteTrackAdapter:
    return ByteTrackAdapter(
        inference_fps=fps,
        confidence_threshold=confidence,
        tracker_factory=_RecordingTracker,
    )


def _detection(x=1.0, coco=0, object_class="person", category=ObjectCategory.PERSON):
    return Detection(
        x1=x,
        y1=1.0,
        x2=x + 10.0,
        y2=11.0,
        confidence=0.9,
        coco_class_id=coco,
        object_class=object_class,
        object_category=category,
    )


# -- construction -----------------------------------------------------------


def test_every_constructor_value_is_passed_explicitly():
    adapter = _adapter(fps=7.0, confidence=0.4)
    assert adapter.constructor_kwargs == {
        "lost_track_buffer": 30,
        "frame_rate": 7.0,
        "track_activation_threshold": 0.4,
        "minimum_consecutive_frames": MINIMUM_CONSECUTIVE_FRAMES,
        "minimum_iou_threshold": MINIMUM_IOU_THRESHOLD,
        "high_conf_det_threshold": HIGH_CONFIDENCE_FLOOR,
    }


def test_camera_confidence_is_the_activation_floor():
    assert _adapter(confidence=0.55).constructor_kwargs[
        "track_activation_threshold"
    ] == 0.55


def test_the_high_confidence_threshold_never_drops_below_its_floor():
    assert _adapter(confidence=0.1).constructor_kwargs["high_conf_det_threshold"] == 0.6


def test_the_high_confidence_threshold_never_drops_below_activation():
    assert _adapter(confidence=0.9).constructor_kwargs["high_conf_det_threshold"] == 0.9


def test_a_rejected_parameter_set_is_a_typed_invariant_error():
    class _Picky:
        def __init__(self, **_kwargs):
            raise TypeError("unexpected keyword argument")

    with pytest.raises(TrackerInvariantError):
        ByteTrackAdapter(
            inference_fps=5.0, confidence_threshold=0.25, tracker_factory=_Picky
        )


# -- detection conversion ---------------------------------------------------


def test_an_empty_detection_list_has_the_exact_shapes():
    adapter = _adapter()
    adapter.update([], frame=FRAME, timestamp=1.0)
    sent, _timestamp = adapter._tracker.updates[0]  # noqa: SLF001
    assert sent.xyxy.shape == (0, 4)
    assert sent.confidence.shape == (0,)
    assert sent.class_id.shape == (0,)
    assert len(sent) == 0


def test_detections_are_converted_with_their_custom_data():
    adapter = _adapter()
    adapter.update(
        [_detection(), _detection(x=30.0, coco=7, object_class="truck",
                                 category=ObjectCategory.VEHICLE)],
        frame=FRAME,
        timestamp=2.5,
    )
    sent, timestamp = adapter._tracker.updates[0]  # noqa: SLF001
    assert timestamp == 2.5
    assert sent.xyxy.shape == (2, 4)
    assert list(sent.class_id) == [0, 7]
    assert list(sent.data["object_class"]) == ["person", "truck"]
    assert list(sent.data["object_category"]) == ["person", "vehicle"]


def test_monotonic_timestamps_are_forwarded_verbatim():
    adapter = _adapter()
    for moment in (10.0, 10.4, 11.9):
        adapter.update([_detection()], frame=FRAME, timestamp=moment)
    assert [update[1] for update in adapter._tracker.updates] == [10.0, 10.4, 11.9]  # noqa: SLF001


# -- result conversion ------------------------------------------------------


def _result(tracker_ids, classes=("person",), categories=("person",), count=None):
    import supervision as sv

    size = count if count is not None else len(tracker_ids)
    return sv.Detections(
        xyxy=np.array([[1.0, 1.0, 11.0, 11.0]] * size, dtype=np.float32),
        confidence=np.array([0.9] * size, dtype=np.float32),
        class_id=np.array([0] * size, dtype=int),
        tracker_id=np.array(tracker_ids, dtype=int),
        data={
            "object_class": np.array(list(classes) * size, dtype=object)[:size],
            "object_category": np.array(list(categories) * size, dtype=object)[:size],
        },
    )


def test_confirmed_tracks_are_returned_with_preserved_data():
    adapter = _adapter()
    adapter._tracker.result = _result([3])  # noqa: SLF001
    tracked = adapter.update([_detection()], frame=FRAME, timestamp=1.0)
    assert len(tracked) == 1
    assert tracked[0].track_id == 3
    assert tracked[0].object_class == "person"
    assert tracked[0].object_category is ObjectCategory.PERSON
    assert tracked[0].confidence == pytest.approx(0.9)
    assert (tracked[0].x1, tracked[0].y2) == (1.0, 11.0)


def test_unconfirmed_negative_ids_are_filtered_out():
    adapter = _adapter()
    adapter._tracker.result = _result([-1, 4, -1])  # noqa: SLF001
    tracked = adapter.update([_detection()], frame=FRAME, timestamp=1.0)
    assert [item.track_id for item in tracked] == [4]


def test_a_result_without_tracker_ids_returns_nothing():
    import supervision as sv

    adapter = _adapter()
    adapter._tracker.result = sv.Detections.empty()  # noqa: SLF001
    assert adapter.update([], frame=FRAME, timestamp=1.0) == []


def test_a_duplicate_track_id_in_one_update_is_fatal():
    adapter = _adapter()
    adapter._tracker.result = _result([5, 5])  # noqa: SLF001
    with pytest.raises(TrackerInvariantError):
        adapter.update([_detection()], frame=FRAME, timestamp=1.0)


def test_an_update_exception_becomes_a_typed_invariant_error():
    adapter = _adapter()

    def boom(*_a, **_k):
        raise RuntimeError("tracker exploded")

    adapter._tracker.update = boom  # noqa: SLF001
    with pytest.raises(TrackerInvariantError):
        adapter.update([], frame=FRAME, timestamp=1.0)


def test_an_unknown_category_in_the_result_is_fatal():
    adapter = _adapter()
    adapter._tracker.result = _result([1], categories=("aircraft",))  # noqa: SLF001
    with pytest.raises(TrackerInvariantError):
        adapter.update([_detection()], frame=FRAME, timestamp=1.0)


# -- isolation and lifecycle ------------------------------------------------


def test_two_adapters_own_independent_tracker_instances():
    first, second = _adapter(), _adapter()
    assert first._tracker is not second._tracker  # noqa: SLF001


def test_reset_clears_continuity_on_this_instance_only():
    first, second = _adapter(), _adapter()
    first._tracker.result = _result([1])  # noqa: SLF001
    first.update([_detection()], frame=FRAME, timestamp=1.0)
    assert first.issued_track_ids == frozenset({1})
    first.reset()
    assert first.issued_track_ids == frozenset()
    assert first._tracker.reset_calls == 1  # noqa: SLF001
    assert second._tracker.reset_calls == 0  # noqa: SLF001


def test_active_ids_fall_back_to_the_last_returned_set():
    adapter = _adapter()
    adapter._tracker.result = _result([2, 3])  # noqa: SLF001
    adapter.update([_detection()], frame=FRAME, timestamp=1.0)
    assert adapter.active_track_ids() >= {2, 3}


def test_issued_ids_accumulate_within_a_session():
    adapter = _adapter()
    adapter._tracker.result = _result([1])  # noqa: SLF001
    adapter.update([_detection()], frame=FRAME, timestamp=1.0)
    adapter._tracker.result = _result([2])  # noqa: SLF001
    adapter.update([_detection()], frame=FRAME, timestamp=1.2)
    assert adapter.issued_track_ids == frozenset({1, 2})
