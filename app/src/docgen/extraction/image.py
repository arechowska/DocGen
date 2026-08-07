from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError

from docgen.extraction.registry import ExtractionError, ExtractionResult, stable_block_id
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.models import Source


class ImageExtractor:
    def extract(self, source: Source, path: Path) -> ExtractionResult:
        try:
            with Image.open(path) as image:
                image.load()
                width, height = image.size
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
