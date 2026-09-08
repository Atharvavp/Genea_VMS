"""Structured, leak-free logging for Component 5.

One stable key/value record per line. Nothing here ever renders a raw search
query, an uploaded filename, image bytes, a vector, a playback URL, an upstream
response body, a model path, or an environment value (PLAN section 16).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any, Final, Mapping

__all__ = [
    "configure_logging",
    "log_event",
    "KeyValueFormatter",
    "RateLimiter",
    "RESERVED_LOG_KEYS",
]

RESERVED_LOG_KEYS: Final[frozenset[str]] = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)

_QUIET_LOGGERS: Final[tuple[str, ...]] = (
    "httpx",
    "httpcore",
    "urllib3",
    "PIL",
    "transformers",
    "asyncio",
    "filelock",
)

_CONTROL_TRANSLATION: Final[dict[int, str]] = {
    c: " " for c in list(range(0x00, 0x20)) + [0x7F]
}


def _scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    text = str(value).translate(_CONTROL_TRANSLATION)
    if len(text) > 400:
        text = text[:397] + "..."
    if any(ch in text for ch in (" ", "=", '"')):
        return '"' + text.replace("\\", "\\\\").replace('"', "'") + '"'
    return text or "-"


class KeyValueFormatter(logging.Formatter):
    """``ts=... level=... logger=... event=... key=value`` records."""

    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            f"ts={time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created))}"
            f".{int(record.msecs):03d}Z",
            f"level={record.levelname}",
            f"logger={record.name}",
            f"event={_scalar(record.getMessage())}",
        ]
        for key, value in record.__dict__.items():
            if key in RESERVED_LOG_KEYS or key.startswith("_"):
                continue
            parts.append(f"{key}={_scalar(value)}")
        if record.exc_info and record.exc_info[0] is not None:
            parts.append(f"exc_type={record.exc_info[0].__name__}")
        return " ".join(parts)


def configure_logging(level: str = "INFO", *, stream: Any = None) -> None:
    """Install the single application log handler. Safe to call repeatedly."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(KeyValueFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True


def safe_extra(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Rename keys that would shadow a LogRecord attribute.

    Python's logging raises ``KeyError`` when an ``extra`` key collides with a
    record attribute; a logging mistake must never fail a request.
    """
    return {
        (f"field_{key}" if key in RESERVED_LOG_KEYS else key): value
        for key, value in fields.items()
    }


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    /,
    exc_info: Any = None,
    **fields: Any,
) -> None:
    """Emit one structured record. ``fields`` must already be caller-sanitized."""
    logger.log(level, event, exc_info=exc_info, extra=safe_extra(fields))


class RateLimiter:
    """First occurrence, then at most once per window, per key.

    Repeated upstream/retry warnings are rate limited by error code so a
    Component 4 outage cannot flood the log (PLAN section 16).
    """

    def __init__(self, window_seconds: float = 30.0, *, clock: Any = time.monotonic):
        self._window = float(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._last: dict[tuple[Any, ...], float] = {}
        self._suppressed: dict[tuple[Any, ...], int] = {}

    def allow(self, *key: Any) -> tuple[bool, int]:
        now = self._clock()
        composite = tuple(key)
        with self._lock:
            last = self._last.get(composite)
            if last is None or (now - last) >= self._window:
                self._last[composite] = now
                return True, self._suppressed.pop(composite, 0)
            self._suppressed[composite] = self._suppressed.get(composite, 0) + 1
            return False, self._suppressed[composite]

    def reset(self, *key: Any) -> None:
        with self._lock:
            composite = tuple(key)
            self._last.pop(composite, None)
            self._suppressed.pop(composite, None)
