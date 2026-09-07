"""Server-side event path construction and root-contained resolution.

No function here ever accepts a client-supplied path fragment. Relative paths
are built exclusively from validated identifiers and a parsed UTC date, and any
resolution result must be a strict descendant of the configured data root.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Final

from app.domain.models import CAMERA_ID_RE, EVENT_ID_RE

__all__ = [
    "UnsafeEventPath",
    "event_directory_relative",
    "event_image_relative",
    "temp_directory_name",
    "TEMP_DIR_RE",
    "DAY_RE",
    "resolve_within",
    "is_safe_event_component",
    "IMAGE_FILENAMES",
]

DAY_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TEMP_DIR_RE: Final[re.Pattern[str]] = re.compile(
    r"^\.tmp-evt_[0-9a-f]{32}-[0-9a-f]{16}$"
)
IMAGE_FILENAMES: Final[dict[str, str]] = {"frame": "frame.jpg", "crop": "crop.jpg"}


class UnsafeEventPath(ValueError):
    """A path escaped the event root, or was not built from validated ids."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise UnsafeEventPath(message)


def _day_string(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        text = value.astimezone(UTC).date().isoformat()
    elif isinstance(value, date):
        text = value.isoformat()
    else:
        text = str(value)
    _require(bool(DAY_RE.fullmatch(text)), "event day must be YYYY-MM-DD")
    # Reject a syntactically shaped but impossible date such as 2026-13-40.
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise UnsafeEventPath("event day is not a real calendar date") from exc
    return text


def event_directory_relative(
    events_root_name: str, camera_id: str, day: date | datetime | str, event_id: str
) -> PurePosixPath:
    """``events/<camera_id>/<YYYY-MM-DD>/<event_id>`` relative to the data root."""
    _require(bool(CAMERA_ID_RE.fullmatch(camera_id)), "camera id is not an analytics id")
    _require(bool(EVENT_ID_RE.fullmatch(event_id)), "event id is malformed")
    _require(
        bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", events_root_name)),
        "event root name is not a safe single segment",
    )
    return PurePosixPath(events_root_name) / camera_id / _day_string(day) / event_id


def event_image_relative(
    events_root_name: str,
    camera_id: str,
    day: date | datetime | str,
    event_id: str,
    kind: str,
) -> str:
    filename = IMAGE_FILENAMES.get(kind)
    _require(filename is not None, "image kind must be frame or crop")
    directory = event_directory_relative(events_root_name, camera_id, day, event_id)
    return (directory / str(filename)).as_posix()


def temp_directory_name(event_id: str, nonce: str) -> str:
    """Hidden sibling directory used before the atomic rename."""
    _require(bool(EVENT_ID_RE.fullmatch(event_id)), "event id is malformed")
    _require(bool(re.fullmatch(r"[0-9a-f]{16}", nonce)), "temp nonce is malformed")
    name = f".tmp-{event_id}-{nonce}"
    _require(bool(TEMP_DIR_RE.fullmatch(name)), "temp directory name is malformed")
    return name


def is_safe_event_component(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value)) and value not in (
        ".",
        "..",
    )


def resolve_within(root: Path, relative: str | PurePosixPath) -> Path:
    """Resolve ``relative`` beneath ``root``, rejecting escapes and symlinks.

    ``lstat`` is used on every component so a symlink anywhere along the path -
    not only at its end - is refused.
    """
    raw = str(relative)
    _require(not raw.startswith("/"), "event path must be relative")
    raw_segments = raw.split("/")
    _require(
        all(segment not in ("", ".", "..") for segment in raw_segments),
        "event path must not contain empty, '.', or '..' segments",
    )
    relative_path = PurePosixPath(raw)
    parts = relative_path.parts
    _require(bool(parts), "event path must not be empty")
    for part in parts:
        _require(is_safe_event_component(part), f"unsafe path component: {part!r}")
        _require(part not in (".", ".."), "event path must not contain . or ..")

    resolved_root = root.resolve(strict=False)
    candidate = resolved_root
    for part in parts:
        candidate = candidate / part
        try:
            stat = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UnsafeEventPath("event path could not be inspected") from exc
        import stat as stat_module

        if stat_module.S_ISLNK(stat.st_mode):
            raise UnsafeEventPath("event paths must not traverse a symbolic link")

    final = candidate
    try:
        strict_resolved = final.resolve(strict=False)
    except OSError as exc:
        raise UnsafeEventPath("event path could not be resolved") from exc
    if strict_resolved != final:
        raise UnsafeEventPath("event path resolved outside its literal location")
    if not strict_resolved.is_relative_to(resolved_root):
        raise UnsafeEventPath("event path escaped the data root")
    if strict_resolved == resolved_root:
        raise UnsafeEventPath("event path must be a strict descendant of the root")
    return strict_resolved
