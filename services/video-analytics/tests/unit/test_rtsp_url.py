"""RTSP URL validation, masking, and scrubbing (PLAN sections 20.4, 23.2)."""

from __future__ import annotations

import pytest

from app.security.rtsp_url import (
    InvalidRtspUrl,
    has_credentials,
    mask_rtsp_url,
    parse_rtsp_url,
    safe_gst_error,
    scrub_text,
    validate_rtsp_url,
)

pytestmark = pytest.mark.unit

SECRET = "hunter2"
CREDENTIALED = f"rtsp://admin:{SECRET}@10.0.0.9:554/Streaming/Channels/101"


@pytest.mark.parametrize(
    "url",
    [
        "rtsp://camera/live",
        "rtsp://10.0.0.9/live",
        "rtsp://10.0.0.9:554/live",
        "rtsp://[2001:db8::1]:554/live",
        "rtsp://[::1]/live",
        "rtsp://host.docker.internal:8555/vms_cam_0123abcd",
        CREDENTIALED,
        "rtsp://user@camera/live",
        "rtsp://camera/live?channel=1&subtype=0",
    ],
)
def test_valid_urls_are_accepted(url: str):
    assert validate_rtsp_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "http://camera/live",
        "rtsps://camera/live",
        "rtsp://",
        "rtsp:///live",
        "rtsp://camera:0/live",
        "rtsp://camera:70000/live",
        "rtsp://camera:abc/live",
        "rtsp://camera/live#fragment",
        "rtsp://camera/live\nX-Injected: 1",
        "rtsp://camera/li ve",
        "rtsp://camera/live\x00",
        "rtsp://" + "a" * 3000,
    ],
)
def test_invalid_urls_are_rejected(url: str):
    with pytest.raises(InvalidRtspUrl):
        validate_rtsp_url(url)


def test_rejection_message_never_contains_the_input():
    try:
        validate_rtsp_url(f"http://admin:{SECRET}@camera/live")
    except InvalidRtspUrl as exc:
        assert SECRET not in str(exc)
        assert "camera" not in str(exc)
    else:  # pragma: no cover
        pytest.fail("expected InvalidRtspUrl")


# -- masking ----------------------------------------------------------------


def test_masking_removes_both_credentials():
    masked = mask_rtsp_url(CREDENTIALED)
    assert masked == "rtsp://***:***@10.0.0.9:554/Streaming/Channels/101"
    assert SECRET not in masked
    assert "admin" not in masked


def test_masking_removes_a_username_only_credential():
    masked = mask_rtsp_url("rtsp://admin@10.0.0.9/live")
    assert masked == "rtsp://***:***@10.0.0.9/live"


def test_masking_preserves_a_url_without_credentials():
    url = "rtsp://host.docker.internal:8555/vms_cam_0123abcd"
    assert mask_rtsp_url(url) == url


def test_masking_preserves_the_query_string():
    assert mask_rtsp_url("rtsp://cam/live?a=1") == "rtsp://cam/live?a=1"


def test_masking_keeps_ipv6_brackets():
    assert mask_rtsp_url("rtsp://[2001:db8::1]:554/live") == "rtsp://[2001:db8::1]:554/live"


@pytest.mark.parametrize("value", ["", "   ", "not a url", None, 12345])
def test_unparsable_values_collapse_to_a_placeholder(value):
    masked = mask_rtsp_url(value)  # type: ignore[arg-type]
    assert masked.startswith("rtsp://")
    assert "not a url" not in masked


def test_percent_encoded_password_is_masked():
    masked = mask_rtsp_url("rtsp://admin:p%40ss@cam/live")
    assert "p%40ss" not in masked and "p@ss" not in masked


def test_has_credentials():
    assert has_credentials(CREDENTIALED) is True
    assert has_credentials("rtsp://cam/live") is False


def test_parse_returns_host_port_and_flag():
    parsed = parse_rtsp_url(CREDENTIALED)
    assert parsed.host == "10.0.0.9"
    assert parsed.port == 554
    assert parsed.has_credentials is True
    assert SECRET not in parsed.masked


# -- scrubbing --------------------------------------------------------------


def test_scrubber_removes_a_known_url_from_nested_error_text():
    text = f"gstrtspsrc: could not open {CREDENTIALED} (retrying)"
    scrubbed = scrub_text(text, [CREDENTIALED])
    assert SECRET not in scrubbed
    assert CREDENTIALED not in scrubbed
    assert "could not open" in scrubbed


def test_scrubber_removes_a_bare_password_it_was_told_about():
    scrubbed = scrub_text(f"auth failed for {SECRET}", [CREDENTIALED])
    assert SECRET not in scrubbed


def test_scrubber_masks_an_unknown_credentialed_url_generically():
    scrubbed = scrub_text("failed: rtsp://root:toor@other/live")
    assert "toor" not in scrubbed
    assert "***:***@" in scrubbed


def test_scrubber_strips_control_characters():
    assert "\n" not in scrub_text("a\nb\rc\x00d")


def test_scrubber_tolerates_non_strings():
    assert scrub_text(None) == ""  # type: ignore[arg-type]


# -- GStreamer errors -------------------------------------------------------


class _FakeGError:
    domain = "gst-resource-error-quark"
    code = 9
    message = "Could not open resource for reading."


def test_safe_gst_error_returns_a_stable_code_and_scrubbed_message():
    code, message = safe_gst_error(
        _FakeGError(), f"debug: {CREDENTIALED}", known_urls=[CREDENTIALED]
    )
    assert code == "gst_gst_resource_error_quark_9"
    assert SECRET not in message
    assert "Could not open resource" in message


def test_safe_gst_error_caps_length():
    _code, message = safe_gst_error(_FakeGError(), "x" * 5000)
    assert len(message) <= 300


def test_safe_gst_error_handles_a_missing_error():
    code, message = safe_gst_error(None, None)
    assert code and message
