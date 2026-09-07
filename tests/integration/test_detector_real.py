"""The real YOLO11n CPU detector inside the built image."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from app.analytics.detector import INFERENCE_IMAGE_SIZE, YoloDetector
from app.config import DEFAULT_MODEL_SHA256
from app.domain.models import ObjectCategory

pytestmark = [pytest.mark.integration, pytest.mark.real_model]

ALL = frozenset({ObjectCategory.PERSON, ObjectCategory.VEHICLE})
MODEL_PATH = Path(os.environ.get("ANALYTICS_MODEL_PATH", "/opt/models/yolo11n.pt"))


@pytest.fixture(scope="module")
def detector() -> YoloDetector:
    instance = YoloDetector(
        model_path=MODEL_PATH, expected_sha256=DEFAULT_MODEL_SHA256, torch_threads=2
    )
    instance.load()
    return instance


def load_rgb(path: Path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as image:
        return np.ascontiguousarray(np.array(image.convert("RGB"), dtype=np.uint8))


# -- runtime facts ----------------------------------------------------------


def test_this_build_is_cpu_only_at_the_pinned_versions():
    import torch
    import torchvision
    import ultralytics

    assert torch.cuda.is_available() is False
    assert torch.__version__.startswith("2.7.1")
    assert torchvision.__version__.startswith("0.22.1")
    assert ultralytics.__version__ == "8.4.49"


def test_the_model_asset_matches_the_pinned_checksum(detector: YoloDetector):
    assert detector.verify_asset() == DEFAULT_MODEL_SHA256
    assert detector.ready is True


def test_torch_thread_limits_are_applied():
    import torch

    assert torch.get_num_threads() <= 8


# -- real inference ---------------------------------------------------------


def test_the_official_sample_image_yields_a_person_and_a_bus(
    detector: YoloDetector, bus_image_path: Path
):
    rgb = load_rgb(bus_image_path)
    batch = detector.try_detect(rgb, 0.25, ALL)
    assert batch is not None

    classes = [item.object_class for item in batch.detections]
    assert "person" in classes, f"expected a person, got {classes}"
    assert "bus" in classes, f"expected a bus, got {classes}"

    # The original COCO class is preserved and the category is normalised.
    bus = next(item for item in batch.detections if item.object_class == "bus")
    assert bus.object_category is ObjectCategory.VEHICLE
    assert bus.coco_class_id == 5
    assert bus.confidence >= 0.5

    person = next(item for item in batch.detections if item.object_class == "person")
    assert person.object_category is ObjectCategory.PERSON
    assert person.coco_class_id == 0
    assert person.confidence >= 0.5


def test_detections_stay_inside_the_frame(detector: YoloDetector, bus_image_path: Path):
    rgb = load_rgb(bus_image_path)
    height, width = rgb.shape[:2]
    batch = detector.try_detect(rgb, 0.25, ALL)
    for item in batch.detections:
        assert 0.0 <= item.x1 < item.x2 <= float(width)
        assert 0.0 <= item.y1 < item.y2 <= float(height)
        assert 0.0 <= item.confidence <= 1.0


def test_the_category_filter_applies_to_real_output(
    detector: YoloDetector, bus_image_path: Path
):
    rgb = load_rgb(bus_image_path)
    people = detector.try_detect(rgb, 0.25, frozenset({ObjectCategory.PERSON}))
    assert people is not None and len(people) > 0
    assert {item.object_category for item in people.detections} == {
        ObjectCategory.PERSON
    }


def test_a_blank_frame_produces_no_detections(detector: YoloDetector):
    blank = np.zeros((320, 320, 3), dtype=np.uint8)
    batch = detector.try_detect(blank, 0.25, ALL)
    assert batch is not None
    assert len(batch) == 0


def test_inference_reports_a_plausible_duration(
    detector: YoloDetector, bus_image_path: Path
):
    rgb = load_rgb(bus_image_path)
    detector.try_detect(rgb, 0.25, ALL)  # warm
    batch = detector.try_detect(rgb, 0.25, ALL)
    assert 0.0 < batch.inference_seconds < 30.0
    print(
        f"\nmeasured warm inference at imgsz={INFERENCE_IMAGE_SIZE}: "
        f"{batch.inference_seconds * 1000:.1f} ms"
    )


# -- no runtime downloads or writes ----------------------------------------


def test_repeated_inference_writes_nothing_outside_tmp(
    detector: YoloDetector, bus_image_path: Path
):
    watched = [Path("/opt/models"), Path("/opt/venv/lib"), Path("/srv/analytics/app")]

    def snapshot():
        state = {}
        for root in watched:
            for path in root.rglob("*"):
                try:
                    state[str(path)] = path.stat().st_mtime_ns
                except OSError:
                    continue
        return state

    before = snapshot()
    rgb = load_rgb(bus_image_path)
    for _ in range(3):
        detector.try_detect(rgb, 0.25, ALL)
    after = snapshot()

    added = set(after) - set(before)
    changed = {key for key in set(before) & set(after) if before[key] != after[key]}
    assert not added, f"inference created files: {sorted(added)[:5]}"
    assert not changed, f"inference modified files: {sorted(changed)[:5]}"


def test_no_extra_model_weight_was_downloaded():
    weights = sorted(Path("/opt/models").glob("*.pt"))
    assert [path.name for path in weights] == ["yolo11n.pt"]
