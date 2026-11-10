"""Image attachments from the command protocol and the CLI.

The command protocol (and therefore the browser UI) transports images as JSON,
because a WebSocket frame cannot carry a Python object.  This module is the one
place that turns those payloads -- and image files named on the command line --
into :class:`pi_ai.ImageContent` blocks, so every entry point validates images
the same way.

Accepted protocol shape (``images`` on ``prompt`` / ``steer`` / ``follow_up``)::

    [{"type": "image", "mimeType": "image/png", "data": "<base64>"}]

``mimeType`` may be omitted when ``data`` is a ``data:image/png;base64,...``
URL.  Only inline base64 is accepted: a caller must not be able to make the
server read arbitrary local paths or fetch arbitrary URLs.  Local files are
read by the CLI itself with :func:`image_from_file`.

Validation is deliberately strict and the message always names the offending
item index: a silently dropped image turns into "the model cannot see my
picture" with no clue why.
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import Any

from pi_ai import ImageContent

__all__ = [
    "ImageInputError",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGES",
    "SUPPORTED_MIME_TYPES",
    "image_from_base64",
    "image_from_file",
    "normalize_images",
]

#: Hard cap per request.  More images than this is more likely a bug than a use case.
MAX_IMAGES = 8

#: Decoded bytes per image.  The browser UI downscales before sending, so this is
#: a backstop against a client that skips that step, not the expected size.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

SUPPORTED_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
    }
)

_EXTENSION_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


class ImageInputError(ValueError):
    """An image attachment could not be turned into ``ImageContent``."""


def _normalise_mime(value: str | None) -> str | None:
    mime = (value or "").strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if not mime:
        return None
    if mime not in SUPPORTED_MIME_TYPES:
        supported = ", ".join(sorted(SUPPORTED_MIME_TYPES))
        raise ImageInputError(f"unsupported image type {mime!r} (supported: {supported})")
    return mime


def _decode_payload(payload: str, where: str) -> tuple[bytes, str | None]:
    """Decode plain base64 or a ``data:`` URL into ``(bytes, embedded mime)``."""
    text = payload.strip()
    embedded_mime: str | None = None
    if text.lower().startswith("data:"):
        header, separator, body = text.partition(",")
        if not separator:
            raise ImageInputError(f"{where}: malformed data URL (missing ',')")
        if ";base64" not in header.lower():
            raise ImageInputError(f"{where}: data URL must be base64 encoded")
        embedded_mime = header[5:].split(";", 1)[0].strip() or None
        text = body
    # Canvas output and hand-written base64 often wrap; whitespace is not data.
    text = "".join(text.split())
    if not text:
        raise ImageInputError(f"{where}: image data is empty")
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ImageInputError(f"{where}: image data is not valid base64 ({error})") from None
    if not raw:
        raise ImageInputError(f"{where}: image data decodes to zero bytes")
    return raw, embedded_mime


def _build(raw: bytes, mime_type: str | None, where: str) -> ImageContent:
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageInputError(
            f"{where}: image is {len(raw)} bytes; the limit is {MAX_IMAGE_BYTES} bytes"
        )
    mime = _normalise_mime(mime_type)
    if mime is None:
        raise ImageInputError(f"{where}: 'mimeType' is required for inline base64 images")
    return ImageContent(data=base64.b64encode(raw).decode("ascii"), mime_type=mime)


def image_from_base64(
    data: str, mime_type: str | None = None, *, where: str = "image"
) -> ImageContent:
    """Build one ``ImageContent`` from base64 data or a base64 data URL."""
    raw, embedded_mime = _decode_payload(data, where)
    return _build(raw, _normalise_mime(mime_type) or embedded_mime, where)


def image_from_file(path: str, *, cwd: str | None = None, where: str = "image") -> ImageContent:
    """Read one local image file (CLI ``--image``) into ``ImageContent``."""
    resolved = os.path.expanduser(path)
    if not os.path.isabs(resolved):
        resolved = os.path.join(cwd or os.getcwd(), resolved)
    resolved = os.path.abspath(resolved)
    if not os.path.exists(resolved):
        raise ImageInputError(f"{where}: file not found: {resolved}")
    if os.path.isdir(resolved):
        raise ImageInputError(f"{where}: path is a directory, not a file: {resolved}")
    mime = _EXTENSION_MIME_TYPES.get(os.path.splitext(resolved)[1].lower())
    if mime is None:
        supported = ", ".join(sorted(_EXTENSION_MIME_TYPES))
        raise ImageInputError(f"{where}: unsupported image extension for {resolved} (supported: {supported})")
    try:
        size = os.path.getsize(resolved)
        if size > MAX_IMAGE_BYTES:
            raise ImageInputError(
                f"{where}: image is {size} bytes; the limit is {MAX_IMAGE_BYTES} bytes"
            )
        with open(resolved, "rb") as handle:
            raw = handle.read(MAX_IMAGE_BYTES + 1)
    except OSError as error:
        raise ImageInputError(f"{where}: could not read {resolved} ({error})") from None
    return _build(raw, mime, where)


def normalize_images(raw: Any) -> list[ImageContent]:
    """Validate the protocol ``images`` array and return ``ImageContent`` blocks.

    ``None`` and an empty array mean "no images".  Anything else must follow the
    inline-base64 shape described in the module docstring; local paths and remote
    URLs are rejected on purpose.
    """
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise ImageInputError("'images' must be an array of image attachments")
    if len(raw) > MAX_IMAGES:
        raise ImageInputError(f"'images' carries {len(raw)} items; at most {MAX_IMAGES} are allowed")

    images: list[ImageContent] = []
    for index, item in enumerate(raw):
        where = f"images[{index}]"
        if isinstance(item, ImageContent):
            try:
                raw_bytes = base64.b64decode(item.data, validate=True)
            except (binascii.Error, ValueError) as error:
                raise ImageInputError(f"{where}: image data is not valid base64 ({error})") from None
            images.append(_build(raw_bytes, item.mime_type, where))
            continue
        if not isinstance(item, dict):
            raise ImageInputError(f"{where}: expected an object with 'data' and 'mimeType'")
        if "path" in item or "url" in item:
            raise ImageInputError(
                f"{where}: only inline base64 images are accepted over the protocol; "
                "send {'type': 'image', 'mimeType': ..., 'data': ...}"
            )
        data = item.get("data")
        if not isinstance(data, str) or not data:
            raise ImageInputError(f"{where}: 'data' must be a non-empty base64 string")
        mime = (
            item.get("mimeType")
            or item.get("mediaType")
            or item.get("mime_type")
            or item.get("media_type")
        )
        if mime is None and isinstance(item.get("type"), str) and item["type"].startswith("image/"):
            mime = item["type"]
        images.append(image_from_base64(data, mime, where=where))
    return images
