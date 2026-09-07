"""HTTP client for the test-only fixture publisher."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


class PublisherClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _post(self, path: str, payload: dict[str, Any] | None = None) -> dict:
        data = json.dumps(payload or {}).encode()
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.loads(response.read() or b"{}")

    def _get(self, path: str, timeout: float = 10.0) -> dict:
        with urllib.request.urlopen(  # noqa: S310
            f"{self.base_url}{path}", timeout=timeout
        ) as response:
            return json.loads(response.read() or b"{}")

    def wait_ready(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._get("/ready", timeout=3.0)
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.5)
        raise RuntimeError(f"publisher fixture never became ready: {last}")

    def start(self, path: str, **spec: Any) -> dict:
        return self._post("/start", {"path": path, **spec})

    def stop(self, path: str) -> dict:
        return self._post("/stop", {"path": path})

    def stop_all(self) -> dict:
        try:
            return self._post("/stop_all")
        except Exception:  # noqa: BLE001 - teardown must never fail a test run
            return {"stopped": []}

    def status(self) -> dict:
        return self._get("/status")
