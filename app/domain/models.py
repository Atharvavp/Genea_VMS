"""Domain models: what the API accepts, what it returns, what is stored.

Two shapes of state live here and are deliberately kept apart:

* **Desired state** - ``CameraRecord``, persisted in SQLite. Name, source URL,
  enabled flag. This is what the VMS is asked to do.
* **Runtime state** - ``CameraHealth``, derived from MediaMTX on every poll and
  never written to the database. This is what is currently true.

A third kind of state, the browser's WebRTC session, lives only in the browser.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.security.rtsp_url import InvalidRtspUrl, validate_rtsp_url

NAME_MAX_LENGTH = 100
CAMERA_ID_PATTERN = re.compile(r"^cam_[0-9a-f]{8}$")


class CameraHealthState(str, Enum):
    """Can the VMS currently obtain this configured RTSP source?

    Distinct from the browser player state, which the backend never sees.
    """

    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


def utcnow_iso() -> str:
    # Milliseconds, not seconds: two cameras created in the same second must
    # not tie when the grid is ordered by creation time.
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_camera_id() -> str:
    return f"cam_{uuid.uuid4().hex[:8]}"


def normalize_name(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("Name must not be blank.")
    if len(trimmed) > NAME_MAX_LENGTH:
        raise ValueError(f"Name must be at most {NAME_MAX_LENGTH} characters.")
    return trimmed


def normalize_rtsp_url(value: str) -> str:
    try:
        return validate_rtsp_url(value).url
    except InvalidRtspUrl as exc:
        raise ValueError(str(exc)) from exc


# --- Requests ---------------------------------------------------------------


class CameraCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    rtsp_url: str
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        return normalize_name(value)

    @field_validator("rtsp_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return normalize_rtsp_url(value)


class CameraUpdate(BaseModel):
    """Partial update. Omitted fields are left as they are.

    ``rtsp_url`` must be omitted (not sent as empty) to keep the current source;
    that is what the edit form does when the stored URL carries credentials and
    is therefore never shown back to the user.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    rtsp_url: str | None = None
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str | None) -> str | None:
        return None if value is None else normalize_name(value)

    @field_validator("rtsp_url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        return None if value is None else normalize_rtsp_url(value)

    @property
    def provided_fields(self) -> set[str]:
        """Field names the caller actually sent, so `null` is not a silent no-op."""
        return set(self.model_fields_set)


# --- Responses --------------------------------------------------------------


class CameraHealth(BaseModel):
    """Source availability, as last derived from MediaMTX."""

    state: CameraHealthState = CameraHealthState.UNKNOWN
    last_error: str | None = None
    checked_at: str | None = None
    mediamtx_available: bool = False


class CameraView(BaseModel):
    """A camera as the API and UI see it.

    Note what is *not* here: the stored RTSP URL. ``rtsp_url_display`` has its
    password masked, and there is no endpoint that returns the raw value.
    """

    id: str
    name: str
    rtsp_url_display: str
    has_credentials: bool
    enabled: bool
    mediamtx_path: str
    webrtc_url: str
    health: CameraHealth
    created_at: str
    updated_at: str


# --- Persistence ------------------------------------------------------------


@dataclass(frozen=True)
class CameraRecord:
    """One row of `cameras`: the desired state, nothing derived."""

    id: str
    name: str
    rtsp_url: str
    mediamtx_path: str
    enabled: bool
    created_at: str
    updated_at: str
