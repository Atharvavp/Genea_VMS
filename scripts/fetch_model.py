#!/usr/bin/env python3
"""Build-time acquisition of the frozen SigLIP snapshot. Never used at runtime.

Downloads exactly the files named in ``app/embeddings/manifest.py`` at exactly
the frozen revision, verifies size and SHA-256 for each one, and only then
renames the staging directory into place. A partial or tampered download
therefore cannot become the model directory.

No Hugging Face credential is used or accepted: the checkpoint is public and the
runtime image must never need one. The runtime never calls this script - it sets
``HF_HUB_OFFLINE=1`` and loads from the verified directory.

    python scripts/fetch_model.py --target /opt/models/siglip
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.embeddings.manifest import MANIFEST, ManifestError, sha256_file  # noqa: E402

_BASE = "https://huggingface.co/{repo}/resolve/{revision}/{name}"
_CHUNK = 1024 * 1024
_ATTEMPTS = 3


def _download(url: str, destination: Path) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": "genea-c5-build/1.0"})
    written = 0
    with urllib.request.urlopen(request, timeout=300) as response:
        with destination.open("wb") as handle:
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
    return written


def fetch(target: Path) -> int:
    staging = Path(tempfile.mkdtemp(prefix=".siglip-staging-", dir=str(target.parent)))
    try:
        for asset in MANIFEST.assets:
            url = _BASE.format(
                repo=MANIFEST.repository, revision=MANIFEST.revision, name=asset.name
            )
            path = staging / asset.name
            last: Exception | None = None
            for attempt in range(1, _ATTEMPTS + 1):
                try:
                    written = _download(url, path)
                    last = None
                    break
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    last = exc
                    print(
                        f"retry {attempt}/{_ATTEMPTS} {asset.name}: "
                        f"{type(exc).__name__}",
                        file=sys.stderr,
                    )
            if last is not None:
                raise SystemExit(f"download failed for {asset.name}: {last!r}")
            if written != asset.size_bytes:
                raise SystemExit(
                    f"{asset.name}: downloaded {written} bytes, "
                    f"expected {asset.size_bytes}"
                )
            digest = sha256_file(path)
            if digest != asset.sha256:
                raise SystemExit(
                    f"{asset.name}: sha256 {digest} != expected {asset.sha256}"
                )
            print(f"verified {asset.name} bytes={written} sha256={digest}")

        # Full-manifest verification of the staging copy before it is promoted.
        MANIFEST.verify(staging)

        if target.exists():
            shutil.rmtree(target)
        os.rename(staging, target)
        for entry in target.iterdir():
            entry.chmod(0o444)
        target.chmod(0o555)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(
        f"model ready repo={MANIFEST.repository} revision={MANIFEST.revision} "
        f"model_id={MANIFEST.model_id} target={target}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="/opt/models/siglip", type=Path)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify an existing directory instead of downloading",
    )
    args = parser.parse_args(argv)
    target: Path = args.target

    if args.verify_only:
        try:
            MANIFEST.verify(target)
        except ManifestError as exc:
            print(f"manifest verification failed: {exc}", file=sys.stderr)
            return 1
        print(f"manifest verified target={target} model_id={MANIFEST.model_id}")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    return fetch(target)


if __name__ == "__main__":
    raise SystemExit(main())
