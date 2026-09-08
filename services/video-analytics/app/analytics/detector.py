"""Process-wide, CPU-only YOLO11n detector with a non-queuing admission lock.

One model is loaded for the whole process. Workers compete for it with a
``blocking=False`` lock: a worker that loses admission drops that analytics
opportunity rather than queueing behind another camera's inference.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from app.analytics.types import Detection, DetectionBatch
from app.domain.models import SUPPORTED_COCO_CLASSES, ObjectCategory

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "Detector",
    "YoloDetector",
    "DetectorUnavailable",
    "DetectorFailure",
    "INFERENCE_IMAGE_SIZE",
]

logger = logging.getLogger("analytics.detector")

INFERENCE_IMAGE_SIZE: Final[int] = 640


def _seed_ultralytics_config() -> None:
    """Give Ultralytics a writable config dir seeded from the baked copy.

    The shipped image keeps every application directory read-only, so the
    library's settings and cache live in /tmp. Failure is harmless: Ultralytics
    simply recreates its own defaults.
    """
    try:
        from scripts.seed_ultralytics import seed_config_dir
    except Exception:  # noqa: BLE001 - scripts/ is not importable in every context
        import importlib.util
        import sys
        from pathlib import Path as _Path

        candidate = _Path(__file__).resolve().parents[2] / "scripts" / "seed_ultralytics.py"
        if not candidate.is_file():
            return
        spec = importlib.util.spec_from_file_location("_seed_ultralytics", candidate)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules["_seed_ultralytics"] = module
        spec.loader.exec_module(module)
        seed_config_dir = module.seed_config_dir
    try:
        seed_config_dir()
    except Exception:  # noqa: BLE001
        logger.debug("ultralytics_config_seed_skipped")




_seed_ultralytics_config()


class DetectorUnavailable(RuntimeError):
    """The model could not be loaded, verified, or warmed up."""


class DetectorFailure(RuntimeError):
    """An admitted inference call raised unexpectedly."""


@runtime_checkable
class Detector(Protocol):
    """The contract a stream worker depends on.

    ``None`` means admission was lost because another camera was inferring; it
    is not an error and must not change worker state.
    """

    def try_detect(
        self,
        rgb: "NDArray[np.uint8]",
        confidence: float,
        enabled_categories: frozenset[ObjectCategory],
    ) -> DetectionBatch | None: ...

    @property
    def ready(self) -> bool: ...


class YoloDetector:
    """Ultralytics YOLO11n on CPU, driven with externally supplied frames."""

    def __init__(
        self,
        *,
        model_path: Path,
        expected_sha256: str,
        torch_threads: int = 2,
        image_size: int = INFERENCE_IMAGE_SIZE,
    ):
        self._model_path = Path(model_path)
        self._expected_sha256 = expected_sha256
        self._torch_threads = int(torch_threads)
        self._image_size = int(image_size)
        self._model = None
        self._ready = False
        self._lock = threading.Lock()
        self._load_lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def model_path(self) -> Path:
        return self._model_path

    # -- load ---------------------------------------------------------------

    def verify_asset(self) -> str:
        if not self._model_path.is_file():
            raise DetectorUnavailable(f"model file not found at {self._model_path}")
        digest = hashlib.sha256()
        with self._model_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != self._expected_sha256:
            raise DetectorUnavailable(
                "model checksum mismatch; refusing to load an unverified model"
            )
        return actual

    def load(self) -> None:
        """Verify, load, and warm the model exactly once."""
        with self._load_lock:
            if self._ready:
                return
            self.verify_asset()
            try:
                import torch

                torch.set_num_threads(self._torch_threads)
                try:
                    torch.set_num_interop_threads(1)
                except RuntimeError:
                    # Already initialised in this process; not fatal.
                    logger.debug("torch_interop_threads_already_set")
                if torch.cuda.is_available():  # pragma: no cover - CPU-only image
                    raise DetectorUnavailable("this build must run on CPU only")

                from ultralytics import YOLO

                model = YOLO(str(self._model_path))
                import numpy as np

                blank = np.zeros((320, 320, 3), dtype=np.uint8)
                model.predict(
                    blank,
                    device="cpu",
                    imgsz=self._image_size,
                    conf=0.25,
                    verbose=False,
                )
            except DetectorUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001
                raise DetectorUnavailable(
                    f"model could not be loaded: {type(exc).__name__}: {exc}"
                ) from exc
            self._model = model
            self._ready = True
            logger.info(
                "detector_ready",
                extra={"model": self._model_path.name, "imgsz": self._image_size},
            )

    def release(self) -> None:
        with self._load_lock:
            self._model = None
            self._ready = False

    # -- inference ----------------------------------------------------------

    def try_detect(
        self,
        rgb: "NDArray[np.uint8]",
        confidence: float,
        enabled_categories: frozenset[ObjectCategory],
    ) -> DetectionBatch | None:
        if not self._ready or self._model is None:
            raise DetectorUnavailable("detector is not loaded")
        if not self._lock.acquire(blocking=False):
            return None
        try:
            started = time.monotonic()
            import numpy as np

            # Ultralytics' ndarray input convention is BGR.
            bgr = np.ascontiguousarray(rgb[:, :, ::-1])
            results = self._model.predict(
                bgr,
                device="cpu",
                imgsz=self._image_size,
                conf=float(confidence),
                verbose=False,
            )
            elapsed = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001
            raise DetectorFailure(f"inference failed: {type(exc).__name__}") from exc
        finally:
            self._lock.release()

        detections = self._convert(
            results, rgb.shape[1], rgb.shape[0], confidence, enabled_categories
        )
        return DetectionBatch(detections=detections, inference_seconds=elapsed)

    def _convert(
        self,
        results,
        width: int,
        height: int,
        confidence: float,
        enabled_categories: frozenset[ObjectCategory],
    ) -> tuple[Detection, ...]:
        import math

        accepted: list[Detection] = []
        for result in results or ():
            boxes = getattr(result, "boxes", None)
            if boxes is None or len(boxes) == 0:
                continue
            xyxy = boxes.xyxy.cpu().numpy()
            confs = boxes.conf.cpu().numpy()
            classes = boxes.cls.cpu().numpy()
            for index in range(len(classes)):
                coco_id = int(classes[index])
                mapping = SUPPORTED_COCO_CLASSES.get(coco_id)
                if mapping is None:
                    continue
                object_class, category = mapping
                if category not in enabled_categories:
                    continue
                score = float(confs[index])
                if not math.isfinite(score) or score < confidence:
                    continue
                x1, y1, x2, y2 = (float(value) for value in xyxy[index])
                if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
                    continue
                x1 = min(max(x1, 0.0), float(width))
                y1 = min(max(y1, 0.0), float(height))
                x2 = min(max(x2, 0.0), float(width))
                y2 = min(max(y2, 0.0), float(height))
                if x2 <= x1 or y2 <= y1:
                    continue
                accepted.append(
                    Detection(
                        x1=x1,
                        y1=y1,
                        x2=x2,
                        y2=y2,
                        confidence=min(max(score, 0.0), 1.0),
                        coco_class_id=coco_id,
                        object_class=object_class,
                        object_category=category,
                    )
                )
        return tuple(accepted)
