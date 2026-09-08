#!/usr/bin/env python3
"""Prove the built image can actually run Component 5, before it serves traffic.

Each flag is an independent gate. The Docker build runs the static ones so a
missing wheel or a corrupt model asset fails the image rather than the first
request; the Compose healthcheck uses ``--health-url``.

    python scripts/validate_runtime.py --platform --imports --model-manifest \
        --offline-inference
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FAILED = False


def _ok(line: str) -> None:
    print(f"ok    {line}")


def _fail(line: str) -> None:
    global _FAILED
    _FAILED = True
    print(f"FAIL  {line}", file=sys.stderr)


def check_platform() -> None:
    _ok(
        f"platform machine={platform.machine()} system={platform.system()} "
        f"python={platform.python_version()}"
    )
    if sys.version_info < (3, 12):
        _fail(f"python {platform.python_version()} is older than 3.12")


def check_imports() -> None:
    modules = [
        "fastapi", "uvicorn", "pydantic", "pydantic_settings", "httpx",
        "numpy", "PIL", "transformers", "tokenizers", "huggingface_hub",
        "safetensors", "sentencepiece", "multipart",
    ]
    versions = []
    for name in modules:
        try:
            module = __import__(name)
        except Exception as exc:  # noqa: BLE001
            _fail(f"import {name}: {type(exc).__name__}: {exc}")
            continue
        versions.append(f"{name}=={getattr(module, '__version__', 'n/a')}")
    for chunk in range(0, len(versions), 5):
        _ok("import " + "   ".join(versions[chunk : chunk + 5]))


def check_torch() -> None:
    try:
        import torch
        import torchvision
    except Exception as exc:  # noqa: BLE001
        _fail(f"torch import: {type(exc).__name__}: {exc}")
        return
    _ok(
        f"torch torch={torch.__version__} torchvision={torchvision.__version__} "
        f"cuda={torch.cuda.is_available()}"
    )
    if torch.cuda.is_available():
        _fail("CUDA is visible; Component 5 P0 is CPU-only")


def check_manifest(model_dir: Path) -> None:
    from app.embeddings.manifest import MANIFEST, ManifestError

    started = time.monotonic()
    try:
        MANIFEST.verify(model_dir)
    except ManifestError as exc:
        _fail(f"model-manifest {exc}")
        return
    _ok(
        f"model-manifest dir={model_dir} files={len(MANIFEST.assets)} "
        f"model_id={MANIFEST.model_id} revision={MANIFEST.revision} "
        f"sha256={MANIFEST.weights_sha256} "
        f"elapsed_ms={round((time.monotonic() - started) * 1000)}"
    )


def check_offline_inference(model_dir: Path) -> None:
    """Load the checkpoint with the hub in offline mode and embed real inputs."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        import numpy as np
        from PIL import Image

        from app.embeddings.manifest import EMBEDDING_DIM
        from app.embeddings.runtime import EmbeddingRuntime
    except Exception as exc:  # noqa: BLE001
        _fail(f"offline-inference import: {type(exc).__name__}: {exc}")
        return

    started = time.monotonic()
    try:
        runtime = EmbeddingRuntime(model_dir=model_dir, torch_threads=2)
        runtime.load()
        load_ms = round((time.monotonic() - started) * 1000)

        image = Image.new("RGB", (256, 192), (40, 90, 160))
        t0 = time.monotonic()
        image_vectors = runtime.embed_images([image])
        image_ms = round((time.monotonic() - t0) * 1000)

        t0 = time.monotonic()
        text_vectors = runtime.embed_texts(["a red car"])
        text_ms = round((time.monotonic() - t0) * 1000)
    except Exception as exc:  # noqa: BLE001
        _fail(f"offline-inference {type(exc).__name__}: {exc}")
        return

    for label, vectors in (("image", image_vectors), ("text", text_vectors)):
        if vectors.shape != (1, EMBEDDING_DIM):
            _fail(f"offline-inference {label} shape {vectors.shape}")
            return
        if vectors.dtype != np.float32:
            _fail(f"offline-inference {label} dtype {vectors.dtype}")
            return
        if not np.isfinite(vectors).all():
            _fail(f"offline-inference {label} produced non-finite values")
            return
        norm = float(np.linalg.norm(vectors[0]))
        if abs(norm - 1.0) > 1e-3:
            _fail(f"offline-inference {label} norm {norm}")
            return
    _ok(
        f"offline-inference dim={EMBEDDING_DIM} load_ms={load_ms} "
        f"image_ms={image_ms} text_ms={text_ms} offline=1"
    )


def check_health(url: str) -> None:
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            body = json.loads(response.read().decode("utf-8"))
            status = response.status
    except Exception as exc:  # noqa: BLE001
        _fail(f"health {type(exc).__name__}")
        return
    if status != 200:
        _fail(f"health status={status}")
        return
    _ok(f"health status={body.get('status')} search={body.get('search')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", action="store_true")
    parser.add_argument("--imports", action="store_true")
    parser.add_argument("--torch", action="store_true")
    parser.add_argument("--model-manifest", action="store_true")
    parser.add_argument("--offline-inference", action="store_true")
    parser.add_argument("--health-url")
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path(os.environ.get("SEMANTIC_MODEL_DIR", "/opt/models/siglip")),
    )
    args = parser.parse_args(argv)

    if not any(
        [
            args.platform, args.imports, args.torch, args.model_manifest,
            args.offline_inference, args.health_url,
        ]
    ):
        parser.error("select at least one check")

    if args.platform:
        check_platform()
    if args.imports:
        check_imports()
    if args.torch:
        check_torch()
    if args.model_manifest:
        check_manifest(args.model_dir)
    if args.offline_inference:
        check_offline_inference(args.model_dir)
    if args.health_url:
        check_health(args.health_url)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
