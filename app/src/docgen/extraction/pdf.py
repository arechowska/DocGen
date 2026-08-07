from __future__ import annotations

from pathlib import Path

import pymupdf

from docgen.extraction.registry import ExtractionError, ExtractionResult, stable_block_id
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.models import Source


class PdfExtractor:
    def extract(self, source: Source, path: Path) -> ExtractionResult:
        blocks: list[NormalizedBlock] = []
        warnings: list[str] = []
        try:
            with pymupdf.open(path) as document:
                for page_number, page in enumerate(document, start=1):
                    page_blocks = [block for block in page.get_text("blocks") if block[4].strip()]
                    if not page_blocks:
                        warnings.append(f"Страница {page_number} не содержит извлекаемого текста")
                        continue
                    for block_number, block in enumerate(page_blocks, start=1):
                        text = block[4].strip()
                        locator = f"page:{page_number}/block:{block_number}"
                        blocks.append(
                            NormalizedBlock(
                                id=stable_block_id(source.id, BlockKind.TEXT, locator, text),
                                kind=BlockKind.TEXT,
                                text=text,
                                data={"bbox": list(block[:4])},
                                provenance=[Provenance(source_id=source.id, locator=locator)],
                                confidence=1.0,
                            )
                        )
                page_units = document.page_count
        except (OSError, pymupdf.FileDataError, pymupdf.FileNotFoundError) as error:
            raise ExtractionError("Не удалось прочитать PDF-файл") from error
        return ExtractionResult(blocks=blocks, page_units=page_units, warnings=warnings)
