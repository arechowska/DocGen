from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from docgen.extraction.registry import ExtractionError, ExtractionResult, ExtractorRegistry
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.models import Source, SourceKind
from docgen.workflows.normalize import (
    NormalizationWorkflow,
    PageLimitExceeded,
    VirtualPageCalculator,
)


@dataclass
class FakeSources:
    results: list[ExtractionResult] = field(default_factory=list)
    project_id: str | None = None

    def list_for_project(self, project_id: str) -> list[Source]:
        self.project_id = project_id
        return [
            Source(
                id=f"source-{index}",
                project_id=project_id,
                kind=SourceKind.FILE,
                display_name=f"source-{index}.txt",
                media_type="text/plain",
                size_bytes=1,
                storage_path=f"projects/{project_id}/sources/source-{index}.txt",
                status="stored",
            )
            for index in range(1, len(self.results) + 1)
        ]


class FakeStorage:
    def resolve(self, relative_path: str) -> Path:
        return Path(relative_path)


class FakeExtractors:
    def __init__(self, sources: FakeSources) -> None:
        self._sources = sources
        self.extracted_source_ids: list[str] = []

    def for_source(self, _source: Source) -> FakeExtractors:
        return self

    def extract(self, source: Source, path: Path) -> ExtractionResult:
        self.extracted_source_ids.append(source.id)
        return self._sources.results[int(source.id.removeprefix("source-")) - 1]


class FakeConfluence:
    def fetch(self, url: str, *, before_external_call=None) -> ExtractionResult:
        del before_external_call
        raise AssertionError("a file source must not use the Confluence client")


@dataclass
class StaticSources:
    source: Source

    def list_for_project(self, project_id: str) -> list[Source]:
        assert project_id == self.source.project_id
        return [self.source]


@dataclass
class StaticStorage:
    path: Path

    def resolve(self, relative_path: str) -> Path:
        assert relative_path == "projects/p1/sources/source-1.txt"
        return self.path


@pytest.fixture
def sources() -> FakeSources:
    return FakeSources()


@pytest.fixture
def extractors(sources: FakeSources) -> FakeExtractors:
    return FakeExtractors(sources)


@pytest.fixture
def workflow(
    sources: FakeSources,
    extractors: FakeExtractors,
) -> NormalizationWorkflow:
    return NormalizationWorkflow(sources, FakeStorage(), extractors, FakeConfluence())


def test_page_limit_accepts_150_and_rejects_151(
    workflow: NormalizationWorkflow,
    sources: FakeSources,
) -> None:
    sources.results = [ExtractionResult(blocks=[], page_units=150, warnings=[])]

    assert workflow.run("p1").total_pages == 150

    sources.results = [ExtractionResult(blocks=[], page_units=151, warnings=[])]

    with pytest.raises(PageLimitExceeded, match="Максимальный объём — 150 страниц"):
        workflow.run("p1")


def test_virtual_page_count_is_deterministic() -> None:
    assert VirtualPageCalculator(chars_per_page=1800).from_text("а" * 1801) == 2


def test_virtual_pages_keep_one_text_page_and_add_one_for_each_image() -> None:
    calculator = VirtualPageCalculator(chars_per_page=1800)
    blocks = [
        _block("text", BlockKind.TEXT, " \n\t "),
        _block("image-1", BlockKind.IMAGE, "diagram.png"),
        _block("image-2", BlockKind.IMAGE, "flow.png"),
    ]

    assert calculator.from_blocks(blocks) == 3


def test_normalization_preserves_source_order_and_makes_block_ids_unique(
    workflow: NormalizationWorkflow,
    sources: FakeSources,
    extractors: FakeExtractors,
) -> None:
    sources.results = [
        ExtractionResult(blocks=[_block("shared", BlockKind.TEXT, "first")], page_units=1, warnings=[]),
        ExtractionResult(blocks=[_block("shared", BlockKind.TEXT, "second")], page_units=1, warnings=[]),
    ]

    result = workflow.run("p1")

    assert sources.project_id == "p1"
    assert extractors.extracted_source_ids == ["source-1", "source-2"]
    assert [block.id for block in result.blocks] == ["source-1:shared", "source-2:shared"]
    assert len({block.id for block in result.blocks}) == len(result.blocks)


def test_normalization_calls_cancellation_gate_immediately_before_every_extractor(
    sources: FakeSources,
) -> None:
    events: list[str] = []
    sources.results = [
        ExtractionResult(blocks=[], page_units=1, warnings=[]),
        ExtractionResult(blocks=[], page_units=1, warnings=[]),
    ]

    class GatedExtractors(FakeExtractors):
        def extract(self, source: Source, path: Path) -> ExtractionResult:
            events.append(f"extract:{source.id}")
            return super().extract(source, path)

    workflow = NormalizationWorkflow(
        sources,
        FakeStorage(),
        GatedExtractors(sources),
        FakeConfluence(),
    )

    workflow.run("p1", before_extract=lambda: events.append("gate"))

    assert events == ["gate", "extract:source-1", "gate", "extract:source-2"]


def test_normalization_delegates_each_confluence_request_gate_to_client() -> None:
    events: list[str] = []
    source = Source(
        id="source-1",
        project_id="p1",
        kind=SourceKind.CONFLUENCE,
        display_name="https://wiki.example.test/pages/42",
        url="https://wiki.example.test/pages/42",
        status="linked",
    )

    class GatedConfluence:
        def fetch(self, url: str, *, before_external_call=None) -> ExtractionResult:
            assert url == source.url
            assert before_external_call is not None
            before_external_call()
            events.append("request")
            return ExtractionResult(blocks=[], page_units=1, warnings=[])

    workflow = NormalizationWorkflow(
        StaticSources(source),
        StaticStorage(Path("unused")),
        ExtractorRegistry.default(),
        GatedConfluence(),
    )

    workflow.run("p1", before_extract=lambda: events.append("gate"))

    assert events == ["gate", "request"]


def test_normalization_rebinds_confluence_provenance_to_stored_source_id() -> None:
    source = Source(
        id="stored-source-id",
        project_id="p1",
        kind=SourceKind.CONFLUENCE,
        display_name="https://wiki.example.test/pages/42",
        url="https://wiki.example.test/pages/42",
        status="linked",
    )

    class ConfluencePage:
        def fetch(self, url: str, *, before_external_call=None) -> ExtractionResult:
            del url, before_external_call
            return ExtractionResult(
                blocks=[
                    NormalizedBlock(
                        id="wiki-block",
                        kind=BlockKind.TEXT,
                        text="Содержимое Wiki",
                        provenance=[
                            Provenance(
                                source_id="confluence:42",
                                locator="confluence:42#paragraph-1",
                            )
                        ],
                        confidence=1.0,
                    )
                ],
                page_units=1,
                warnings=[],
            )

    workflow = NormalizationWorkflow(
        StaticSources(source),
        StaticStorage(Path("unused")),
        ExtractorRegistry.default(),
        ConfluencePage(),
    )

    result = workflow.run("p1")

    assert result.blocks[0].provenance[0].source_id == "stored-source-id"
    assert result.blocks[0].provenance[0].locator == (
        "confluence:42#paragraph-1"
    )


def test_normalization_skips_inaccessible_confluence_and_keeps_file_sources() -> None:
    confluence_source = Source(
        id="confluence-1",
        project_id="p1",
        kind=SourceKind.CONFLUENCE,
        display_name="https://wiki.example.test/pages/42",
        url="https://wiki.example.test/pages/42",
        status="linked",
    )
    file_source = Source(
        id="file-1",
        project_id="p1",
        kind=SourceKind.FILE,
        display_name="case.txt",
        media_type="text/plain",
        size_bytes=4,
        storage_path="projects/p1/sources/case.txt",
        status="stored",
    )

    class MixedSources:
        def list_for_project(self, project_id: str) -> list[Source]:
            assert project_id == "p1"
            return [confluence_source, file_source]

    class FileExtractor:
        def for_source(self, source: Source) -> FileExtractor:
            assert source is file_source
            return self

        def extract(self, source: Source, path: Path) -> ExtractionResult:
            assert source is file_source
            assert path == Path("projects/p1/sources/case.txt")
            return ExtractionResult(
                blocks=[_block("file-block", BlockKind.TEXT, "DOCX content")],
                page_units=1,
                warnings=[],
            )

    class InaccessibleConfluence:
        def fetch(self, url: str, *, before_external_call=None) -> ExtractionResult:
            assert url == confluence_source.url
            raise ExtractionError("Нет доступа к странице Confluence")

    result = NormalizationWorkflow(
        MixedSources(),
        FakeStorage(),
        FileExtractor(),
        InaccessibleConfluence(),
    ).run("p1")

    assert [block.text for block in result.blocks] == ["DOCX content"]
    assert result.total_pages == 1
    assert result.warnings == [
        "Источник Confluence пропущен: Нет доступа к странице Confluence"
    ]
    assert result.unavailable_source_ids == ("confluence-1",)


def test_normalization_includes_source_warnings_and_adds_long_processing_warning(
    workflow: NormalizationWorkflow,
    sources: FakeSources,
) -> None:
    sources.results = [ExtractionResult(blocks=[], page_units=101, warnings=["source warning"])]

    result = workflow.run("p1")

    assert result.warnings == ["source warning", "Обработка может занять более пяти минут"]


def test_normalization_enforces_limit_for_actual_text_extraction(tmp_path: Path) -> None:
    path = tmp_path / "input.txt"
    source = Source(
        id="source-1",
        project_id="p1",
        kind=SourceKind.FILE,
        display_name="input.txt",
        media_type="text/plain",
        size_bytes=1,
        storage_path="projects/p1/sources/source-1.txt",
        status="stored",
    )
    workflow = NormalizationWorkflow(
        StaticSources(source),
        StaticStorage(path),
        ExtractorRegistry.default(),
        FakeConfluence(),
    )

    path.write_text("a" * 270_000, encoding="utf-8")
    assert workflow.run("p1").total_pages == 150

    path.write_text("a" * 270_001, encoding="utf-8")
    with pytest.raises(PageLimitExceeded, match="Максимальный объём — 150 страниц"):
        workflow.run("p1")


def _block(block_id: str, kind: BlockKind, text: str) -> NormalizedBlock:
    return NormalizedBlock(
        id=block_id,
        kind=kind,
        text=text,
        provenance=[Provenance(source_id="original-source", locator="locator")],
        confidence=1.0,
    )
