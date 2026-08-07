from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from docgen.extraction.schemas import NormalizedBlock
from docgen.models import Source, SourceKind


class ExtractionError(ValueError):
    """Raised when a source cannot be extracted safely."""


@dataclass(frozen=True)
class ExtractionResult:
    blocks: list[NormalizedBlock]
    page_units: int
    warnings: list[str]


class Extractor(Protocol):
    def extract(self, source: Source, path: Path) -> ExtractionResult: ...


class ExtractorRegistry:
    def __init__(
        self,
        text_extractor: Extractor,
        docx_extractor: Extractor,
        pdf_extractor: Extractor,
        image_extractor: Extractor,
    ) -> None:
        self._text_extractor = text_extractor
        self._docx_extractor = docx_extractor
        self._pdf_extractor = pdf_extractor
        self._image_extractor = image_extractor

    @classmethod
    def default(cls) -> ExtractorRegistry:
        from docgen.extraction.docx import DocxExtractor
        from docgen.extraction.image import ImageExtractor
        from docgen.extraction.pdf import PdfExtractor
        from docgen.extraction.text import TextExtractor

        return cls(
            text_extractor=TextExtractor(),
            docx_extractor=DocxExtractor(),
            pdf_extractor=PdfExtractor(),
            image_extractor=ImageExtractor(),
        )

    def for_source(self, source: Source) -> Extractor:
        if source.kind is not SourceKind.FILE:
            raise ExtractionError("Поддерживаются только локальные файловые источники")

        media_type = (source.media_type or "").lower()
        extension = Path(source.display_name).suffix.lower()
        if media_type in {"text/plain", "text/markdown", "text/x-markdown", "text/md"} or extension in {
            ".txt",
            ".md",
            ".markdown",
            ".mdown",
        }:
            return self._text_extractor
        if (
            media_type
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            or extension == ".docx"
        ):
            return self._docx_extractor
        if media_type == "application/pdf" or extension == ".pdf":
            return self._pdf_extractor
        if media_type.startswith("image/"):
            return self._image_extractor

        raise ExtractionError("Неподдерживаемый формат источника")
