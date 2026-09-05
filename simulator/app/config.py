"""Application settings, loaded from the environment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- RTSP endpoints -------------------------------------------------
    # Where FFmpeg publishes (reachable from inside the simulator container).
    mediamtx_host: str = "mediamtx"
    mediamtx_rtsp_port: int = 8554
    # What we advertise to RTSP clients (reachable from the user's machine).
    public_rtsp_host: str = "localhost"
    public_rtsp_port: int = 8554
    rtsp_path_prefix: str = "simulator"

    # --- Storage --------------------------------------------------------
    data_dir: Path = Path("/data")
    database_path: Path = Path("/data/simulator.db")
    upload_video_dir: Path = Path("/data/videos")
    source_video_dir: Path = Path("/data/local-sources")
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024

    # --- Subprocesses ---------------------------------------------------
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    ffmpeg_startup_grace_seconds: float = 2.0
    ffmpeg_stop_timeout_seconds: float = 5.0
    ffmpeg_stderr_tail_lines: int = 50
    ffprobe_timeout_seconds: float = 20.0

    log_level: str = "INFO"

    def internal_rtsp_url(self, stream_path: str) -> str:
        """URL FFmpeg publishes to."""
        return (
            f"rtsp://{self.mediamtx_host}:{self.mediamtx_rtsp_port}"
            f"/{self.rtsp_path_prefix}/{stream_path}"
        )

    def public_rtsp_url(self, stream_path: str) -> str:
        """URL shown in the UI/API for external players."""
        return (
            f"rtsp://{self.public_rtsp_host}:{self.public_rtsp_port}"
            f"/{self.rtsp_path_prefix}/{stream_path}"
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_video_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        # source_video_dir is a read-only mount in Docker; create it only if missing.
        if not self.source_video_dir.exists():
            try:
                self.source_video_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass


@lru_cache
def get_settings() -> Settings:
    return Settings()
