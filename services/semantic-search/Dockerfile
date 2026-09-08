# syntax=docker/dockerfile:1
#
# Component 5 - Standalone Semantic Image Retrieval & Historical Event Search.
#
# The image contains the service, hash-locked wheels, and the verified SigLIP
# snapshot. It contains no compiler, no git, no curl, no test dependency, and no
# runtime download path: the model is acquired and checksum-verified in the
# build stage only, and the runtime loads it with the Hugging Face hub forced
# offline.

# ---------------------------------------------------------------------------
# Stage 1: runtime-base - the exact OS surface the application runs on.
# ---------------------------------------------------------------------------
FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517 AS runtime-base

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3=3.12.3-0ubuntu2.1 \
      python3-venv=3.12.3-0ubuntu2.1 \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2

# ---------------------------------------------------------------------------
# Stage 2: python-deps - hash-locked wheels and the verified model snapshot.
# ---------------------------------------------------------------------------
FROM runtime-base AS python-deps

RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==25.2"

COPY requirements/torch-cpu.txt /tmp/requirements/torch-cpu.txt
# CPU-only Torch from the official CPU wheel index. An unqualified PyPI Torch on
# this platform attempts to pull CUDA packages; Component 5 P0 is CPU-only.
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps --require-hashes \
      --index-url https://download.pytorch.org/whl/cpu \
      -r /tmp/requirements/torch-cpu.txt

COPY requirements/runtime.lock /tmp/requirements/runtime.lock
RUN /opt/venv/bin/pip install --no-cache-dir --require-hashes \
      -r /tmp/requirements/runtime.lock

# The frozen SigLIP snapshot: exactly the manifest files, at exactly the frozen
# revision, each verified by size and SHA-256 before the directory is promoted.
# The checkpoint is public - no Hugging Face credential is used or accepted.
COPY app/__init__.py /build/app/__init__.py
COPY app/embeddings/__init__.py /build/app/embeddings/__init__.py
COPY app/embeddings/manifest.py /build/app/embeddings/manifest.py
COPY scripts/fetch_model.py /build/scripts/fetch_model.py
RUN /opt/venv/bin/python /build/scripts/fetch_model.py --target /opt/models/siglip \
 && /opt/venv/bin/python /build/scripts/fetch_model.py --target /opt/models/siglip --verify-only

# ---------------------------------------------------------------------------
# Stage 3: final - non-root application image.
# ---------------------------------------------------------------------------
FROM runtime-base AS final

COPY --from=python-deps /opt/venv /opt/venv
COPY --from=python-deps --chown=root:root /opt/models /opt/models

# The hub is offline for every process in this image. A missing or mismatched
# asset must fail model readiness, never trigger a download.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/tmp/hf \
    SEMANTIC_MODEL_DIR=/opt/models/siglip \
    SEMANTIC_DATA_DIR=/data \
    SEMANTIC_DB_PATH=/data/semantic.db

RUN groupadd --system --gid 10002 semantic \
 && useradd --system --uid 10002 --gid 10002 --home-dir /nonexistent \
      --shell /usr/sbin/nologin semantic \
 && mkdir -p /data \
 && chown -R 10002:10002 /data \
 && chmod 0750 /data

WORKDIR /srv/semantic
COPY --chown=root:root app ./app
COPY --chown=root:root scripts ./scripts
COPY --chown=root:root pyproject.toml ./pyproject.toml

ENV PYTHONPATH=/srv/semantic

# Build-time gate: a missing wheel or a corrupt model asset fails the image.
RUN /opt/venv/bin/python scripts/validate_runtime.py \
      --platform --imports --torch --model-manifest \
 && rm -rf /tmp/* /tmp/.[!.]* \
 && chmod 1777 /tmp

USER 10002:10002
EXPOSE 8200
VOLUME ["/data"]

HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=120s \
  CMD ["/opt/venv/bin/python", "scripts/validate_runtime.py", \
       "--health-url", "http://127.0.0.1:8200/health"]

# Exactly one worker: a second process would index the same volume concurrently
# and is refused by the /data process lock.
CMD ["/opt/venv/bin/uvicorn", "app.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8200", "--workers", "1", "--no-access-log"]

# ---------------------------------------------------------------------------
# Stage 4: tests - opt-in image used only by docker-compose.test.yml.
# ---------------------------------------------------------------------------
FROM final AS tests

USER root
COPY requirements/test.lock /tmp/requirements/test.lock
RUN /opt/venv/bin/pip install --no-cache-dir --require-hashes \
      -r /tmp/requirements/test.lock \
 && /opt/venv/bin/playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright \
    HOME=/root
COPY --chown=root:root tests ./tests
CMD ["/opt/venv/bin/pytest", "-q"]
