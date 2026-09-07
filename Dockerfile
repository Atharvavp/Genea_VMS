# syntax=docker/dockerfile:1
#
# Component 4 - Standalone Video Analytics & Event Detection.
#
# Ubuntu system Python is used deliberately: Debian/Ubuntu compile PyGObject
# against the distribution interpreter, so a `python:*-slim` base would require
# building PyGObject from source. A `--system-site-packages` venv gives the
# application pinned PyPI wheels *and* the distribution `gi` bindings.

# ---------------------------------------------------------------------------
# Stage 1: runtime-base - the exact OS surface the application runs on.
# ---------------------------------------------------------------------------
FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517 AS runtime-base

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Versions below are the set validated for this image. They are pinned together;
# never partially pin a stale revision (see PLAN section 15.1).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3=3.12.3-0ubuntu2.1 \
      python3-venv=3.12.3-0ubuntu2.1 \
      python3-gi=3.48.2-1 \
      gir1.2-gstreamer-1.0=1.24.2-1ubuntu0.1 \
      gir1.2-gst-plugins-base-1.0=1.24.2-1ubuntu0.4 \
      gstreamer1.0-tools=1.24.2-1ubuntu0.1 \
      gstreamer1.0-plugins-base=1.24.2-1ubuntu0.4 \
      gstreamer1.0-plugins-good=1.24.2-1ubuntu1.5 \
      gstreamer1.0-plugins-bad=1.24.2-1ubuntu4 \
      gstreamer1.0-libav=1.24.1-1build1 \
      libgl1 \
      libglib2.0-0t64 \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    GST_REGISTRY=/tmp/gstreamer-registry.bin \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2

# ---------------------------------------------------------------------------
# Stage 2: python-deps - hash-locked wheels and the pinned model asset.
# ---------------------------------------------------------------------------
FROM runtime-base AS python-deps

RUN python3 -m venv --system-site-packages /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==25.2"

COPY requirements/torch-cpu.txt /tmp/requirements/torch-cpu.txt
# CPU-only Torch, from the official CPU wheel index. Installing an unqualified
# PyPI Torch on this platform attempts to pull CUDA packages.
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps --require-hashes \
      --index-url https://download.pytorch.org/whl/cpu \
      -r /tmp/requirements/torch-cpu.txt

COPY requirements/runtime.lock /tmp/requirements/runtime.lock
RUN /opt/venv/bin/pip install --no-cache-dir --require-hashes \
      -r /tmp/requirements/runtime.lock

# The model is acquired at build time only; the runtime image has no download path.
ARG MODEL_URL=https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt
ARG MODEL_SHA256=0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1
RUN mkdir -p /opt/models \
 && python3 - "$MODEL_URL" "$MODEL_SHA256" <<'PY'
import hashlib, os, sys, urllib.request
url, expected = sys.argv[1], sys.argv[2]
tmp = "/opt/models/.yolo11n.pt.partial"
with urllib.request.urlopen(url, timeout=180) as response, open(tmp, "wb") as handle:
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        handle.write(chunk)
digest = hashlib.sha256()
with open(tmp, "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
actual = digest.hexdigest()
if actual != expected:
    os.unlink(tmp)
    raise SystemExit(f"model sha256 mismatch: expected {expected} got {actual}")
os.rename(tmp, "/opt/models/yolo11n.pt")
print(f"model verified sha256={actual} bytes={os.path.getsize('/opt/models/yolo11n.pt')}")
PY

# Ultralytics writes a settings file and a cache on first import. Bake a seed
# copy with telemetry sync disabled; the runtime copies it into /tmp, so no
# application directory needs to be writable and the service never phones home.
COPY scripts/seed_ultralytics.py /tmp/seed_ultralytics.py
RUN mkdir -p /opt/ultralytics-seed \
 && YOLO_CONFIG_DIR=/opt/ultralytics-seed /opt/venv/bin/python -c "import ultralytics; print(ultralytics.__version__)" \
 && /opt/venv/bin/python /tmp/seed_ultralytics.py --harden /opt/ultralytics-seed \
 && chmod -R a+rX /opt/ultralytics-seed

# ---------------------------------------------------------------------------
# Stage 3: final - non-root application image.
# ---------------------------------------------------------------------------
FROM runtime-base AS final

COPY --from=python-deps /opt/venv /opt/venv
COPY --from=python-deps /opt/models /opt/models
COPY --from=python-deps /opt/ultralytics-seed /opt/ultralytics-seed

ENV YOLO_CONFIG_DIR=/tmp/ultralytics \
    ULTRALYTICS_SEED_DIR=/opt/ultralytics-seed \
    MPLCONFIGDIR=/tmp/matplotlib \
    ANALYTICS_DATA_DIR=/data \
    ANALYTICS_DB_PATH=/data/analytics.db \
    ANALYTICS_EVENT_DIR=/data/events \
    ANALYTICS_MODEL_PATH=/opt/models/yolo11n.pt

RUN groupadd --system --gid 10001 analytics \
 && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent \
      --shell /usr/sbin/nologin analytics \
 && mkdir -p /data /data/events \
 && chown -R 10001:10001 /data \
 && chmod 0750 /data /data/events

WORKDIR /srv/analytics
COPY --chown=root:root app ./app
COPY --chown=root:root scripts ./scripts
COPY --chown=root:root pyproject.toml ./pyproject.toml

ENV PYTHONPATH=/srv/analytics

# Build-time gate: a missing plugin, import, or model asset fails the image.
# The scratch directories the libraries create while running as root are then
# removed, so the unprivileged runtime user gets a clean, writable /tmp.
RUN /opt/venv/bin/python scripts/validate_runtime.py --platform --imports --gstreamer --torch --model-metadata \
 && rm -rf /tmp/* /tmp/.[!.]* \
 && chmod 1777 /tmp

USER 10001:10001
EXPOSE 8100
VOLUME ["/data"]

HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=60s \
  CMD ["/opt/venv/bin/python", "scripts/validate_runtime.py", \
       "--health-url", "http://127.0.0.1:8100/health"]

CMD ["/opt/venv/bin/uvicorn", "app.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8100", "--workers", "1", "--no-access-log"]

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
