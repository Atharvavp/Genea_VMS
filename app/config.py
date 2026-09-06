"""Application settings, loaded from the environment."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Every MediaMTX path the VMS owns starts with this. Reconciliation uses it to
# tell "mine, and stale" from "someone else's, leave alone".
MANAGED_PATH_PREFIX = "vms_"

# A MediaMTX duration, restricted to the units this application needs. MediaMTX
# accepts more (`d`, and composite forms such as `1h30m`), but the VMS only ever
# writes a single-unit value and must reject anything it cannot check.
_DURATION_PATTERN = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h)$")
_DURATION_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration_seconds(value: str) -> float | None:
    """Seconds in a MediaMTX duration string, or None if it is not one."""
    match = _DURATION_PATTERN.match(value.strip())
    if match is None:
        return None
    return float(match.group("value")) * _DURATION_UNIT_SECONDS[match.group("unit")]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- MediaMTX -------------------------------------------------------
    # Control API, reachable over the compose network only.
    mediamtx_api_url: str = "http://vms-mediamtx:9997"
    mediamtx_timeout_seconds: float = 5.0

    # --- WebRTC advertisement -------------------------------------------
    # What the browser is told to connect to. These are host-side values: the
    # browser does not live on the compose network.
    public_webrtc_host: str = "localhost"
    public_webrtc_port: int = 8889
    public_webrtc_scheme: str = "http"

    # --- Playback advertisement -----------------------------------------
    # MediaMTX's playback server. The first is reached over the compose
    # network; the rest are what the browser is told to fetch recordings from.
    mediamtx_playback_url: str = "http://vms-mediamtx:9996"
    public_playback_host: str = "localhost"
    public_playback_port: int = 9996
    public_playback_scheme: str = "http"

    # --- Storage --------------------------------------------------------
    database_path: Path = Path("/data/vms.db")
    # Recording root *inside the MediaMTX container*. The VMS never reads it:
    # MediaMTX writes the files and serves them back over the playback server.
    recording_storage_path: str = "/recordings"

    # --- Recording ------------------------------------------------------
    # Minimum length of one recorded segment. Actual boundaries depend on
    # keyframes and stream conditions, so this is a floor, not a guarantee.
    recording_segment_duration: str = "5m"
    # Age after which MediaMTX deletes a finalised recording itself.
    recording_retention_hours: float = 24.0

    @field_validator("recording_segment_duration")
    @classmethod
    def _check_segment_duration(cls, value: str) -> str:
        seconds = parse_duration_seconds(value)
        if seconds is None:
            raise ValueError(
                "RECORDING_SEGMENT_DURATION must be a MediaMTX duration such as "
                "'5m', '30s' or '1h'."
            )
        if seconds <= 0:
            raise ValueError("RECORDING_SEGMENT_DURATION must be positive.")
        return value.strip()

    @field_validator("recording_retention_hours")
    @classmethod
    def _check_retention(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("RECORDING_RETENTION_HOURS must be positive.")
        return value

    @field_validator("recording_storage_path")
    @classmethod
    def _check_storage_path(cls, value: str) -> str:
        trimmed = value.strip().rstrip("/")
        if not trimmed.startswith("/"):
            raise ValueError("RECORDING_STORAGE_PATH must be an absolute path.")
        return trimmed

    # --- Runtime --------------------------------------------------------
    camera_health_poll_seconds: float = 2.0
    # Drift found by the health poll (a missing or stale managed path) triggers
    # a full reconcile, but never more often than this.
    reconcile_min_interval_seconds: float = 10.0

    log_level: str = "INFO"

    @property
    def public_webrtc_base_url(self) -> str:
        return (
            f"{self.public_webrtc_scheme}://{self.public_webrtc_host}"
            f":{self.public_webrtc_port}"
        )

    @property
    def public_playback_base_url(self) -> str:
        return (
            f"{self.public_playback_scheme}://{self.public_playback_host}"
            f":{self.public_playback_port}"
        )

    @property
    def record_path_template(self) -> str:
        """MediaMTX `recordPath`, verified to create `<path>/<date>/<time>.mp4`."""
        return f"{self.recording_storage_path}/%path/%Y-%m-%d/%H-%M-%S-%f"

    @property
    def record_delete_after_duration(self) -> str:
        """`recordDeleteAfter`, as a MediaMTX duration in hours."""
        hours = self.recording_retention_hours
        rendered = f"{hours:.6f}".rstrip("0").rstrip(".")
        return f"{rendered or '0'}h"

    def playback_url(
        self, mediamtx_path: str, start_time: str, duration_seconds: float
    ) -> str:
        """Browser URL for one recorded timespan.

        `format=mp4` is deliberate: it is the standard-MP4 variant, whose `moov`
        box sits at the front of the file, so a native <video> element can start
        playing and seek within what it has buffered. The default fMP4 variant
        is not needed here and is less widely seekable.
        """
        query = (
            f"path={quote(mediamtx_path, safe='')}"
            f"&start={quote(start_time, safe='')}"
            f"&duration={duration_seconds}"
            f"&format=mp4"
        )
        return f"{self.public_playback_base_url}/get?{query}"

    def mediamtx_path_for(self, camera_id: str) -> str:
        """MediaMTX path name owned by a camera.

        `cam_ab12cd34` -> `vms_cam_ab12cd34`. Slashless on purpose: it keeps
        the WHEP URL a single path segment.
        """
        return f"{MANAGED_PATH_PREFIX}{camera_id}"

    def is_managed_path(self, path_name: str) -> bool:
        return path_name.startswith(MANAGED_PATH_PREFIX)

    def whep_url(self, mediamtx_path: str) -> str:
        """WHEP endpoint the browser reads the camera from."""
        return f"{self.public_webrtc_base_url}/{mediamtx_path}/whep"

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
