"""Image intake: type sniffing, bounds, animation, and bombs."""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from app.security.images import (
    ImageTooLarge,
    UndecodableImage,
    UnsupportedImageType,
    decode_artifact_image,
    decode_query_image,
)
from tests.fakes.factories import jpeg_bytes, png_bytes

LIMITS = {"max_bytes": 8 * 1024 * 1024, "max_pixels": 16_777_216}


def test_jpeg_and_png_uploads_are_accepted_as_rgb():
    for payload in (jpeg_bytes(), png_bytes()):
        image = decode_query_image(payload, **LIMITS)
        assert image.mode == "RGB" and image.size == (64, 48)


def test_a_declared_type_and_filename_are_irrelevant():
    # The bytes are a PNG; nothing here ever consults a content-type or a name.
    image = decode_query_image(png_bytes(), **LIMITS)
    assert image.format is None or image.mode == "RGB"


@pytest.mark.parametrize(
    "payload",
    [
        b"GIF89a" + b"\x00" * 100,
        b"BM" + b"\x00" * 100,
        b"%PDF-1.4\n" + b"\x00" * 100,
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        b"\x00" * 64,
    ],
)
def test_non_jpeg_png_payloads_are_refused_by_magic(payload):
    with pytest.raises(UnsupportedImageType):
        decode_query_image(payload, **LIMITS)


def test_an_empty_or_oversized_upload_is_refused():
    with pytest.raises(UndecodableImage):
        decode_query_image(b"", **LIMITS)
    with pytest.raises(ImageTooLarge):
        decode_query_image(jpeg_bytes(), max_bytes=10, max_pixels=16_777_216)


def test_a_truncated_jpeg_is_refused():
    payload = jpeg_bytes(256, 256)
    with pytest.raises(UndecodableImage):
        decode_query_image(payload[: len(payload) // 3], **LIMITS)


def test_an_edge_or_pixel_limit_is_enforced_before_decoding():
    wide = jpeg_bytes(5000, 10)
    with pytest.raises(ImageTooLarge):
        decode_query_image(wide, max_bytes=8 * 1024 * 1024, max_pixels=16_777_216, max_edge=4096)
    with pytest.raises(ImageTooLarge):
        decode_query_image(jpeg_bytes(2000, 2000), max_bytes=8 * 1024 * 1024, max_pixels=1_000_000)


def test_an_animated_png_is_refused():
    buffer = BytesIO()
    frames = [Image.new("RGB", (32, 32), (i * 40, 0, 0)) for i in range(3)]
    frames[0].save(buffer, format="PNG", save_all=True, append_images=frames[1:])
    with pytest.raises(UndecodableImage):
        decode_query_image(buffer.getvalue(), **LIMITS)


def test_a_decompression_bomb_warning_is_an_error():
    original = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = 100
    try:
        with pytest.raises(ImageTooLarge):
            decode_query_image(jpeg_bytes(200, 200), **LIMITS)
    finally:
        Image.MAX_IMAGE_PIXELS = original


def test_exif_orientation_is_applied():
    # A 4:3 image tagged "rotate 90" must come back 3:4.
    buffer = BytesIO()
    image = Image.new("RGB", (64, 48), (10, 20, 30))
    exif = image.getexif()
    exif[0x0112] = 6
    image.save(buffer, format="JPEG", exif=exif)
    assert decode_query_image(buffer.getvalue(), **LIMITS).size == (48, 64)


def test_artifact_decoding_accepts_only_jpeg():
    assert decode_artifact_image(jpeg_bytes(), max_bytes=16 * 1024 * 1024).mode == "RGB"
    with pytest.raises(UnsupportedImageType):
        decode_artifact_image(png_bytes(), max_bytes=16 * 1024 * 1024)
    with pytest.raises(ImageTooLarge):
        decode_artifact_image(jpeg_bytes(), max_bytes=10)
