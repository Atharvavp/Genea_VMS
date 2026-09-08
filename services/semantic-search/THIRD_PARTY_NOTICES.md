# Third-party notices — Component 5

Component 5 redistributes no third-party source in this repository. The Docker
image it builds contains the components below, each fetched at build time from
its official distribution point and pinned by version and hash.

## Model weights

**`google/siglip-base-patch16-224`**, revision
`7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed` — **Apache License 2.0**.

Downloaded at build time by `scripts/fetch_model.py`, verified file-by-file
against the digests frozen in `app/embeddings/manifest.py`, and baked into the
image read-only. The weights are **not** committed to this repository. See the
[model card](https://huggingface.co/google/siglip-base-patch16-224).

If you redistribute the built image, you redistribute these weights and their
Apache-2.0 terms apply.

## Python runtime dependencies

Resolved and hash-locked in `requirements/runtime.lock` and
`requirements/torch-cpu.txt`.

| Component | Version | License |
| --- | --- | --- |
| PyTorch (CPU build) | 2.7.1+cpu | BSD-3-Clause |
| torchvision (CPU build) | 0.22.1 | BSD-3-Clause |
| Transformers | 4.57.6 | Apache-2.0 |
| tokenizers | 0.22.2 | Apache-2.0 |
| huggingface-hub | 0.36.2 | Apache-2.0 |
| safetensors | 0.7.0 | Apache-2.0 |
| SentencePiece | 0.2.1 | Apache-2.0 |
| protobuf | 6.32.1 | BSD-3-Clause |
| NumPy | 2.5.3 | BSD-3-Clause |
| Pillow | 11.3.0 | MIT-CMU (HPND) |
| FastAPI | 0.116.1 | MIT |
| Starlette | 0.47.3 | BSD-3-Clause |
| Uvicorn | 0.35.0 | BSD-3-Clause |
| Pydantic / pydantic-settings | 2.13.5 / 2.10.1 | MIT |
| httpx / httpcore | 0.28.1 / 1.0.9 | BSD-3-Clause |
| python-multipart | 0.0.20 | Apache-2.0 |

Their own transitive dependencies are pinned with hashes in the same lock file.

## Test-only dependencies

Installed only in the opt-in `tests` image stage
(`requirements/test.lock`): pytest 8.4.1 (MIT), pytest-asyncio 1.1.0 (Apache-2.0),
Playwright 1.55.0 (Apache-2.0) with Chromium, psutil 7.2.2 (BSD-3-Clause).

## Base image

`ubuntu:24.04`, digest-pinned. Ubuntu package licences apply to the packages it
contains.

## Evaluation corpus

`tests/assets/semantic_eval_manifest.json` records identities, digests and
labels only. The images it describes are captured from the operator's own
Component 4 deployment into the gitignored `tests/assets/cache/` directory by
`scripts/capture_eval_set.py`. **No image from any upstream component is
committed to or redistributed with this repository.**
