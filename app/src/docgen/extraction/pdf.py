from __future__ import annotations

from pathlib import Path

import pymupdf

from docgen.extraction.registry import ExtractionResult
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.models import Source


class PdfExtractor:
    def extract(self, source: Source, path: Path) -> ExtractionResult:
        blocks: list[NormalizedBlock] = []
        warnings: list[str] = []
        with pymupdf.open(path) as document:
            for page_number, page in enumerate(document, start=1):
                page_blocks = [block for block in page.get_text("blocks") if block[4].strip()]
                if not page_blocks:
                    warnings.append(f"Страница {page_number} не содержит извлекаемого текста")
                    continue
                for block_number, block in enumerate(page_blocks, start=1):
                    blocks.append(
                        NormalizedBlock(
                            kind=BlockKind.TEXT,
                            text=block[4].strip(),
                            data={"bbox": list(block[:4])},
                            provenance=[
                                Provenance(
                                    source_id=source.id,
                                    locator=f"page:{page_number}/block:{block_number}",
                                )
                            ],
                            confidence=1.0,
                        )
                    )
            page_units = document.page_count
        return ExtractionResult(blocks=blocks, page_units=page_units, warnings=warnings)
