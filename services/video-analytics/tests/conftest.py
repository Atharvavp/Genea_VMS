"""Shared test fixtures for Component 4."""

from __future__ import annotations

import hashlib
import os
import socket
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

BUS_IMAGE_URL = "https://github.com/ultralytics/assets/raw/main/im/bus.jpg"
BUS_IMAGE_SHA256 = "c02019c4979c191eb739ddd944445ef408dad5679acab6fd520ef9d434bfbc63"

ANALYTICS_ENV_PREFIXES = ("ANALYTICS_", "VMS_", "CURSOR_")


# --------------------------------------------------------------------------
# Deterministic clocks and id generators
# --------------------------------------------------------------------------


class FakeClock:
    """Injected monotonic + wall clock with no real sleeping."""

    def __init__(self, monotonic: float = 1000.0, wall: float = 1_788_000_000.0):
        self._monotonic = float(monotonic)
        self._wall = float(wall)

    def monotonic(self) -> float:
        return self._monotonic

    def wall(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._monotonic += float(seconds)
        self._wall += float(seconds)


class SequenceIds:
    """Deterministic replacement for ``secrets.token_hex``."""

    def __init__(self, width: int = 8, start: int = 1):
        self._width = width
        self._next = start

    def __call__(self, nbytes: int | None = None) -> str:
        width = (nbytes * 2) if nbytes else self._width
        value = format(self._next, "x").rjust(width, "0")[-width:]
        self._next += 1
        return value


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sequence_ids() -> SequenceIds:
    return SequenceIds()


# --------------------------------------------------------------------------
# Filesystem / settings
# --------------------------------------------------------------------------


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "events").mkdir(parents=True)
    return root


@pytest.fixture
def settings(data_root: Path):
    from app.config import build_test_settings

    return build_test_settings(
        analytics_data_dir=data_root,
        analytics_db_path=data_root / "analytics.db",
        analytics_event_dir=data_root / "events",
        analytics_model_path=Path("/opt/models/yolo11n.pt"),
        vms_api_base_url="http://vms.invalid:8090",
    )


@pytest.fixture
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient Component 4 environment so a test sees only its inputs."""
    for name in list(os.environ):
        if name.startswith(ANALYTICS_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------
# Test assets
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def bus_image_path() -> Path:
    """The Ultralytics sample image, downloaded (never committed) and verified.

    See THIRD_PARTY_NOTICES.md: redistribution terms for a standalone copy in
    this repository could not be confirmed, so the asset is fetched by the
    opt-in job that needs it and checked against a recorded digest.
    """
    repo_asset = Path(__file__).parent / "assets" / "bus.jpg"
    if repo_asset.is_file():
        target = repo_asset
    else:
        # The working tree is mounted read-only inside the test image, so the
        # downloaded asset is cached in a writable scratch directory instead.
        cache = Path(os.environ.get("TEST_ASSET_CACHE", "/tmp/analytics-test-assets"))
        try:
            cache.mkdir(parents=True, exist_ok=True)
            target = cache / "bus.jpg"
        except OSError:
            target = repo_asset
    if not target.is_file():
        try:
            with urllib.request.urlopen(BUS_IMAGE_URL, timeout=60) as response:
                payload = response.read()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"bus.jpg could not be downloaded: {type(exc).__name__}: {exc}")
        target.write_bytes(payload)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    assert digest == BUS_IMAGE_SHA256, (
        f"tests/assets/bus.jpg digest {digest} does not match the recorded "
        f"{BUS_IMAGE_SHA256}; refusing to use an unverified asset"
    )
    try:
        (target.parent / "bus.jpg.sha256").write_text(f"{digest}  bus.jpg\n")
    except OSError:
        pass  # a read-only cache location is fine; the digest was still checked
    return target


# --------------------------------------------------------------------------
# Fixture service discovery (integration / four_camera tiers)
# --------------------------------------------------------------------------


def _reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def rtsp_base() -> str:
    base = os.environ.get("TEST_RTSP_BASE")
    if not base:
        pytest.skip("TEST_RTSP_BASE is not set; run through docker-compose.test.yml")
    host = base.split("//", 1)[1].split(":")[0]
    port = int(base.rsplit(":", 1)[1])
    if not _reachable(host, port):
        pytest.skip(f"RTSP fixture {base} is not reachable")
    return base


@pytest.fixture(scope="session")
def publisher_url() -> str:
    url = os.environ.get("TEST_PUBLISHER_URL")
    if not url:
        pytest.skip("TEST_PUBLISHER_URL is not set; run through docker-compose.test.yml")
    return url.rstrip("/")


@pytest.fixture
def publisher(publisher_url: str, rtsp_base: str) -> Iterator[Any]:
    """Control handle for the fixture publisher; stops everything it started."""
    from tests.fakes.publisher_client import PublisherClient

    client = PublisherClient(publisher_url)
    client.wait_ready(timeout=60.0)
    try:
        yield client
    finally:
        client.stop_all()
