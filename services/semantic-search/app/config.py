"""Immutable, validated process settings for Component 5.

Every value is parsed once at startup and never re-read. Two rules matter more
than the individual ranges:

* ``COMPONENT4_API_BASE_URL`` is the ONLY upstream this service may contact, and
  it is a fixed base - no caller, event, or upstream response may ever influence
  the host of an outbound request (PLAN sections 11 and 14.6).
* every writable path must resolve strictly beneath the Component 5 data root.
  Component 5 owns exactly one volume and never mounts Component 2-4 storage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "MODEL_DIR_DEFAULT"]

MODEL_DIR_DEFAULT: Final[str] = "/opt/models/siglip"
_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


class Settings(BaseSettings):
    """Frozen runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        frozen=True,
        # `model_` is a pydantic-protected prefix; the model directory setting
        # below is namespaced under SEMANTIC_ so nothing collides.
        protected_namespaces=(),
    )

    semantic_http_port: int = Field(default=8200, ge=1, le=65535)
    semantic_data_dir: Path = Field(default=Path("/data"))
    semantic_db_path: Path = Field(default=Path("/data/semantic.db"))
    semantic_model_dir: Path = Field(default=Path(MODEL_DIR_DEFAULT))

    component4_api_base_url: str = Field(default="http://host.docker.internal:8100")

    semantic_torch_threads: int = Field(default=2, ge=1, le=8)
    semantic_index_batch_size: int = Field(default=4, ge=1, le=8)
    semantic_poll_interval_seconds: int = Field(default=10, ge=2, le=300)
    semantic_overlap_seconds: int = Field(default=300, ge=60, le=3600)
    semantic_full_reconcile_seconds: int = Field(default=21600, ge=300, le=86400)
    semantic_max_retry_seconds: int = Field(default=300, ge=30, le=3600)

    semantic_upload_max_bytes: int = Field(default=8_388_608, ge=1_048_576, le=16_777_216)
    semantic_upload_max_pixels: int = Field(default=16_777_216, ge=1_000_000, le=32_000_000)
    semantic_upstream_image_max_bytes: int = Field(
        default=16_777_216, ge=1_048_576, le=33_554_432
    )

    semantic_shutdown_timeout_seconds: int = Field(default=15, ge=5, le=60)
    semantic_log_level: str = Field(default="INFO")

    @field_validator("semantic_data_dir", "semantic_db_path", "semantic_model_dir")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("must be an absolute path")
        if any(part == ".." for part in value.parts):
            raise ValueError("must not contain '..'")
        return value

    @field_validator("semantic_log_level")
    @classmethod
    def _level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("must be one of DEBUG, INFO, WARNING, ERROR")
        return normalized

    @field_validator("component4_api_base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        # Control characters are rejected on the RAW value, before any
        # trimming: a stripped trailing newline would otherwise hide one.
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise ValueError("must not contain control characters")
        raw = value.strip(" ")
        if not raw:
            raise ValueError("must not be empty")
        if len(raw) > 2048:
            raise ValueError("is too long")
        parts = urlsplit(raw)
        if parts.scheme not in _ALLOWED_SCHEMES:
            raise ValueError("must use http or https")
        if not parts.hostname:
            raise ValueError("must include a host")
        if parts.username or parts.password:
            raise ValueError("must not embed credentials")
        if parts.query or parts.fragment:
            raise ValueError("must not include a query or fragment")
        path = parts.path.rstrip("/")
        if path:
            raise ValueError("must not include a path")
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    @model_validator(mode="after")
    def _paths_beneath_data_root(self) -> "Settings":
        root = self.semantic_data_dir
        for name in ("semantic_db_path",):
            candidate: Path = getattr(self, name)
            if candidate == root or root not in candidate.parents:
                raise ValueError(f"{name} must be strictly beneath semantic_data_dir")
        return self

    # ---- derived, read-only helpers -------------------------------------

    @property
    def quarantine_dir(self) -> Path:
        return self.semantic_data_dir / "quarantine"

    @property
    def lock_path(self) -> Path:
        return self.semantic_data_dir / "semantic.lock"

    @property
    def upload_max_edge(self) -> int:
        """Maximum edge length accepted for an uploaded query image."""
        return 4096
