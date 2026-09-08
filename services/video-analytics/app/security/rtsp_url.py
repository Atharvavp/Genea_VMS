"""RTSP URL validation, masking, and free-text scrubbing.

A stored RTSP URL may contain credentials. It is written to exactly two places:
the analytics SQLite row, and the GStreamer ``rtspsrc location`` property.
Everything else in this process - API responses, HTML, logs, tracebacks, health
data, validation-error echoes - must go through the helpers in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Iterable
from urllib.parse import quote, urlsplit

__all__ = [
    "InvalidRtspUrl",
    "ParsedRtspUrl",
    "validate_rtsp_url",
    "parse_rtsp_url",
    "mask_rtsp_url",
    "has_credentials",
    "scrub_text",
    "safe_gst_error",
    "MAX_RTSP_URL_LENGTH",
    "MAX_SAFE_ERROR_LENGTH",
]

MAX_RTSP_URL_LENGTH: Final[int] = 2048
MAX_SAFE_ERROR_LENGTH: Final[int] = 300

_CONTROL_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_USERINFO_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/@\s]+)@"
)
_MASK: Final[str] = "***"


class InvalidRtspUrl(ValueError):
    """Raised for a syntactically unusable RTSP URL.

    The offending value is deliberately never included in the message.
    """


@dataclass(frozen=True, slots=True)
class ParsedRtspUrl:
    raw: str
    masked: str
    host: str
    port: int | None
    has_credentials: bool


def _reject(reason: str) -> "ParsedRtspUrl":
    raise InvalidRtspUrl(reason)


def parse_rtsp_url(value: str) -> ParsedRtspUrl:
    """Validate ``value`` and return its parsed, masked form.

    Syntax only: an unreachable but well-formed URL is accepted, because a
    source that is merely down must surface as ``RECONNECTING``, not as a
    rejected configuration.
    """
    if not isinstance(value, str):
        _reject("RTSP URL must be a string")
    raw = value.strip()
    if not raw:
        _reject("RTSP URL must not be empty")
    if len(raw) > MAX_RTSP_URL_LENGTH:
        _reject(f"RTSP URL must be at most {MAX_RTSP_URL_LENGTH} characters")
    if _CONTROL_RE.search(raw):
        _reject("RTSP URL must not contain control characters")
    if any(ch.isspace() for ch in raw):
        _reject("RTSP URL must not contain whitespace")

    try:
        parts = urlsplit(raw)
    except ValueError:
        _reject("RTSP URL could not be parsed")

    if parts.scheme.lower() != "rtsp":
        _reject("RTSP URL scheme must be rtsp")
    if parts.fragment:
        _reject("RTSP URL must not contain a fragment")

    try:
        host = parts.hostname
    except ValueError:
        _reject("RTSP URL host could not be parsed")
        host = None
    if not host:
        _reject("RTSP URL must contain a host")

    try:
        port = parts.port
    except ValueError:
        _reject("RTSP URL port must be an integer between 1 and 65535")
        port = None
    if port is not None and not (1 <= port <= 65535):
        _reject("RTSP URL port must be between 1 and 65535")

    credentials = bool(parts.username) or bool(parts.password)
    return ParsedRtspUrl(
        raw=raw,
        masked=mask_rtsp_url(raw),
        host=str(host),
        port=port,
        has_credentials=credentials,
    )


def validate_rtsp_url(value: str) -> str:
    """Return the normalised raw URL, or raise :class:`InvalidRtspUrl`."""
    return parse_rtsp_url(value).raw


def has_credentials(value: str) -> bool:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return _USERINFO_RE.search(value) is not None
    return bool(parts.username) or bool(parts.password)


def mask_rtsp_url(value: str) -> str:
    """Replace any user-info with ``***:***`` without altering anything else.

    A value that cannot be parsed collapses to a fixed placeholder rather than
    being echoed, so a malformed URL can never leak through an error path.
    """
    if not isinstance(value, str):
        return "rtsp://<unparsable-url>"
    text = value.strip()
    if not text:
        return "rtsp://<unparsable-url>"
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return _USERINFO_RE.sub(r"\g<scheme>" + f"{_MASK}:{_MASK}@", text) or (
            "rtsp://<unparsable-url>"
        )
    if not parts.scheme or not host:
        masked = _USERINFO_RE.sub(r"\g<scheme>" + f"{_MASK}:{_MASK}@", text)
        return masked if masked != text else "rtsp://<unparsable-url>"

    if ":" in host and not host.startswith("["):
        authority = f"[{host}]"
    else:
        authority = host
    if parts.port is not None:
        authority = f"{authority}:{parts.port}"
    if parts.username or parts.password:
        authority = f"{_MASK}:{_MASK}@{authority}"

    rebuilt = f"{parts.scheme}://{authority}{parts.path}"
    if parts.query:
        rebuilt = f"{rebuilt}?{parts.query}"
    return rebuilt


def _userinfo_variants(url: str) -> list[str]:
    """Every literal form of a URL's secret that could appear in free text."""
    variants: list[str] = []
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return variants
    for value in (parts.password, parts.username):
        if value:
            variants.append(value)
            quoted = quote(value, safe="")
            if quoted != value:
                variants.append(quoted)
    return variants


def scrub_text(text: str, known_urls: Iterable[str] = ()) -> str:
    """Remove credentials and known raw URLs from arbitrary free text.

    Three layers, applied in order:

    1. every supplied raw URL is replaced by its masked form;
    2. any bare username/password from those URLs is replaced by ``***``;
    3. any remaining ``scheme://userinfo@`` sequence is masked generically, so
       a URL this process has never seen still cannot carry a secret.
    """
    if not isinstance(text, str):
        return ""
    result = text
    secrets: list[str] = []
    for url in known_urls:
        if not url:
            continue
        raw = url.strip()
        if raw and raw in result:
            result = result.replace(raw, mask_rtsp_url(raw))
        secrets.extend(_userinfo_variants(raw))
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret and secret not in ("", _MASK):
            result = result.replace(secret, _MASK)
    result = _USERINFO_RE.sub(r"\g<scheme>" + f"{_MASK}:{_MASK}@", result)
    result = _CONTROL_RE.sub(" ", result)
    return result


def safe_gst_error(
    error: object,
    debug: str | None = None,
    known_urls: Iterable[str] = (),
) -> tuple[str, str]:
    """Turn a GLib/GStreamer error into a stable code plus a scrubbed message.

    The returned message is capped so an enormous native debug string cannot
    dominate a log line or an API error body.
    """
    domain = getattr(error, "domain", None)
    code = getattr(error, "code", None)
    parts: list[str] = []
    if domain:
        parts.append(str(domain))
    if code is not None:
        parts.append(str(code))
    error_code = "gst_" + ("_".join(parts) if parts else "error")
    error_code = re.sub(r"[^0-9a-zA-Z_]+", "_", error_code).strip("_").lower()[:60]

    message = getattr(error, "message", None) or str(error or "")
    combined = message if not debug else f"{message} | {debug}"
    scrubbed = scrub_text(combined, known_urls).strip()
    if len(scrubbed) > MAX_SAFE_ERROR_LENGTH:
        scrubbed = scrubbed[: MAX_SAFE_ERROR_LENGTH - 3] + "..."
    return error_code or "gst_error", scrubbed or "GStreamer reported a failure."
