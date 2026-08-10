from __future__ import annotations

from pathlib import Path
from typing import IO

from PIL import Image, UnidentifiedImageError

from docgen.extraction.registry import (
    ExtractionError,
    ExtractionResult,
    preflight_file_size,
    stable_block_id,
)
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.models import Source

_IMAGE_READ_ERROR = "Не удалось прочитать изображение"
_OVERSIZED_IMAGE_ERROR = "Изображение слишком большое"


def preflight_image(
    image_source: Path | IO[bytes],
    max_image_pixels: int,
    *,
    read_error_message: str = _IMAGE_READ_ERROR,
) -> tuple[int, int]:
    try:
        with Image.open(image_source) as image:
            width, height = image.size
            if width * height > max_image_pixels:
                raise ExtractionError(_OVERSIZED_IMAGE_ERROR)
            image.load()
    except ExtractionError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise ExtractionError(read_error_message) from error
    return width, height


class ImageExtractor:
    def __init__(
        self,
        *,
        max_file_bytes: int = 52_428_800,
        max_image_pixels: int = 40_000_000,
    ) -> None:
        self._max_file_bytes = max_file_bytes
        self._max_image_pixels = max_image_pixels

    def extract(self, source: Source, path: Path) -> ExtractionResult:
        preflight_file_size(
            path,
            self._max_file_bytes,
            read_error_message=_IMAGE_READ_ERROR,
        )
        width, height = preflight_image(path, self._max_image_pixels)

        block = NormalizedBlock(
            id=stable_block_id(source.id, BlockKind.IMAGE, "image:1", ""),
            kind=BlockKind.IMAGE,
            text="",
            data={"width": width, "height": height, "storage_path": str(path)},
            provenance=[Provenance(source_id=source.id, locator="image:1")],
            confidence=1.0,
        )
        return ExtractionResult(blocks=[block], page_units=1, warnings=[])
