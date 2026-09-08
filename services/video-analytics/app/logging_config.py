"""Structured, secret-safe logging for Component 4.

One stable key/value record per line. Nothing in this module ever renders a raw
RTSP URL, a request body, a detection array, or an environment secret.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any, Final, Iterable, Mapping

__all__ = [
    "configure_logging",
    "log_event",
    "KeyValueFormatter",
    "RateLimiter",
    "RESERVED_LOG_KEYS",
]

RESERVED_LOG_KEYS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_QUIET_LOGGERS: Final[tuple[str, ...]] = (
    "httpx",
    "httpcore",
    "urllib3",
    "PIL",
    "matplotlib",
    "asyncio",
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
    """Install the single application log handler.

    Safe to call more than once; the previous handlers are replaced so a test
    or a reload cannot double every line.
    """
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


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    /,
    exc_info: Any = None,
    **fields: Any,
) -> None:
    """Emit one structured record.

    ``event`` is a stable machine-readable code; ``fields`` must already be
    sanitized by the caller.
    """
    logger.log(
        level, event, exc_info=exc_info, extra=ReservedKeyGuard.safe_extra(fields)
    )


class RateLimiter:
    """First occurrence, then at most once per window, per key.

    Used for repetitive source warnings so a flapping camera cannot flood the
    log. Returns the suppressed count alongside the decision.
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
                suppressed = self._suppressed.pop(composite, 0)
                return True, suppressed
            self._suppressed[composite] = self._suppressed.get(composite, 0) + 1
            return False, self._suppressed[composite]

    def reset(self, *key: Any) -> None:
        with self._lock:
            composite = tuple(key)
            self._last.pop(composite, None)
            self._suppressed.pop(composite, None)


def merge_fields(*mappings: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for mapping in mappings:
        merged.update(dict(mapping))  # type: ignore[arg-type]
    return merged


class ReservedKeyGuard(logging.Filter):
    """Rename any ``extra`` key that collides with a LogRecord attribute.

    Python's logging raises ``KeyError`` when an ``extra`` key shadows a record
    attribute. A structured-logging mistake must never take down a request, so
    colliding keys are prefixed rather than allowed to raise.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        return True

    @staticmethod
    def safe_extra(fields: Mapping[str, Any]) -> dict[str, Any]:
        return {
            (f"field_{key}" if key in RESERVED_LOG_KEYS else key): value
            for key, value in fields.items()
        }
