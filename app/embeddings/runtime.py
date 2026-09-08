"""The one SigLIP instance in the process, wrapped in a frozen contract.

Responsibilities are deliberately narrow: load the verified local checkpoint
offline on CPU, apply exactly the frozen preprocessing, and return explicitly
L2-normalized, finite, C-contiguous float32 vectors of the manifest dimension.

It knows nothing about events, SQLite, HTTP, batching policy, or fairness -
``services/inference.InferenceCoordinator`` owns those. Every method here is
synchronous and blocking; callers run it via ``asyncio.to_thread`` so the event
loop is never blocked by inference (PLAN section 10.2).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Final, Sequence

import numpy as np

from app.embeddings.manifest import (
    EMBEDDING_DIM,
    MANIFEST,
    NORM_TOLERANCE,
    TEXT_MAX_TOKENS,
)

__all__ = ["EmbeddingRuntime", "EmbeddingError", "ModelUnavailable"]

logger = logging.getLogger("semantic.embeddings")

#: Explicit channel layout for every image handed to the processor.
_CHANNELS_LAST: Final[str] = "channels_last"

_OFFLINE_ENV: Final[dict[str, str]] = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
}


class EmbeddingError(RuntimeError):
    """Inference produced output that violates the frozen vector contract."""


class ModelUnavailable(RuntimeError):
    """The model could not be loaded, or has been marked unavailable."""


class EmbeddingRuntime:
    """One CPU SigLIP model: text and image towers, one vector space."""

    def __init__(
        self,
        *,
        model_dir: Path,
        torch_threads: int = 2,
        verify_manifest: bool = True,
    ) -> None:
        self._model_dir = Path(model_dir)
        self._torch_threads = int(torch_threads)
        self._verify_manifest = verify_manifest
        self._model: Any = None
        self._processor: Any = None
        self._torch: Any = None
        self._load_lock = threading.Lock()
        self._unavailable_reason: str | None = None

    # ---- identity --------------------------------------------------------

    @property
    def model_id(self) -> str:
        return MANIFEST.model_id

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM

    @property
    def ready(self) -> bool:
        return self._model is not None and self._unavailable_reason is None

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def mark_unavailable(self, reason: str) -> None:
        """Pause use of the model without discarding any stored vector."""
        self._unavailable_reason = reason

    def clear_unavailable(self) -> None:
        self._unavailable_reason = None

    # ---- lifecycle -------------------------------------------------------

    def load(self) -> None:
        """Verify the manifest and load the checkpoint. Blocking; idempotent."""
        with self._load_lock:
            if self._model is not None:
                return
            # Offline is set before transformers is imported so no code path can
            # reach the network, even on a cache miss.
            os.environ.update(_OFFLINE_ENV)
            if self._verify_manifest:
                MANIFEST.verify(self._model_dir)

            import torch
            from transformers import AutoProcessor, SiglipModel

            torch.set_num_threads(max(1, self._torch_threads))
            torch.set_grad_enabled(False)

            processor = AutoProcessor.from_pretrained(
                str(self._model_dir), use_fast=False, local_files_only=True
            )
            model = SiglipModel.from_pretrained(
                str(self._model_dir),
                local_files_only=True,
                dtype=torch.float32,
            )
            model.eval()

            self._torch = torch
            self._processor = processor
            self._model = model
            self._unavailable_reason = None
            logger.info(
                "model_loaded",
                extra={
                    "model_id": MANIFEST.model_id,
                    "revision": MANIFEST.revision,
                    "dimension": EMBEDDING_DIM,
                    "torch_threads": self._torch_threads,
                },
            )

    def release(self) -> None:
        self._model = None
        self._processor = None

    # ---- inference -------------------------------------------------------

    def _require(self) -> None:
        if self._model is None:
            raise ModelUnavailable("model is not loaded")
        if self._unavailable_reason is not None:
            raise ModelUnavailable(self._unavailable_reason)

    def embed_images(self, images: Sequence[Any]) -> np.ndarray:
        """Embed already-decoded RGB PIL images. Returns ``(n, 768)`` float32."""
        self._require()
        if not images:
            return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
        torch = self._torch
        # The channel dimension is stated explicitly. Transformers infers it
        # from the array shape otherwise, and that inference is ambiguous for
        # small images: a 1x1 crop raises ValueError, and a 3-pixel-tall crop is
        # silently read as channels-first and embedded from transposed data.
        # Component 4 crops of distant objects really are that small. For every
        # unambiguous image the hint is a no-op - verified bit-identical.
        inputs = self._processor(
            images=list(images),
            return_tensors="pt",
            input_data_format=_CHANNELS_LAST,
        )
        with torch.inference_mode():
            features = self._model.get_image_features(**inputs)
        return self._finalize(features, len(images))

    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        """Embed already-normalized query strings. Returns ``(n, 768)`` float32.

        Tokenization is exactly the frozen processor with fixed 64-token
        padding: no prefix, template, rewrite, translation, or expansion.
        """
        self._require()
        if not texts:
            return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
        torch = self._torch
        inputs = self._processor(
            text=list(texts),
            padding="max_length",
            max_length=TEXT_MAX_TOKENS,
            truncation=True,
            return_tensors="pt",
        )
        with torch.inference_mode():
            features = self._model.get_text_features(**inputs)
        return self._finalize(features, len(texts))

    # ---- vector contract -------------------------------------------------

    def _finalize(self, features: Any, expected_rows: int) -> np.ndarray:
        raw = features.detach().to("cpu").to(self._torch.float32).numpy()
        if raw.ndim != 2 or raw.shape[0] != expected_rows:
            raise EmbeddingError(
                f"model returned shape {raw.shape}, expected ({expected_rows}, n)"
            )
        if raw.shape[1] != EMBEDDING_DIM:
            raise EmbeddingError(
                f"model returned dimension {raw.shape[1]}, expected {EMBEDDING_DIM}"
            )
        if not np.isfinite(raw).all():
            raise EmbeddingError("model returned non-finite features")
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        if not np.all(norms > 0):
            raise EmbeddingError("model returned a zero-length feature vector")
        vectors = np.ascontiguousarray(raw / norms, dtype=np.float32)
        final = np.linalg.norm(vectors, axis=1)
        if not np.all(np.abs(final - 1.0) <= NORM_TOLERANCE):
            raise EmbeddingError("normalization did not produce unit vectors")
        if not np.isfinite(vectors).all():
            raise EmbeddingError("normalization produced non-finite values")
        return vectors
