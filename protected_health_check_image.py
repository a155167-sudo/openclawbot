from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import re
import stat
import warnings

from PIL import Image

IMAGE_REF_PATTERN = re.compile(r"nutrition-image:([0-9a-f]{32})\.(jpg|png|webp)")
MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_SOURCE_PIXELS = 25_000_000
MAX_PREVIEW_BYTES = 950 * 1024


class ImageUnavailable(Exception):
    """The private source cannot safely be projected."""


@dataclass(frozen=True)
class ImagePreview:
    data: bytes
    media_type: str = "image/jpeg"


def read_bounded_preview(image_root: str | os.PathLike[str], image_ref: object) -> ImagePreview:
    match = IMAGE_REF_PATTERN.fullmatch(str(image_ref or ""))
    if match is None:
        raise ImageUnavailable
    filename = f"{match.group(1)}.{match.group(2)}"
    root_fd = file_fd = None
    try:
        root_fd = os.open(
            Path(image_root),
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        file_fd = os.open(
            filename,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=root_fd,
        )
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_SOURCE_BYTES:
            raise ImageUnavailable
        with os.fdopen(file_fd, "rb", closefd=True) as source:
            file_fd = None
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(source) as image:
                    expected_format = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}[match.group(2)]
                    if image.format != expected_format:
                        raise ImageUnavailable
                    if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_SOURCE_PIXELS:
                        raise ImageUnavailable
                    image.load()
                    image.thumbnail((1200, 1200))
                    if image.mode != "RGB":
                        background = Image.new("RGB", image.size, "white")
                        if "A" in image.getbands():
                            background.paste(image, mask=image.getchannel("A"))
                        else:
                            background.paste(image)
                        image = background
                    for quality in (85, 75, 65, 55, 45, 35):
                        output = BytesIO()
                        image.save(output, "JPEG", quality=quality, optimize=True)
                        data = output.getvalue()
                        if len(data) <= MAX_PREVIEW_BYTES:
                            return ImagePreview(data)
        raise ImageUnavailable
    except (ImageUnavailable, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageUnavailable from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if root_fd is not None:
            os.close(root_fd)
