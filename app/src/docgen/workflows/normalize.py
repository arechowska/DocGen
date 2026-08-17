from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from docgen.extraction.page_units import VirtualPageCalculator
from docgen.extraction.registry import ExtractionError, ExtractionResult, ExtractorRegistry
from docgen.extraction.schemas import NormalizedBlock
from docgen.models import Source, SourceKind
from docgen.sources.repository import SourceRepository
from docgen.sources.storage import LocalStorage

if TYPE_CHECKING:
    from docgen.extraction.confluence import ConfluenceSource

_MAX_PAGE_UNITS = 150
_LONG_PROCESSING_THRESHOLD = 101
_PAGE_LIMIT_MESSAGE = "Максимальный объём — 150 страниц"
_LONG_PROCESSING_WARNING = "Обработка может занять более пяти минут"

__all__ = [
    "NormalizationWorkflow",
    "NormalizedProject",
    "PageLimitExceeded",
    "VirtualPageCalculator",
]


class PageLimitExceeded(ValueError):
    """Raised before downstream model work for a project over the page limit."""


@dataclass(frozen=True)
class NormalizedProject:
    blocks: list[NormalizedBlock]
    total_pages: int
    warnings: list[str]


class NormalizationWorkflow:
    def __init__(
        self,
        sources: SourceRepository,
        storage: LocalStorage,
        extractors: ExtractorRegistry,
        confluence: ConfluenceSource,
    ) -> None:
        self._sources = sources
        self._storage = storage
        self._extractors = extractors
        self._confluence = confluence

    def run(
        self,
        project_id: str,
        before_extract: Callable[[], None] | None = None,
    ) -> NormalizedProject:
        blocks: list[NormalizedBlock] = []
        warnings: list[str] = []
        total_pages = 0
        block_ids: set[str] = set()

        for source in self._sources.list_for_project(project_id):
            try:
                extraction = self._extract(source, before_extract)
            except ExtractionError as error:
                if source.kind is not SourceKind.CONFLUENCE:
                    raise
                warnings.append(f"Источник Confluence пропущен: {error}")
                continue
            total_pages += extraction.page_units
            if total_pages > _MAX_PAGE_UNITS:
                raise PageLimitExceeded(_PAGE_LIMIT_MESSAGE)

            warnings.extend(extraction.warnings)
            for block in extraction.blocks:
                normalized_block = block.model_copy(update={"id": f"{source.id}:{block.id}"})
                if normalized_block.id in block_ids:
                    raise ValueError(f"Повторяющийся идентификатор блока: {normalized_block.id}")
                block_ids.add(normalized_block.id)
                blocks.append(normalized_block)

        if total_pages >= _LONG_PROCESSING_THRESHOLD:
            warnings.append(_LONG_PROCESSING_WARNING)

        return NormalizedProject(blocks=blocks, total_pages=total_pages, warnings=warnings)

    def _extract(
        self,
        source: Source,
        before_extract: Callable[[], None] | None,
    ) -> ExtractionResult:
        if source.kind is SourceKind.CONFLUENCE:
            if source.url is None:
                raise ValueError("Для источника Confluence не указан URL")
            return self._confluence.fetch(
                source.url,
                before_external_call=before_extract,
            )

        if source.storage_path is None:
            raise ValueError("Для файлового источника не указан путь хранения")
        path: Path = self._storage.resolve(source.storage_path)
        if before_extract is not None:
            before_extract()
        return self._extractors.for_source(source).extract(source, path)
