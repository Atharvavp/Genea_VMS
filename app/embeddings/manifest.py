"""The frozen embedding-model identity for Component 5.

Everything in this module is a code constant, never a runtime selector. The
model, its revision, its file digests, the embedding dimension, the dtype and
the preprocessing contract together define the vector space that
``system_state.model_id`` records in SQLite. Changing any of them changes the
space, which is why ``MODEL_ID`` embeds the revision, the Transformers version
and a preprocessing generation: stored vectors from a different manifest must
never be mixed with new ones, and startup refuses to open a store whose
recorded identity differs (PLAN sections 6.2, 10.3 step 7 and 13).

The digests below were verified against the Hugging Face repository at the
frozen revision before being written here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping

__all__ = [
    "ModelAsset",
    "ModelManifest",
    "MANIFEST",
    "MODEL_ID",
    "MODEL_REPOSITORY",
    "MODEL_REVISION",
    "EMBEDDING_DIM",
    "EMBEDDING_DTYPE",
    "EMBEDDING_BYTES",
    "TEXT_MAX_TOKENS",
    "IMAGE_SIZE",
    "ManifestError",
]


class ManifestError(RuntimeError):
    """A model asset is missing, the wrong size, or the wrong digest."""


MODEL_REPOSITORY: Final[str] = "google/siglip-base-patch16-224"
MODEL_REVISION: Final[str] = "7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed"

#: Vector-space identity. Stored on every representation row and in
#: ``system_state``; a mismatch requires an explicit offline reindex.
MODEL_ID: Final[str] = "siglip-base-p16-224@7fd15f06-tf4.57.6-prep1"

#: Preprocessing generation, bumped whenever the image/text pipeline changes.
PREPROCESSOR_ID: Final[str] = "siglip-prep1-224-mean0.5-std0.5-pad64"

EMBEDDING_DIM: Final[int] = 768
EMBEDDING_DTYPE: Final[str] = "float32"
#: Exact byte length of one persisted embedding: 768 little-endian float32.
EMBEDDING_BYTES: Final[int] = EMBEDDING_DIM * 4

#: The SigLIP text tower is trained with fixed 64-token padding.
TEXT_MAX_TOKENS: Final[int] = 64
IMAGE_SIZE: Final[int] = 224

#: Norm tolerance for an accepted stored vector (PLAN section 9).
NORM_TOLERANCE: Final[float] = 1e-3


@dataclass(frozen=True, slots=True)
class ModelAsset:
    """One required file in the local snapshot directory."""

    name: str
    size_bytes: int
    sha256: str


REQUIRED_ASSETS: Final[tuple[ModelAsset, ...]] = (
    ModelAsset(
        "model.safetensors",
        812_672_320,
        "2c63cb7d1f2e95ba501893cbb8faeb4ea9a3af295498d35097126228659c2af8",
    ),
    ModelAsset(
        "config.json",
        432,
        "cd85b3d28829722820bcb89a2cfbb4160e55fd359249a3044da724166a8d9688",
    ),
    ModelAsset(
        "preprocessor_config.json",
        368,
        "d11ccb80f15d358a11bdb070e92e2d889005874b7db15823d5f10d9b2533b14a",
    ),
    ModelAsset(
        "tokenizer_config.json",
        711,
        "d6423dae508cc3a129d22ea443841c111832a1a73125b8f25ea8736951698bcb",
    ),
    ModelAsset(
        "tokenizer.json",
        2_399_357,
        "c6e405cb7c670d56636a9402c81023a55bc6c3c53d89cf02b92f5c5005bfe920",
    ),
    ModelAsset(
        "spiece.model",
        798_330,
        "1e5036bed065526c3c212dfbe288752391797c4bb1a284aa18c9a0b23fcaf8ec",
    ),
    ModelAsset(
        "special_tokens_map.json",
        409,
        "2b6a1ff67a27e0df9ac0c7d93250fc0d87431c7b366b3d5669217104f9088a26",
    ),
)

#: The weight digest recorded in ``system_state``; identifies the checkpoint.
WEIGHTS_SHA256: Final[str] = REQUIRED_ASSETS[0].sha256

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Stream a file's SHA-256 without holding it in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ModelManifest:
    """The complete frozen contract, plus verification against a directory."""

    repository: str = MODEL_REPOSITORY
    revision: str = MODEL_REVISION
    model_id: str = MODEL_ID
    preprocessor_id: str = PREPROCESSOR_ID
    dimension: int = EMBEDDING_DIM
    dtype: str = EMBEDDING_DTYPE
    weights_sha256: str = WEIGHTS_SHA256
    assets: tuple[ModelAsset, ...] = REQUIRED_ASSETS

    def verify(self, directory: Path) -> dict[str, str]:
        """Verify every required asset. Raises ``ManifestError`` on any fault.

        Size is checked before the digest so an obviously wrong file fails fast
        without hashing 800 MB. Nothing here downloads, repairs, or renames.
        """
        if not directory.is_dir():
            raise ManifestError(f"model directory is missing: {directory}")
        verified: dict[str, str] = {}
        for asset in self.assets:
            path = directory / asset.name
            if not path.is_file():
                raise ManifestError(f"model asset is missing: {asset.name}")
            actual_size = path.stat().st_size
            if actual_size != asset.size_bytes:
                raise ManifestError(
                    f"model asset {asset.name} has {actual_size} bytes, "
                    f"expected {asset.size_bytes}"
                )
            actual = sha256_file(path)
            if actual != asset.sha256:
                raise ManifestError(
                    f"model asset {asset.name} sha256 {actual} != {asset.sha256}"
                )
            verified[asset.name] = actual
        return verified

    def describe(self) -> Mapping[str, object]:
        """Safe identity fields for /api/index/status and startup logs."""
        return {
            "model_id": self.model_id,
            "model_revision": self.revision,
            "model_sha256": self.weights_sha256,
            "preprocessor_id": self.preprocessor_id,
            "embedding_dim": self.dimension,
            "embedding_dtype": self.dtype,
        }


MANIFEST: Final[ModelManifest] = ModelManifest()
