"""Application settings, loaded from the environment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Every MediaMTX path the VMS owns starts with this. Reconciliation uses it to
# tell "mine, and stale" from "someone else's, leave alone".
MANAGED_PATH_PREFIX = "vms_"


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

    # --- Storage --------------------------------------------------------
    database_path: Path = Path("/data/vms.db")

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
