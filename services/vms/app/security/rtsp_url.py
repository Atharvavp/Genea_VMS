"""RTSP URL validation and credential-safe rendering.

Real cameras hand out URLs like ``rtsp://admin:hunter2@10.0.0.9:554/Streaming``.
The full URL is needed by MediaMTX and is therefore stored, but it must never
reach an API response, an error message or a log line. Everything that renders
a URL for a human goes through :func:`sanitize_rtsp_url`; everything that
renders free text goes through :func:`sanitize_text`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = ("rtsp", "rtsps")
MAX_URL_LENGTH = 2048
MASK = "***"

# C0 controls, DEL, and the C1 range. A camera URL never legitimately contains
# these; a log-injection payload does.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WHITESPACE = re.compile(r"\s")

# `scheme://user:password@host` anywhere in free text. Used as a backstop by
# `sanitize_text` for messages whose originating URL is not known to the caller
# - so a password cannot survive simply because nobody passed the right URL in.
_EMBEDDED_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/@:]+):[^\s/@]+@")


class InvalidRtspUrl(ValueError):
    """The supplied string is not a usable RTSP URL."""


@dataclass(frozen=True)
class ParsedRtspUrl:
    """A validated RTSP URL, split into the parts the app cares about."""

    url: str
    scheme: str
    host: str
    port: int | None
    username: str | None
    password: str | None

    @property
    def has_credentials(self) -> bool:
        return bool(self.username or self.password)


def strip_control_chars(value: str) -> str:
    """Remove characters that would corrupt a log line or an HTTP header."""
    return _CONTROL_CHARS.sub("", value)


def validate_rtsp_url(value: str) -> ParsedRtspUrl:
    """Validate an RTSP URL, returning its parts.

    Syntax only: a syntactically valid URL is accepted even when the camera is
    unreachable, which is the behaviour the PRD asks for (the camera then shows
    up as OFFLINE rather than being rejected).
    """
    if not isinstance(value, str):
        raise InvalidRtspUrl("RTSP URL must be a string.")

    candidate = value.strip()
    if not candidate:
        raise InvalidRtspUrl("RTSP URL must not be empty.")
    if len(candidate) > MAX_URL_LENGTH:
        raise InvalidRtspUrl(f"RTSP URL must be at most {MAX_URL_LENGTH} characters.")
    if _CONTROL_CHARS.search(candidate):
        raise InvalidRtspUrl("RTSP URL must not contain control characters.")
    if _WHITESPACE.search(candidate):
        raise InvalidRtspUrl("RTSP URL must not contain whitespace.")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:  # malformed IPv6 literal, bad port, ...
        raise InvalidRtspUrl("RTSP URL could not be parsed.") from exc

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidRtspUrl(
            "RTSP URL must start with rtsp:// or rtsps:// "
            f"(got '{strip_control_chars(parts.scheme) or '?'}://')."
        )
    # A fragment is never sent on the wire, so a URL carrying one would silently
    # mean something different to MediaMTX than it does to the user.
    if parts.fragment:
        raise InvalidRtspUrl("RTSP URL must not contain a fragment ('#').")

    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise InvalidRtspUrl("RTSP URL has an invalid host or port.") from exc

    if not host:
        raise InvalidRtspUrl("RTSP URL must include a host.")
    if port is not None and not (0 < port < 65536):
        raise InvalidRtspUrl("RTSP URL port must be between 1 and 65535.")

    return ParsedRtspUrl(
        url=candidate,
        scheme=parts.scheme.lower(),
        host=host,
        port=port,
        username=parts.username or None,
        password=parts.password or None,
    )


def sanitize_rtsp_url(value: str) -> str:
    """Return the URL with any password replaced by ``***``.

    Falls back to a scheme-only placeholder if the URL cannot be parsed, so a
    malformed value can never leak verbatim through an error path.
    """
    if not value:
        return ""
    try:
        parts = urlsplit(strip_control_chars(value).strip())
        host = parts.hostname
    except ValueError:
        return "rtsp://<unparsable-url>"
    if not host:
        return "rtsp://<unparsable-url>"

    netloc = host
    if ":" in host and not host.startswith("["):  # IPv6 literal
        netloc = f"[{host}]"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    if parts.username or parts.password:
        userinfo = parts.username or ""
        if parts.password:
            userinfo = f"{userinfo}:{MASK}"
        netloc = f"{userinfo}@{netloc}"

    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def url_has_credentials(value: str) -> bool:
    try:
        parts = urlsplit(strip_control_chars(value).strip())
        return bool(parts.username or parts.password)
    except ValueError:
        return False


def secret_of(value: str) -> str | None:
    """The password embedded in a URL, if any."""
    try:
        return urlsplit(strip_control_chars(value).strip()).password or None
    except ValueError:
        return None


def sanitize_text(text: str, *urls: str) -> str:
    """Scrub free text before it is logged or returned.

    MediaMTX quotes the source URL back in some error messages, so any password
    from the URLs involved is replaced, and the credentialed URLs themselves are
    swapped for their masked form.

    Callers that know which URLs are involved should pass them. Callers that do
    not - the playback client, for one, which deals in path names and never sees
    a source URL - still get the backstop below, which masks the password of any
    credentialed URL embedded in the text.
    """
    cleaned = strip_control_chars(str(text))
    for url in urls:
        if not url:
            continue
        stripped = url.strip()
        if stripped:
            cleaned = cleaned.replace(stripped, sanitize_rtsp_url(stripped))
        secret = secret_of(url)
        if secret:
            cleaned = cleaned.replace(secret, MASK)
    return _EMBEDDED_CREDENTIALS.sub(rf"\1:{MASK}@", cleaned)
