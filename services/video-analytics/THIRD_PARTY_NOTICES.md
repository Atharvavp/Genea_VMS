# Third-party notices

Component 4 redistributes no third-party source code. It depends on the
following third-party components at build and run time.

## Runtime dependencies

| Component | Version | Licence | Obtained from |
|---|---|---|---|
| Ubuntu base image | 24.04 (`sha256:33ceb719...b987517`) | various (Ubuntu archive) | Docker Hub `ubuntu:24.04` |
| GStreamer 1.0 + PyGObject | 1.24.x / 3.48.2 | LGPL-2.1-or-later | Ubuntu 24.04 archive |
| PyTorch (`torch`, `torchvision`) | 2.7.1+cpu / 0.22.1+cpu | BSD-3-Clause | `https://download.pytorch.org/whl/cpu` |
| Ultralytics | 8.4.49 | AGPL-3.0 | PyPI |
| Roboflow `trackers` | 2.6.0 | Apache-2.0 | PyPI |
| `supervision` | 0.30.2 | MIT | PyPI |
| FastAPI / Starlette / Uvicorn / Pydantic | see `requirements/runtime.lock` | MIT / BSD-3-Clause | PyPI |
| Pillow | 11.3.0 | MIT-CMU | PyPI |
| httpx | 0.28.1 | BSD-3-Clause | PyPI |

### Ultralytics AGPL-3.0 notice

`ultralytics` and the **YOLO11n** weights are distributed under **AGPL-3.0**.
This repository uses them unmodified. The weights are downloaded at image build
time from the pinned release asset

```
https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt
SHA-256 0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1
```

and are **not** committed to this repository. Anyone deploying this service over
a network must honour the AGPL-3.0 source-availability obligation for the
Ultralytics parts, or replace the detector with a differently licensed model.

## Test assets

| Asset | Source | Licence / terms |
|---|---|---|
| `tests/assets/bus.jpg` | `https://github.com/ultralytics/assets/raw/main/im/bus.jpg` (Ultralytics sample image; the Ultralytics assets project is AGPL-3.0) | Redistribution terms for a standalone copy inside this repository could not be confirmed, so the file is **not committed**. The opt-in `real_model` integration job downloads it at test time and verifies SHA-256 `c02019c4979c191eb739ddd944445ef408dad5679acab6fd520ef9d434bfbc63` (810x1080 baseline JPEG, 137419 bytes, verified 2026-09-07) before use. |

`tests/assets/` therefore contains no binary blob in version control; see
`tests/conftest.py::bus_image_path`.
