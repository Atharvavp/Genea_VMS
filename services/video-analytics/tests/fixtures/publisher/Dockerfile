# Deterministic RTSP publisher used only by the Component 4 test overlay.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY control.py /srv/control.py
EXPOSE 8000
CMD ["python3", "/srv/control.py"]
