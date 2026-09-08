#!/usr/bin/env python3
"""Test-only RTSP publisher control plane.

Starts and stops deterministic synthetic streams against the private fixture
MediaMTX so an integration test can create, lose, and restore a source on
demand. It is never part of the shipped analytics image.

    POST /start   {"path": "cam1", "codec": "h264", "audio": false, "fps": 15,
                   "width": 320, "height": 240, "pattern": "smpte"}
    POST /stop    {"path": "cam1"}
    POST /stop_all
    GET  /status
    GET  /ready
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUBLISH_BASE = os.environ.get("PUBLISH_BASE", "rtsp://mediamtx:8554")

_LOCK = threading.Lock()
_PROCESSES: dict[str, subprocess.Popen] = {}

_ENCODERS = {
    "h264": ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
             "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", "15"],
    "h265": ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params",
             "log-level=none:keyint=15", "-pix_fmt", "yuv420p"],
}


def _build_command(spec: dict) -> list[str]:
    path = spec["path"]
    codec = spec.get("codec", "h264")
    if codec not in _ENCODERS:
        raise ValueError(f"unsupported codec {codec}")
    fps = int(spec.get("fps", 15))
    width = int(spec.get("width", 320))
    height = int(spec.get("height", 240))
    pattern = spec.get("pattern", "smpte")

    if pattern == "moving":
        # A bright square sweeping left-to-right across a dark frame: a
        # deterministic "object" that crosses a vertical configured line.
        source = [
            "-f", "lavfi", "-i",
            f"color=c=black:s={width}x{height}:r={fps}",
            "-f", "lavfi", "-i",
            f"color=c=white:s=40x40:r={fps}",
            "-filter_complex",
            "[0:v][1:v]overlay=x='mod(t*80\\,W+40)-40':y=(H-h)/2[v]",
            "-map", "[v]",
        ]
    else:
        source = [
            "-f", "lavfi", "-i",
            f"testsrc=size={width}x{height}:rate={fps}",
        ]

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", *source]
    if spec.get("audio"):
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-c:a", "aac", "-b:a", "32k", "-shortest"]
    command += _ENCODERS[codec]
    command += ["-f", "rtsp", "-rtsp_transport", "tcp", f"{PUBLISH_BASE}/{path}"]
    return command


def start(spec: dict) -> dict:
    path = spec["path"]
    with _LOCK:
        existing = _PROCESSES.get(path)
        if existing is not None and existing.poll() is None:
            return {"path": path, "pid": existing.pid, "started": False}
        command = _build_command(spec)
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        _PROCESSES[path] = process
        return {"path": path, "pid": process.pid, "started": True}


def stop(path: str) -> dict:
    with _LOCK:
        process = _PROCESSES.pop(path, None)
    if process is None:
        return {"path": path, "stopped": False}
    if process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    return {"path": path, "stopped": True}


def stop_all() -> dict:
    with _LOCK:
        paths = list(_PROCESSES)
    return {"stopped": [stop(path)["path"] for path in paths]}


def status() -> dict:
    with _LOCK:
        return {
            "base": PUBLISH_BASE,
            "streams": {
                path: {
                    "pid": process.pid,
                    "alive": process.poll() is None,
                    "returncode": process.returncode,
                }
                for path, process in _PROCESSES.items()
            },
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # noqa: D102 - silence access log
        return

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/status"):
            self._respond(200, status())
        elif self.path.startswith("/ready"):
            self._respond(200, {"ready": True})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path.startswith("/start"):
                self._respond(200, start(self._body()))
            elif self.path.startswith("/stop_all"):
                self._respond(200, stop_all())
            elif self.path.startswith("/stop"):
                self._respond(200, stop(self._body()["path"]))
            else:
                self._respond(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._respond(400, {"error": f"{type(exc).__name__}: {exc}"})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    try:
        server.serve_forever()
    finally:
        stop_all()


if __name__ == "__main__":
    main()
