"""Detector contract: BGR conversion, class mapping, and non-queuing lock."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest

from app.analytics.detector import (
    INFERENCE_IMAGE_SIZE,
    DetectorFailure,
    DetectorUnavailable,
    YoloDetector,
)
from app.domain.models import ObjectCategory

pytestmark = pytest.mark.unit

ALL_CATEGORIES = frozenset({ObjectCategory.PERSON, ObjectCategory.VEHICLE})


class _Boxes:
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    @property
    def xyxy(self):
        return _Tensor(np.array([row[0] for row in self._rows], dtype=np.float32))

    @property
    def conf(self):
        return _Tensor(np.array([row[1] for row in self._rows], dtype=np.float32))

    @property
    def cls(self):
        return _Tensor(np.array([row[2] for row in self._rows], dtype=np.float32))


class _Tensor:
    def __init__(self, array):
        self._array = array

    def cpu(self):
        return self

    def numpy(self):
        return self._array


class _Result:
    def __init__(self, rows):
        self.boxes = _Boxes(rows)


class _FakeModel:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls: list[dict] = []
        self.frames: list[np.ndarray] = []

    def predict(self, image, **kwargs):
        self.calls.append(kwargs)
        self.frames.append(np.array(image, copy=True))
        return [_Result(self.rows)]


def _detector(rows=(), *, path: Path | None = None) -> YoloDetector:
    detector = YoloDetector(
        model_path=path or Path("/opt/models/yolo11n.pt"),
        expected_sha256="0" * 64,
    )
    detector._model = _FakeModel(rows)  # noqa: SLF001 - test seam
    detector._ready = True  # noqa: SLF001
    return detector


def _rgb(width=32, height=24):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[..., 0] = 10  # R
    frame[..., 1] = 20  # G
    frame[..., 2] = 30  # B
    return frame


# -- inference arguments ----------------------------------------------------


def test_the_model_receives_bgr_not_rgb():
    detector = _detector()
    detector.try_detect(_rgb(), 0.25, ALL_CATEGORIES)
    sent = detector._model.frames[0]  # noqa: SLF001
    assert sent[0, 0].tolist() == [30, 20, 10]
    assert sent.flags["C_CONTIGUOUS"]


def test_predict_is_called_with_the_frozen_arguments():
    detector = _detector()
    detector.try_detect(_rgb(), 0.4, ALL_CATEGORIES)
    kwargs = detector._model.calls[0]  # noqa: SLF001
    assert kwargs == {
        "device": "cpu",
        "imgsz": INFERENCE_IMAGE_SIZE,
        "conf": 0.4,
        "verbose": False,
    }


# -- class mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    "coco_id,object_class,category",
    [
        (0, "person", ObjectCategory.PERSON),
        (1, "bicycle", ObjectCategory.VEHICLE),
        (2, "car", ObjectCategory.VEHICLE),
        (3, "motorcycle", ObjectCategory.VEHICLE),
        (5, "bus", ObjectCategory.VEHICLE),
        (7, "truck", ObjectCategory.VEHICLE),
    ],
)
def test_the_six_supported_coco_classes(coco_id, object_class, category):
    detector = _detector([((1.0, 1.0, 10.0, 10.0), 0.9, coco_id)])
    batch = detector.try_detect(_rgb(), 0.25, ALL_CATEGORIES)
    assert len(batch) == 1
    assert batch.detections[0].object_class == object_class
    assert batch.detections[0].object_category is category
    assert batch.detections[0].coco_class_id == coco_id


@pytest.mark.parametrize("coco_id", [4, 6, 8, 9, 15, 16, 79])
def test_unsupported_coco_classes_are_ignored(coco_id):
    """train (6) and boat (8) are deliberately not remapped to vehicle."""
    detector = _detector([((1.0, 1.0, 10.0, 10.0), 0.9, coco_id)])
    assert len(detector.try_detect(_rgb(), 0.25, ALL_CATEGORIES)) == 0


def test_the_category_filter_is_applied():
    detector = _detector(
        [((1.0, 1.0, 10.0, 10.0), 0.9, 0), ((2.0, 2.0, 12.0, 12.0), 0.9, 2)]
    )
    batch = detector.try_detect(_rgb(), 0.25, frozenset({ObjectCategory.PERSON}))
    assert [item.object_class for item in batch.detections] == ["person"]


def test_the_confidence_filter_is_applied():
    detector = _detector(
        [((1.0, 1.0, 10.0, 10.0), 0.9, 0), ((2.0, 2.0, 12.0, 12.0), 0.2, 0)]
    )
    batch = detector.try_detect(_rgb(), 0.5, ALL_CATEGORIES)
    assert [round(item.confidence, 2) for item in batch.detections] == [0.9]


# -- box hygiene ------------------------------------------------------------


def test_boxes_are_clamped_to_frame_bounds():
    detector = _detector([((-20.0, -20.0, 500.0, 500.0), 0.9, 0)])
    detection = detector.try_detect(_rgb(32, 24), 0.25, ALL_CATEGORIES).detections[0]
    assert (detection.x1, detection.y1) == (0.0, 0.0)
    assert (detection.x2, detection.y2) == (32.0, 24.0)


@pytest.mark.parametrize(
    "box",
    [
        (5.0, 5.0, 5.0, 10.0),
        (5.0, 5.0, 10.0, 5.0),
        (10.0, 10.0, 5.0, 5.0),
        (float("nan"), 1.0, 10.0, 10.0),
        (1.0, 1.0, float("inf"), 10.0),
    ],
)
def test_degenerate_and_non_finite_boxes_are_discarded(box):
    detector = _detector([(box, 0.9, 0)])
    assert len(detector.try_detect(_rgb(), 0.25, ALL_CATEGORIES)) == 0


def test_an_empty_result_is_an_empty_batch_not_an_error():
    batch = _detector().try_detect(_rgb(), 0.25, ALL_CATEGORIES)
    assert batch is not None and len(batch) == 0
    assert batch.inference_seconds >= 0.0


# -- the non-queuing admission lock -----------------------------------------


def test_a_contending_caller_receives_none_rather_than_waiting():
    detector = _detector()
    entered = threading.Event()
    release = threading.Event()
    outcome: list[object] = []

    def slow_predict(_image, **_kwargs):
        entered.set()
        release.wait(5)
        return [_Result([])]

    detector._model.predict = slow_predict  # noqa: SLF001
    holder = threading.Thread(
        target=lambda: detector.try_detect(_rgb(), 0.25, ALL_CATEGORIES)
    )
    holder.start()
    try:
        assert entered.wait(5)
        outcome.append(detector.try_detect(_rgb(), 0.25, ALL_CATEGORIES))
    finally:
        release.set()
        holder.join(5)
    assert outcome == [None]


def test_the_lock_is_released_when_inference_raises():
    detector = _detector()

    def boom(*_a, **_k):
        raise RuntimeError("inference exploded")

    detector._model.predict = boom  # noqa: SLF001
    with pytest.raises(DetectorFailure):
        detector.try_detect(_rgb(), 0.25, ALL_CATEGORIES)
    detector._model.predict = lambda *_a, **_k: [_Result([])]  # noqa: SLF001
    assert detector.try_detect(_rgb(), 0.25, ALL_CATEGORIES) is not None


# -- asset verification -----------------------------------------------------


def test_a_missing_model_file_is_typed(tmp_path: Path):
    detector = YoloDetector(
        model_path=tmp_path / "absent.pt", expected_sha256="0" * 64
    )
    with pytest.raises(DetectorUnavailable):
        detector.verify_asset()
    assert detector.ready is False


def test_a_checksum_mismatch_refuses_to_load(tmp_path: Path):
    model = tmp_path / "model.pt"
    model.write_bytes(b"not the pinned asset")
    detector = YoloDetector(model_path=model, expected_sha256="0" * 64)
    with pytest.raises(DetectorUnavailable):
        detector.load()
    assert detector.ready is False


def test_a_correct_checksum_passes_verification(tmp_path: Path):
    import hashlib

    model = tmp_path / "model.pt"
    payload = b"pretend weights"
    model.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    detector = YoloDetector(model_path=model, expected_sha256=digest)
    assert detector.verify_asset() == digest


def test_detecting_before_load_is_typed():
    detector = YoloDetector(model_path=Path("/x"), expected_sha256="0" * 64)
    with pytest.raises(DetectorUnavailable):
        detector.try_detect(_rgb(), 0.25, ALL_CATEGORIES)


def test_release_clears_readiness():
    detector = _detector()
    detector.release()
    assert detector.ready is False
