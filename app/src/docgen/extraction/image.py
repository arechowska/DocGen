from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError

from docgen.extraction.registry import (
    ExtractionError,
    ExtractionResult,
    preflight_file_size,
    stable_block_id,
)
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.models import Source


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
            read_error_message="Не удалось прочитать изображение",
        )
        try:
            with Image.open(path) as image:
                width, height = image.size
                if width * height > self._max_image_pixels:
                    raise ExtractionError("Изображение слишком большое")
                image.load()
        except ExtractionError:
            raise
        except (OSError, UnidentifiedImageError) as error:
            raise ExtractionError("Не удалось прочитать изображение") from error

        block = NormalizedBlock(
            id=stable_block_id(source.id, BlockKind.IMAGE, "image:1", ""),
            kind=BlockKind.IMAGE,
            text="",
            data={"width": width, "height": height, "storage_path": str(path)},
            provenance=[Provenance(source_id=source.id, locator="image:1")],
            confidence=1.0,
        )
        return ExtractionResult(blocks=[block], page_units=1, warnings=[])
