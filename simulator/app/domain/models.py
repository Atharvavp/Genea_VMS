"""Domain models: camera configuration, source metadata, and API payloads."""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STREAM_PATH_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_NAME_LENGTH = 100


class CameraStatus(str, Enum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


#: Statuses in which a lifecycle operation is already in flight.
TRANSITIONAL_STATUSES = frozenset({CameraStatus.STARTING, CameraStatus.STOPPING})


class Codec(str, Enum):
    H264 = "h264"
    H265 = "h265"


class SourceKind(str, Enum):
    UPLOAD = "upload"
    LOCAL = "local"


# --- Video output configuration ----------------------------------------


class SourceResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["source"] = "source"


class FixedResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["fixed"]
    width: int = Field(ge=2, le=7680)
    height: int = Field(ge=2, le=7680)

    @field_validator("width", "height")
    @classmethod
    def _must_be_even(cls, value: int) -> int:
        if value % 2 != 0:
            raise ValueError("must be an even number of pixels")
        return value


Resolution = Annotated[SourceResolution | FixedResolution, Field(discriminator="mode")]


class SourceFps(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["source"] = "source"


class FixedFps(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["fixed"]
    value: float = Field(gt=0, le=120)


Fps = Annotated[SourceFps | FixedFps, Field(discriminator="mode")]


class AutoBitrate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["auto"] = "auto"


class FixedBitrate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["fixed"]
    kbps: int = Field(ge=64, le=50000)


Bitrate = Annotated[AutoBitrate | FixedBitrate, Field(discriminator="mode")]


class VideoConfig(BaseModel):
    """Virtual camera output settings (what the RTSP stream looks like)."""

    model_config = ConfigDict(extra="forbid")

    codec: Codec = Codec.H264
    resolution: Resolution = SourceResolution()
    fps: Fps = SourceFps()
    bitrate: Bitrate = AutoBitrate()


class VideoConfigUpdate(BaseModel):
    """Partial video config; unset fields keep their current value."""

    model_config = ConfigDict(extra="forbid")

    codec: Codec | None = None
    resolution: Resolution | None = None
    fps: Fps | None = None
    bitrate: Bitrate | None = None


# --- Source -------------------------------------------------------------


class SourceSpec(BaseModel):
    """Which video file backs the camera.

    ``upload``: the multipart ``file`` part carries the video.
    ``local``: ``local_path`` names a file inside the mounted source directory.
    """

    model_config = ConfigDict(extra="forbid")

    kind: SourceKind
    local_path: str | None = None

    @model_validator(mode="after")
    def _check_local_path(self) -> "SourceSpec":
        if self.kind is SourceKind.LOCAL:
            if not self.local_path or not self.local_path.strip():
                raise ValueError("local_path is required when source.kind is 'local'")
        elif self.local_path is not None:
            raise ValueError("local_path is only valid when source.kind is 'local'")
        return self


class SourceMetadata(BaseModel):
    """What ffprobe reported about the source file."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    format: str | None = None
    duration_seconds: float | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate_kbps: int | None = None
    has_audio: bool = False
    size_bytes: int | None = None


# --- API payloads -------------------------------------------------------


class CameraCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    stream_path: str | None = None
    source: SourceSpec | None = None
    auto_start: bool = False
    loop: bool = True
    video: VideoConfig = VideoConfig()

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return clean_name(value)

    @field_validator("stream_path")
    @classmethod
    def _check_stream_path(cls, value: str | None) -> str | None:
        return None if value is None else validate_stream_path(value)


class CameraUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    stream_path: str | None = None
    source: SourceSpec | None = None
    auto_start: bool | None = None
    loop: bool | None = None
    video: VideoConfigUpdate | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        return None if value is None else clean_name(value)

    @field_validator("stream_path")
    @classmethod
    def _check_stream_path(cls, value: str | None) -> str | None:
        return None if value is None else validate_stream_path(value)


# --- Persisted record ---------------------------------------------------


class CameraRecord(BaseModel):
    """A camera as stored in SQLite."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    stream_path: str
    source_kind: SourceKind
    source_path: str
    source_original_filename: str
    source_stored_filename: str | None = None
    source_metadata: SourceMetadata
    video: VideoConfig
    loop: bool = True
    auto_start: bool = False
    status: CameraStatus = CameraStatus.CREATED
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime

    def owns_uploaded_file(self) -> bool:
        return (
            self.source_kind is SourceKind.UPLOAD
            and self.source_stored_filename is not None
        )

    def runtime_config_signature(self) -> tuple[Any, ...]:
        """Fields whose change requires a running publisher to be restarted."""
        return (
            self.stream_path,
            self.source_path,
            self.loop,
            tuple(sorted(self.video.model_dump(mode="json").items())),
        )


class RuntimeInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pid: int | None = None
    started_at: datetime | None = None


class CameraView(BaseModel):
    """API response shape."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    stream_path: str
    rtsp_url: str
    auto_start: bool
    loop: bool
    status: CameraStatus
    video: VideoConfig
    source: SourceMetadata
    source_kind: SourceKind
    runtime: RuntimeInfo
    last_error: str | None
    created_at: datetime
    updated_at: datetime


# --- Helpers ------------------------------------------------------------


def clean_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("name must not be blank")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ValueError(f"name must be at most {MAX_NAME_LENGTH} characters")
    return cleaned


def validate_stream_path(value: str) -> str:
    candidate = value.strip().lower()
    if not STREAM_PATH_PATTERN.match(candidate):
        raise ValueError(
            "stream_path must match ^[a-z0-9][a-z0-9_-]{0,63}$ "
            "(lowercase letters, digits, '-' and '_', no slashes)"
        )
    return candidate


def slugify_stream_path(name: str) -> str:
    """Derive a valid stream path from a camera name."""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:64].strip("-")
    if not slug or not STREAM_PATH_PATTERN.match(slug):
        slug = f"camera-{uuid.uuid4().hex[:8]}"
    return slug


def new_camera_id() -> str:
    return f"cam_{uuid.uuid4().hex[:8]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
