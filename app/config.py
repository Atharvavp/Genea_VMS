"""Frozen application settings for Component 4.

Every value in this module comes from the environment (or an explicit override
supplied by a test). Nothing here performs I/O beyond resolving paths, and no
value is ever logged.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Final
from urllib.parse import urlsplit

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "Settings",
    "build_test_settings",
    "DEFAULT_MODEL_SHA256",
    "MIN_CURSOR_KEY_BYTES",
    "TEST_CURSOR_SIGNING_KEY",
]

DEFAULT_MODEL_SHA256: Final[str] = (
    "0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1"
)
MIN_CURSOR_KEY_BYTES: Final[int] = 32
TEST_CURSOR_SIGNING_KEY: Final[str] = "test-only-cursor-signing-key-not-a-secret-0123"

_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_LOG_LEVELS: Final[frozenset[str]] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


def _require_absolute(value: Path, field: str) -> Path:
    if not value.is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    parts = PurePosixPath(value.as_posix()).parts
    if ".." in parts:
        raise ValueError(f"{field} must not contain '..'")
    return Path(value.as_posix())


class Settings(BaseSettings):
    """Immutable runtime configuration.

    Instances are frozen: nothing in the application may mutate configuration
    after startup, because worker threads read it without a lock.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        protected_namespaces=(),
    )

    analytics_http_port: int = Field(default=8100, ge=1, le=65535)
    analytics_data_dir: Path = Field(default=Path("/data"))
    analytics_db_path: Path = Field(default=Path("/data/analytics.db"))
    analytics_event_dir: Path = Field(default=Path("/data/events"))
    analytics_model_path: Path = Field(default=Path("/opt/models/yolo11n.pt"))
    analytics_model_sha256: str = Field(default=DEFAULT_MODEL_SHA256)
    analytics_torch_threads: int = Field(default=2, ge=1, le=8)
    analytics_jpeg_quality: int = Field(default=90, ge=70, le=95)
    analytics_stall_seconds: float = Field(default=10.0, ge=5.0, le=60.0)
    analytics_stop_timeout_seconds: float = Field(default=10.0, ge=2.0, le=30.0)
    analytics_log_level: str = Field(default="INFO")
    vms_api_base_url: str = Field(default="http://host.docker.internal:8090")
    cursor_signing_key: str = Field(...)

    # -- validators ---------------------------------------------------------

    @field_validator("analytics_data_dir", "analytics_model_path", mode="after")
    @classmethod
    def _absolute_paths(cls, value: Path, info: ValidationInfo) -> Path:
        return _require_absolute(value, str(info.field_name))

    @field_validator("analytics_db_path", "analytics_event_dir", mode="after")
    @classmethod
    def _absolute_data_paths(cls, value: Path, info: ValidationInfo) -> Path:
        return _require_absolute(value, str(info.field_name))

    @field_validator("analytics_model_sha256", mode="after")
    @classmethod
    def _model_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError(
                "analytics_model_sha256 must be exactly 64 lowercase hex characters"
            )
        return value

    @field_validator("analytics_log_level", mode="before")
    @classmethod
    def _log_level(cls, value: Any) -> str:
        text = str(value).strip().upper()
        if text not in _LOG_LEVELS:
            raise ValueError(
                "analytics_log_level must be one of DEBUG, INFO, WARNING, ERROR"
            )
        return text

    @field_validator(
        "analytics_stall_seconds", "analytics_stop_timeout_seconds", mode="after"
    )
    @classmethod
    def _finite(cls, value: float, info: ValidationInfo) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{info.field_name} must be a finite number")
        return float(value)

    @field_validator("vms_api_base_url", mode="after")
    @classmethod
    def _vms_base_url(cls, value: str) -> str:
        raw = value.strip()
        if not raw:
            raise ValueError("vms_api_base_url must not be empty")
        if len(raw) > 2048:
            raise ValueError("vms_api_base_url is too long")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
            raise ValueError("vms_api_base_url must not contain control characters")
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https"):
            raise ValueError("vms_api_base_url scheme must be http or https")
        if not parts.hostname:
            raise ValueError("vms_api_base_url must contain a host")
        if parts.username or parts.password:
            raise ValueError("vms_api_base_url must not contain credentials")
        if parts.query:
            raise ValueError("vms_api_base_url must not contain a query string")
        if parts.fragment:
            raise ValueError("vms_api_base_url must not contain a fragment")
        if parts.port is not None and not (1 <= parts.port <= 65535):
            raise ValueError("vms_api_base_url port must be between 1 and 65535")
        return raw.rstrip("/")

    @field_validator("cursor_signing_key", mode="after")
    @classmethod
    def _cursor_key(cls, value: str) -> str:
        if len(value.encode("utf-8")) < MIN_CURSOR_KEY_BYTES:
            raise ValueError(
                "cursor_signing_key must be at least "
                f"{MIN_CURSOR_KEY_BYTES} bytes; set CURSOR_SIGNING_KEY"
            )
        return value

    @model_validator(mode="after")
    def _data_root_containment(self) -> "Settings":
        root = self.analytics_data_dir
        for field, path in (
            ("analytics_db_path", self.analytics_db_path),
            ("analytics_event_dir", self.analytics_event_dir),
        ):
            if not path.is_relative_to(root):
                raise ValueError(
                    f"{field} must live beneath analytics_data_dir ({root})"
                )
            if path == root:
                raise ValueError(f"{field} must not be analytics_data_dir itself")
        return self

    # -- derived helpers ----------------------------------------------------

    @property
    def health_probe_dir(self) -> Path:
        """Directory used by the /health writability probe."""
        return self.analytics_data_dir / ".health"

    @property
    def vms_recordings_url(self) -> str:
        """Absolute URL of the Component 3 recording-list endpoint."""
        return f"{self.vms_api_base_url}/api/recordings"

    def event_relative_root(self) -> str:
        """POSIX path of the event directory relative to the data root."""
        return self.analytics_event_dir.relative_to(self.analytics_data_dir).as_posix()


def build_test_settings(**overrides: Any) -> Settings:
    """Explicit test-mode settings factory.

    Production code never calls this. It supplies the one value that has no
    production default (``cursor_signing_key``) so unit tests do not depend on
    the ambient environment.
    """
    values: dict[str, Any] = {
        "cursor_signing_key": TEST_CURSOR_SIGNING_KEY,
        "analytics_data_dir": Path("/data"),
        "analytics_db_path": Path("/data/analytics.db"),
        "analytics_event_dir": Path("/data/events"),
    }
    values.update(overrides)
    return Settings(**values)
