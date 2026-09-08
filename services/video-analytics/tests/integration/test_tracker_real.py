"""The real Roboflow ByteTrack implementation with externally supplied boxes."""

from __future__ import annotations

import numpy as np
import pytest

from app.analytics.tracker import (
    HIGH_CONFIDENCE_FLOOR,
    MINIMUM_CONSECUTIVE_FRAMES,
    ByteTrackAdapter,
)
from app.analytics.types import Detection
from app.domain.models import ObjectCategory

pytestmark = pytest.mark.integration

FRAME = np.zeros((240, 320, 3), dtype=np.uint8)


def moving_person(step: int, *, confidence: float = 0.9, y: float = 100.0) -> Detection:
    x = 20.0 + step * 8.0
    return Detection(
        x1=x,
        y1=y,
        x2=x + 30.0,
        y2=y + 60.0,
        confidence=confidence,
        coco_class_id=0,
        object_class="person",
        object_category=ObjectCategory.PERSON,
    )


def truck(step: int) -> Detection:
    x = 200.0 - step * 6.0
    return Detection(
        x1=x,
        y1=40.0,
        x2=x + 50.0,
        y2=100.0,
        confidence=0.95,
        coco_class_id=7,
        object_class="truck",
        object_category=ObjectCategory.VEHICLE,
    )


def adapter(fps: float = 5.0, confidence: float = 0.25) -> ByteTrackAdapter:
    return ByteTrackAdapter(inference_fps=fps, confidence_threshold=confidence)


def run(instance: ByteTrackAdapter, sequence, *, fps: float = 5.0, start: float = 0.0):
    results = []
    for index, detections in enumerate(sequence):
        results.append(
            instance.update(
                detections, frame=FRAME, timestamp=start + index / fps
            )
        )
    return results


# -- construction against the real library ----------------------------------


def test_the_frozen_parameter_set_is_accepted_by_the_pinned_library():
    instance = adapter(fps=7.0, confidence=0.4)
    assert instance.constructor_kwargs["track_activation_threshold"] == 0.4
    assert instance.constructor_kwargs["minimum_consecutive_frames"] == 2
    assert instance.constructor_kwargs["high_conf_det_threshold"] == HIGH_CONFIDENCE_FLOOR


def test_the_library_version_is_the_pinned_one():
    import importlib.metadata

    assert importlib.metadata.version("trackers") == "2.6.0"
    assert importlib.metadata.version("supervision") == "0.30.2"


# -- confirmation and stability ---------------------------------------------


def test_a_track_is_confirmed_after_the_configured_consecutive_frames():
    instance = adapter()
    results = run(instance, [[moving_person(step)] for step in range(5)])
    assert results[0] == [], "the first observation must not be a confirmed track"
    confirmed_at = next(
        index for index, result in enumerate(results) if result
    )
    assert confirmed_at == MINIMUM_CONSECUTIVE_FRAMES - 1


def test_the_id_is_stable_across_motion():
    instance = adapter()
    results = run(instance, [[moving_person(step)] for step in range(12)])
    ids = {item.track_id for result in results for item in result}
    assert len(ids) == 1


def test_the_id_survives_one_short_gap():
    instance = adapter()
    sequence = [[moving_person(step)] for step in range(6)]
    sequence.append([])  # one missed observation
    sequence.extend([[moving_person(step)] for step in range(7, 12)])
    results = run(instance, sequence)
    ids = {item.track_id for result in results for item in result}
    assert len(ids) == 1, f"the track should survive one gap, saw ids {ids}"


def test_two_objects_receive_distinct_ids():
    instance = adapter()
    results = run(
        instance, [[moving_person(step), truck(step)] for step in range(8)]
    )
    final = results[-1]
    assert len(final) == 2
    assert len({item.track_id for item in final}) == 2
    assert {item.object_class for item in final} == {"person", "truck"}


def test_ids_are_not_reused_within_one_session():
    instance = adapter()
    run(instance, [[moving_person(step)] for step in range(6)])
    first = set(instance.issued_track_ids)
    # The object leaves for long enough to be removed, then a new one appears.
    run(instance, [[] for _ in range(40)], start=2.0)
    run(instance, [[moving_person(step, y=20.0)] for step in range(6)], start=12.0)
    second = set(instance.issued_track_ids) - first
    assert second, "a new object should have produced a new track"
    assert not (second & first), "a removed track id must not be reused"


# -- data preservation ------------------------------------------------------


def test_class_category_confidence_and_boxes_survive_the_round_trip():
    instance = adapter()
    results = run(instance, [[truck(step)] for step in range(6)])
    tracked = results[-1]
    assert len(tracked) == 1
    item = tracked[0]
    assert item.object_class == "truck"
    assert item.object_category is ObjectCategory.VEHICLE
    assert item.confidence == pytest.approx(0.95, abs=0.02)
    assert item.x2 > item.x1 and item.y2 > item.y1


def test_an_empty_update_is_accepted_by_the_real_library():
    instance = adapter()
    assert instance.update([], frame=FRAME, timestamp=1.0) == []


# -- isolation --------------------------------------------------------------


def test_two_instances_allocate_ids_independently():
    first, second = adapter(), adapter()
    run(first, [[moving_person(step)] for step in range(6)])
    run(second, [[moving_person(step)] for step in range(6)])
    assert first.issued_track_ids == second.issued_track_ids
    assert first._tracker is not second._tracker  # noqa: SLF001


def test_reset_drops_continuity_on_that_instance_only():
    first, second = adapter(), adapter()
    run(first, [[moving_person(step)] for step in range(6)])
    run(second, [[moving_person(step)] for step in range(6)])
    before = set(second.issued_track_ids)
    first.reset()
    assert first.issued_track_ids == frozenset()
    assert set(second.issued_track_ids) == before
    # The reset instance starts a fresh sequence of its own.
    run(first, [[moving_person(step)] for step in range(6)], start=100.0)
    assert first.issued_track_ids


# -- thresholds and time ----------------------------------------------------


def test_the_configured_confidence_is_the_actual_activation_floor():
    instance = adapter(confidence=0.6)
    below = run(instance, [[moving_person(step, confidence=0.35)] for step in range(8)])
    assert all(result == [] for result in below), (
        "detections under the configured threshold must not become tracks"
    )
    strict = adapter(confidence=0.6)
    above = run(strict, [[moving_person(step, confidence=0.85)] for step in range(8)])
    assert any(result for result in above)


@pytest.mark.parametrize("fps", [1.0, 5.0, 10.0])
def test_each_supported_rate_still_confirms_and_ages_tracks(fps: float):
    instance = adapter(fps=fps)
    results = run(instance, [[moving_person(step)] for step in range(6)], fps=fps)
    assert any(result for result in results)
    # A long absence, expressed in real elapsed time, must retire the track.
    run(instance, [[] for _ in range(80)], fps=fps, start=1000.0)
    after = run(
        instance, [[moving_person(step)] for step in range(4)], fps=fps, start=5000.0
    )
    revived = {item.track_id for result in after for item in result}
    original = {item.track_id for result in results for item in result}
    assert revived, "a new object after a long gap must still be trackable"
    assert not (revived & original), (
        "a track that aged out must not be revived with its old id"
    )


def test_active_ids_reflect_the_live_state():
    instance = adapter()
    run(instance, [[moving_person(step)] for step in range(6)])
    assert instance.active_track_ids()
