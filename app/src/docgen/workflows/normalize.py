from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING

from docgen.extraction.registry import ExtractionResult, ExtractorRegistry
from docgen.extraction.schemas import BlockKind, NormalizedBlock
from docgen.models import Source, SourceKind
from docgen.sources.repository import SourceRepository
from docgen.sources.storage import LocalStorage

if TYPE_CHECKING:
    from docgen.extraction.confluence import ConfluenceSource

_MAX_PAGE_UNITS = 150
_LONG_PROCESSING_THRESHOLD = 101
_PAGE_LIMIT_MESSAGE = "Максимальный объём — 150 страниц"
_LONG_PROCESSING_WARNING = "Обработка может занять более пяти минут"


class PageLimitExceeded(ValueError):
    """Raised before downstream model work for a project over the page limit."""


@dataclass(frozen=True)
class VirtualPageCalculator:
    chars_per_page: int = 1800

    def __post_init__(self) -> None:
        if self.chars_per_page <= 0:
            raise ValueError("chars_per_page must be positive")

    def from_text(self, text: str) -> int:
        non_whitespace_characters = sum(not character.isspace() for character in text)
        return ceil(max(1, non_whitespace_characters) / self.chars_per_page)

    def from_blocks(self, blocks: Iterable[NormalizedBlock]) -> int:
        block_list = list(blocks)
        text = "".join(block.text for block in block_list if block.kind is not BlockKind.IMAGE)
        return self.from_text(text) + sum(block.kind is BlockKind.IMAGE for block in block_list)


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

    def run(self, project_id: str) -> NormalizedProject:
        blocks: list[NormalizedBlock] = []
        warnings: list[str] = []
        total_pages = 0
        block_ids: set[str] = set()

        for source in self._sources.list_for_project(project_id):
            extraction = self._extract(source)
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

    def _extract(self, source: Source) -> ExtractionResult:
        if source.kind is SourceKind.CONFLUENCE:
            if source.url is None:
                raise ValueError("Для источника Confluence не указан URL")
            return self._confluence.fetch(source.url)

        if source.storage_path is None:
            raise ValueError("Для файлового источника не указан путь хранения")
        path: Path = self._storage.resolve(source.storage_path)
        return self._extractors.for_source(source).extract(source, path)
