"""Every image byte entering this process passes through here.

Two sources exist and both are hostile until proven otherwise: an operator
upload and a Component 4 artifact. The rules are the same for both - a declared
type is never trusted, the filename and extension are ignored entirely, bytes
and decoded pixels are bounded before any allocation the caller cannot afford,
animated or multi-frame input is refused, and Pillow's decompression-bomb
warning is an error rather than a warning.

Decoded images are returned to the caller and released as soon as inference is
done; nothing here writes to disk or keeps a reference.
"""

from __future__ import annotations

import warnings
from io import BytesIO
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

__all__ = [
    "ImageRejected", "ImageTooLarge", "UnsupportedImageType", "UndecodableImage",
    "decode_query_image", "decode_artifact_image", "MAX_EDGE_DEFAULT",
]

_JPEG_MAGIC: Final[bytes] = b"\xff\xd8\xff"
_PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"
MAX_EDGE_DEFAULT: Final[int] = 4096
_ALLOWED_FORMATS: Final[frozenset[str]] = frozenset({"JPEG", "PNG"})


class ImageRejected(ValueError):
    """Base class. ``code`` maps directly to the public error code."""

    code = "invalid_image"


class ImageTooLarge(ImageRejected):
    code = "image_too_large"


class UnsupportedImageType(ImageRejected):
    code = "unsupported_image_type"


class UndecodableImage(ImageRejected):
    code = "invalid_image"


def _sniff(payload: bytes) -> str:
    """Decide the type from the bytes. A declared type is never consulted."""
    if payload.startswith(_JPEG_MAGIC):
        return "JPEG"
    if payload.startswith(_PNG_MAGIC):
        return "PNG"
    raise UnsupportedImageType("only JPEG and PNG images are accepted")


def _open(payload: bytes, *, expected: str, max_pixels: int, max_edge: int) -> Image.Image:
    with warnings.catch_warnings():
        # A decompression-bomb warning means the header claims more pixels than
        # Pillow is willing to decode by default. Treat it as a rejection.
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            image = Image.open(BytesIO(payload))
            fmt = image.format
            if fmt not in _ALLOWED_FORMATS or fmt != expected:
                image.close()
                raise UnsupportedImageType("image content is not JPEG or PNG")
            if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
                image.close()
                raise UndecodableImage("animated or multi-frame images are not accepted")
            width, height = image.size
            if width <= 0 or height <= 0:
                image.close()
                raise UndecodableImage("image has no pixels")
            if max(width, height) > max_edge:
                image.close()
                raise ImageTooLarge(f"image edge exceeds {max_edge} pixels")
            if width * height > max_pixels:
                image.close()
                raise ImageTooLarge(f"image exceeds {max_pixels} pixels")
            image.load()
        except ImageRejected:
            raise
        except Image.DecompressionBombWarning as exc:
            raise ImageTooLarge("image declares an implausible pixel count") from exc
        except Image.DecompressionBombError as exc:
            raise ImageTooLarge("image declares an implausible pixel count") from exc
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
            raise UndecodableImage("image could not be decoded") from exc
    return image


def decode_query_image(
    payload: bytes,
    *,
    max_bytes: int,
    max_pixels: int,
    max_edge: int = MAX_EDGE_DEFAULT,
) -> Image.Image:
    """Decode an uploaded query image into an RGB PIL image, or reject it."""
    if not isinstance(payload, (bytes, bytearray)):
        raise UndecodableImage("upload is not bytes")
    if not payload:
        raise UndecodableImage("upload is empty")
    if len(payload) > max_bytes:
        raise ImageTooLarge(f"upload exceeds {max_bytes} bytes")
    expected = _sniff(bytes(payload))
    image = _open(bytes(payload), expected=expected, max_pixels=max_pixels, max_edge=max_edge)
    try:
        # EXIF orientation is applied so a phone photo is embedded the way a
        # human sees it, not the way the sensor stored it.
        oriented = ImageOps.exif_transpose(image) or image
        return oriented.convert("RGB")
    finally:
        if image is not None:
            image.close()


def decode_artifact_image(
    payload: bytes, *, max_bytes: int, max_pixels: int = 64_000_000
) -> Image.Image:
    """Decode a Component 4 crop or frame. JPEG only, by contract."""
    if len(payload) > max_bytes:
        raise ImageTooLarge("artifact exceeds the byte cap")
    if not payload.startswith(_JPEG_MAGIC):
        raise UnsupportedImageType("artifact is not a JPEG")
    image = _open(payload, expected="JPEG", max_pixels=max_pixels, max_edge=20_000)
    try:
        return image.convert("RGB")
    finally:
        image.close()
