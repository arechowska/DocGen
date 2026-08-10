from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from docgen.extraction.schemas import BlockKind, NormalizedBlock
from docgen.models import Source, SourceKind

_DEFAULT_MAX_FILE_BYTES = 52_428_800
_FILE_BUDGET_ERROR = "Файл превышает допустимый размер"


class ExtractionError(ValueError):
    """Raised when a source cannot be extracted safely."""


@dataclass(frozen=True)
class ExtractionResult:
    blocks: list[NormalizedBlock]
    page_units: int
    warnings: list[str]


class Extractor(Protocol):
    def extract(self, source: Source, path: Path) -> ExtractionResult: ...


def preflight_file_size(
    path: Path,
    max_bytes: int,
    *,
    read_error_message: str,
) -> None:
    try:
        size_bytes = path.stat().st_size
    except OSError as error:
        raise ExtractionError(read_error_message) from error
    if size_bytes > max_bytes:
        raise ExtractionError(_FILE_BUDGET_ERROR)


def stable_block_id(source_id: str, kind: BlockKind, locator: str, text: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{source_id}\n{kind.value}\n{locator}\n{text}"))


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
    def default(cls, settings: object | None = None) -> ExtractorRegistry:
        from docgen.extraction.docx import DocxExtractor
        from docgen.extraction.image import ImageExtractor
        from docgen.extraction.pdf import PdfExtractor
        from docgen.extraction.text import TextExtractor

        max_file_bytes = getattr(settings, "max_upload_bytes", _DEFAULT_MAX_FILE_BYTES)
        return cls(
            text_extractor=TextExtractor(max_file_bytes=max_file_bytes),
            docx_extractor=DocxExtractor(
                max_file_bytes=max_file_bytes,
                max_archive_entries=getattr(settings, "max_archive_entries", 10_000),
                max_archive_uncompressed_bytes=getattr(
                    settings,
                    "max_archive_uncompressed_bytes",
                    209_715_200,
                ),
            ),
            pdf_extractor=PdfExtractor(max_file_bytes=max_file_bytes),
            image_extractor=ImageExtractor(
                max_file_bytes=max_file_bytes,
                max_image_pixels=getattr(settings, "max_image_pixels", 40_000_000),
            ),
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


__all__ = [
    "ExtractionError",
    "ExtractionResult",
    "Extractor",
    "ExtractorRegistry",
    "preflight_file_size",
    "stable_block_id",
]
