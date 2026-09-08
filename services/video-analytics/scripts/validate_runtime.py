#!/usr/bin/env python3
"""Executable runtime validator for Component 4.

Used three ways:

*   during the Docker build, to fail the image if a required GStreamer factory,
    Python import, or model asset is missing;
*   from CI or a developer shell (``--all``);
*   as the container healthcheck (``--health-url``).

Every check prints one ``ok``/``FAIL`` line and the process exit code is zero
only when every selected check passed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import sys
import urllib.error
import urllib.request
from pathlib import Path

REQUIRED_GST_FACTORIES = (
    "rtspsrc",
    "rtph264depay",
    "h264parse",
    "avdec_h264",
    "videoconvert",
    "appsink",
)

REQUIRED_APP_MODULES = (
    "app",
    "app.config",
    "app.logging_config",
)

_FAILURES: list[str] = []


def _ok(check: str, detail: str = "") -> None:
    print(f"ok    {check}{(' ' + detail) if detail else ''}")


def _fail(check: str, detail: str) -> None:
    _FAILURES.append(check)
    print(f"FAIL  {check} {detail}", file=sys.stderr)


def check_platform() -> None:
    _ok(
        "platform",
        f"machine={platform.machine()} system={platform.system()} "
        f"python={platform.python_version()}",
    )


def check_imports() -> None:
    import importlib

    # Importing the detector module seeds the Ultralytics config directory
    # before anything else can import the library and fix it in place.
    try:
        importlib.import_module("app.analytics.detector")
    except Exception:  # noqa: BLE001 - reported by the loop below
        pass

    for name in REQUIRED_APP_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - validator reports, never raises
            _fail("imports", f"module={name} error={type(exc).__name__}: {exc}")
            return
    for name in (
        "fastapi",
        "uvicorn",
        "pydantic",
        "pydantic_settings",
        "httpx",
        "numpy",
        "PIL",
        "supervision",
        "trackers",
        "ultralytics",
    ):
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            _fail("imports", f"module={name} error={type(exc).__name__}: {exc}")
            return
        version = getattr(module, "__version__", "?")
        print(f"      import {name}=={version}")
    _ok("imports")


def check_gstreamer() -> None:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import Gst, GstApp, GstVideo  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        _fail("gstreamer", f"pygobject import failed: {type(exc).__name__}: {exc}")
        return
    if not Gst.is_initialized():
        Gst.init(None)
    version = ".".join(str(part) for part in Gst.version()[:3])
    missing = [
        name for name in REQUIRED_GST_FACTORIES if Gst.ElementFactory.find(name) is None
    ]
    if missing:
        _fail("gstreamer", f"missing factories: {', '.join(missing)}")
        return
    # GstRtsp is optional: the RTSP lower-transport enum is set numerically when
    # its typelib is not shipped by the distribution (see app/analytics/gst_pipeline.py).
    try:
        gi.require_version("GstRtsp", "1.0")
        from gi.repository import GstRtsp  # noqa: F401

        rtsp_typelib = "present"
    except Exception:  # noqa: BLE001
        rtsp_typelib = "absent(numeric-fallback)"
    _ok(
        "gstreamer",
        f"core={version} factories={len(REQUIRED_GST_FACTORIES)} "
        f"gstrtsp_typelib={rtsp_typelib}",
    )


def check_torch() -> None:
    try:
        import torch
        import torchvision
    except Exception as exc:  # noqa: BLE001
        _fail("torch", f"import failed: {type(exc).__name__}: {exc}")
        return
    if torch.cuda.is_available():
        _fail("torch", "torch.cuda.is_available() is True; this build must be CPU-only")
        return
    _ok(
        "torch",
        f"torch={torch.__version__} torchvision={torchvision.__version__} cuda=False",
    )


def _model_path() -> Path:
    return Path(os.environ.get("ANALYTICS_MODEL_PATH", "/opt/models/yolo11n.pt"))


def _expected_digest() -> str:
    from app.config import DEFAULT_MODEL_SHA256

    return os.environ.get("ANALYTICS_MODEL_SHA256", DEFAULT_MODEL_SHA256)


def check_model_metadata() -> None:
    path = _model_path()
    if not path.is_file():
        _fail("model-metadata", f"model file not found at {path}")
        return
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    expected = _expected_digest()
    if actual != expected:
        _fail("model-metadata", f"sha256 mismatch expected={expected} actual={actual}")
        return
    _ok("model-metadata", f"path={path} bytes={path.stat().st_size} sha256={actual}")


def check_inference() -> None:
    try:
        import numpy as np

        from app.analytics.detector import YoloDetector
        from app.domain.models import ObjectCategory
    except Exception as exc:  # noqa: BLE001
        _fail("inference", f"detector import failed: {type(exc).__name__}: {exc}")
        return
    try:
        detector = YoloDetector(
            model_path=_model_path(),
            expected_sha256=_expected_digest(),
            torch_threads=int(os.environ.get("ANALYTICS_TORCH_THREADS", "2")),
        )
        detector.load()
        blank = np.zeros((320, 320, 3), dtype=np.uint8)
        batch = detector.try_detect(
            blank, 0.25, frozenset({ObjectCategory.PERSON, ObjectCategory.VEHICLE})
        )
    except Exception as exc:  # noqa: BLE001
        _fail("inference", f"{type(exc).__name__}: {exc}")
        return
    if batch is None:
        _fail("inference", "detector admission was refused during a single-threaded check")
        return
    _ok("inference", f"blank_frame_detections={len(batch.detections)}")


def check_tracker() -> None:
    try:
        import numpy as np

        from app.analytics.tracker import ByteTrackAdapter
        from app.analytics.types import Detection
        from app.domain.models import ObjectCategory
    except Exception as exc:  # noqa: BLE001
        _fail("tracker", f"import failed: {type(exc).__name__}: {exc}")
        return
    try:
        adapter = ByteTrackAdapter(
            inference_fps=5.0, confidence_threshold=0.25, lost_track_buffer=30
        )
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        for index in range(4):
            adapter.update(
                [
                    Detection(
                        x1=float(index),
                        y1=0.0,
                        x2=float(index) + 10.0,
                        y2=10.0,
                        confidence=0.9,
                        coco_class_id=0,
                        object_class="person",
                        object_category=ObjectCategory.PERSON,
                    )
                ],
                frame=frame,
                timestamp=float(index) * 0.2,
            )
        adapter.reset()
    except Exception as exc:  # noqa: BLE001
        _fail("tracker", f"{type(exc).__name__}: {exc}")
        return
    _ok("tracker", "bytetrack adapter constructed, updated and reset")


def check_health(url: str) -> None:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310
            status = response.status
            response.read(4096)
    except urllib.error.HTTPError as exc:
        _fail("health", f"url returned HTTP {exc.code}")
        return
    except Exception as exc:  # noqa: BLE001
        _fail("health", f"{type(exc).__name__}")
        return
    if status != 200:
        _fail("health", f"url returned HTTP {status}")
        return
    _ok("health", f"HTTP {status}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Component 4 runtime validator")
    parser.add_argument("--all", action="store_true", help="run every offline check")
    parser.add_argument("--platform", action="store_true")
    parser.add_argument("--imports", action="store_true")
    parser.add_argument("--gstreamer", action="store_true")
    parser.add_argument("--torch", action="store_true")
    parser.add_argument("--model-metadata", action="store_true")
    parser.add_argument("--inference", action="store_true")
    parser.add_argument("--tracker", action="store_true")
    parser.add_argument("--health-url", default=None)
    args = parser.parse_args(argv)

    if args.health_url:
        check_health(args.health_url)
        return 1 if _FAILURES else 0

    selected = any(
        (
            args.platform,
            args.imports,
            args.gstreamer,
            args.torch,
            args.model_metadata,
            args.inference,
            args.tracker,
        )
    )
    if args.all or not selected:
        args.platform = args.imports = args.gstreamer = True
        args.torch = args.model_metadata = True
        args.inference = args.tracker = args.all

    if args.platform:
        check_platform()
    if args.imports:
        check_imports()
    if args.gstreamer:
        check_gstreamer()
    if args.torch:
        check_torch()
    if args.model_metadata:
        check_model_metadata()
    if args.tracker:
        check_tracker()
    if args.inference:
        check_inference()

    if _FAILURES:
        print(f"\nFAILED checks: {', '.join(sorted(set(_FAILURES)))}", file=sys.stderr)
        return 1
    print("\nAll selected runtime checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
