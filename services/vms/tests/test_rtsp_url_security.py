"""RTSP URL validation and credential masking."""

from __future__ import annotations

import pytest

from app.security.rtsp_url import (
    MAX_URL_LENGTH,
    InvalidRtspUrl,
    sanitize_rtsp_url,
    sanitize_text,
    secret_of,
    strip_control_chars,
    url_has_credentials,
    validate_rtsp_url,
)

CREDENTIALED = "rtsp://admin:hunter2@10.0.0.9:554/Streaming/Channels/101?tcp=1"


def test_accepts_plain_url():
    parsed = validate_rtsp_url("rtsp://host.docker.internal:8554/simulator/lobby")
    assert parsed.scheme == "rtsp"
    assert parsed.host == "host.docker.internal"
    assert parsed.port == 8554
    assert parsed.has_credentials is False


def test_accepts_rtsps_and_credentials_and_query():
    parsed = validate_rtsp_url("rtsps://admin:pw@cam.local/live?profile=main")
    assert parsed.scheme == "rtsps"
    assert parsed.username == "admin"
    assert parsed.password == "pw"
    assert parsed.port is None
    assert parsed.has_credentials is True


def test_accepts_url_without_port_or_path():
    assert validate_rtsp_url("rtsp://10.0.0.9").host == "10.0.0.9"


def test_accepts_ipv6_literal():
    parsed = validate_rtsp_url("rtsp://[2001:db8::1]:8554/live")
    assert parsed.host == "2001:db8::1"
    assert parsed.port == 8554


def test_surrounding_whitespace_is_trimmed():
    assert validate_rtsp_url("  rtsp://cam/live  ").url == "rtsp://cam/live"


def test_offline_host_is_still_syntactically_valid():
    # The PRD wants an unreachable-but-valid camera accepted, then shown OFFLINE.
    assert validate_rtsp_url("rtsp://192.0.2.1:554/nothing-here").host == "192.0.2.1"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "http://cam/live",
        "https://cam/live",
        "file:///etc/passwd",
        "cam/live",
        "rtsp://",
        "rtsp:///no-host",
        "rtsp://cam/live#fragment",
        "rtsp://cam:0/live",
        "rtsp://cam:99999/live",
        "rtsp://cam/li ve",
        "rtsp://cam/live\nGET /x",
        "rtsp://cam/live\x00",
        "rtsp://[::1/live",
    ],
)
def test_rejects_invalid_urls(value):
    with pytest.raises(InvalidRtspUrl):
        validate_rtsp_url(value)


def test_rejects_over_long_url():
    with pytest.raises(InvalidRtspUrl):
        validate_rtsp_url("rtsp://cam/" + "a" * MAX_URL_LENGTH)


def test_non_string_rejected():
    with pytest.raises(InvalidRtspUrl):
        validate_rtsp_url(None)  # type: ignore[arg-type]


def test_sanitize_masks_password_and_keeps_everything_else():
    masked = sanitize_rtsp_url(CREDENTIALED)
    assert masked == "rtsp://admin:***@10.0.0.9:554/Streaming/Channels/101?tcp=1"
    assert "hunter2" not in masked


def test_sanitize_keeps_plain_url_intact():
    url = "rtsp://host.docker.internal:8554/simulator/lobby"
    assert sanitize_rtsp_url(url) == url


def test_sanitize_masks_password_with_special_characters():
    masked = sanitize_rtsp_url("rtsp://user:p%40ss%3Aword@cam:554/live")
    assert "p%40ss%3Aword" not in masked
    assert masked == "rtsp://user:***@cam:554/live"


def test_sanitize_handles_username_only():
    assert sanitize_rtsp_url("rtsp://admin@cam/live") == "rtsp://admin@cam/live"


def test_sanitize_preserves_ipv6_brackets():
    assert sanitize_rtsp_url("rtsp://u:p@[::1]:8554/x") == "rtsp://u:***@[::1]:8554/x"


def test_sanitize_falls_back_for_garbage():
    assert sanitize_rtsp_url("not a url") == "rtsp://<unparsable-url>"
    assert sanitize_rtsp_url("") == ""


def test_url_has_credentials():
    assert url_has_credentials(CREDENTIALED) is True
    assert url_has_credentials("rtsp://cam/live") is False
    assert url_has_credentials("garbage") is False


def test_secret_of():
    assert secret_of(CREDENTIALED) == "hunter2"
    assert secret_of("rtsp://cam/live") is None


def test_sanitize_text_scrubs_url_and_password():
    text = f"cannot connect to {CREDENTIALED}: connection refused (pw hunter2)"
    cleaned = sanitize_text(text, CREDENTIALED)
    assert "hunter2" not in cleaned
    assert "rtsp://admin:***@10.0.0.9:554" in cleaned
    assert "connection refused" in cleaned


def test_sanitize_text_removes_control_characters():
    assert "\n" not in sanitize_text("line one\nline two")
    assert sanitize_text("a\x00b") == "ab"


def test_sanitize_text_tolerates_missing_urls():
    assert sanitize_text("plain failure", "", None or "") == "plain failure"


def test_strip_control_chars():
    assert strip_control_chars("a\r\nb\tc") == "abc"
