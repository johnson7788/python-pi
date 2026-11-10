"""Image attachments: protocol normalisation and local file loading.

The browser sends inline base64; the CLI reads files itself and sends the same
normalised blocks.  These tests pin the accepted shape, the rejections that keep
a remote caller from reading arbitrary paths, and the error messages a user sees
when an image is malformed.
"""

from __future__ import annotations

import base64

import pytest
from pi_ai import ImageContent

from pi_coding_agent.images import (
    MAX_IMAGES,
    ImageInputError,
    image_from_base64,
    image_from_file,
    normalize_images,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nnot-a-real-image-but-bytes"
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


def test_none_and_empty_mean_no_images():
    assert normalize_images(None) == []
    assert normalize_images([]) == []


def test_inline_base64_with_explicit_mime():
    assert normalize_images([{"type": "image", "mimeType": "image/png", "data": PNG_B64}]) == [
        ImageContent(data=PNG_B64, mime_type="image/png")
    ]


def test_data_url_supplies_missing_mime():
    image = normalize_images(
        [{"type": "image", "data": f"data:image/jpeg;base64,{PNG_B64}"}]
    )[0]
    assert image.mime_type == "image/jpeg"
    assert base64.b64decode(image.data) == PNG_BYTES


def test_media_type_alias_is_accepted():
    image = normalize_images([{"mediaType": "image/webp", "data": PNG_B64}])[0]
    assert image.mime_type == "image/webp"


def test_base64_with_whitespace_is_normalised():
    wrapped = "\n".join([PNG_B64[:8], PNG_B64[8:]])
    image = image_from_base64(wrapped, "image/png")
    assert image.data == PNG_B64


def test_missing_mime_is_rejected():
    with pytest.raises(ImageInputError, match="mimeType"):
        normalize_images([{"data": PNG_B64}])


def test_unsupported_mime_is_rejected():
    with pytest.raises(ImageInputError, match="unsupported image type"):
        normalize_images([{"mimeType": "image/tiff", "data": PNG_B64}])


def test_invalid_base64_is_rejected():
    with pytest.raises(ImageInputError, match="not valid base64"):
        normalize_images([{"mimeType": "image/png", "data": "not base64!!"}])


def test_paths_and_urls_are_rejected_on_the_protocol():
    for payload in ({"path": "shot.png"}, {"url": "https://example.com/shot.png"}):
        with pytest.raises(ImageInputError, match="only inline base64"):
            normalize_images([payload])


def test_shape_errors_name_the_item_index():
    with pytest.raises(ImageInputError, match=r"images\[1\]"):
        normalize_images(
            [{"mimeType": "image/png", "data": PNG_B64}, {"mimeType": "image/png"}]
        )


def test_too_many_images_is_rejected():
    item = {"mimeType": "image/png", "data": PNG_B64}
    with pytest.raises(ImageInputError, match="at most"):
        normalize_images([item] * (MAX_IMAGES + 1))


def test_image_content_passthrough_is_revalidated():
    image = ImageContent(data=PNG_B64, mime_type="image/png")
    assert normalize_images([image]) == [image]
    with pytest.raises(ImageInputError, match="unsupported image type"):
        normalize_images([ImageContent(data=PNG_B64, mime_type="image/tiff")])


def test_image_from_file_resolves_against_cwd(tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(PNG_BYTES)
    image = image_from_file("shot.png", cwd=str(tmp_path))
    assert image.mime_type == "image/png"
    assert base64.b64decode(image.data) == PNG_BYTES


def test_image_from_file_rejects_missing_and_unknown_types(tmp_path):
    with pytest.raises(ImageInputError, match="file not found"):
        image_from_file("missing.png", cwd=str(tmp_path))
    unknown = tmp_path / "notes.txt"
    unknown.write_text("hello")
    with pytest.raises(ImageInputError, match="unsupported image extension"):
        image_from_file("notes.txt", cwd=str(tmp_path))
