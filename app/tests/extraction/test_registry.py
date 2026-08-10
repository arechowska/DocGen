from pathlib import Path

import pytest
from PIL import Image

from docgen.extraction.image import ImageExtractor
from docgen.extraction.pdf import PdfExtractor
from docgen.extraction.registry import ExtractionError, ExtractorRegistry
from docgen.extraction.text import TextExtractor
from docgen.models import Source, SourceKind


def make_source(media_type: str, storage_path: str = "projects/p1/sources/s1") -> Source:
    return Source(
        id="s1",
        project_id="p1",
        kind=SourceKind.FILE,
        display_name="input",
        media_type=media_type,
        size_bytes=1,
        storage_path=storage_path,
        status="stored",
    )


@pytest.fixture
def pdf_source() -> Source:
    return make_source("application/pdf")


def test_registry_selects_pdf_extractor(pdf_source: Source) -> None:
    assert isinstance(ExtractorRegistry.default().for_source(pdf_source), PdfExtractor)


def test_registry_selects_text_and_image_extractors() -> None:
    registry = ExtractorRegistry.default()

    assert isinstance(registry.for_source(make_source("text/plain")), TextExtractor)
    assert isinstance(registry.for_source(make_source("image/png")), ImageExtractor)


def test_registry_rejects_an_unsupported_file_type() -> None:
    with pytest.raises(ExtractionError, match="Неподдерживаемый формат"):
        ExtractorRegistry.default().for_source(make_source("application/zip"))


def test_image_extractor_returns_dimensions_and_local_path(tmp_path: Path) -> None:
    path = tmp_path / "diagram.png"
    Image.new("RGB", (16, 9), "white").save(path)

    extractor = ImageExtractor()
    result = extractor.extract(make_source("image/png"), path)
    repeated_result = extractor.extract(make_source("image/png"), path)

    assert result.page_units == 1
    assert result.blocks[0].data == {"width": 16, "height": 9, "storage_path": str(path)}
    assert result.blocks[0].provenance[0].locator == "image:1"
    assert result.blocks[0].id == repeated_result.blocks[0].id


def test_image_pixel_budget_is_checked_before_full_decode(tmp_path: Path) -> None:
    path = tmp_path / "diagram.png"
    Image.new("RGB", (5, 4), "white").save(path)

    assert ImageExtractor(max_image_pixels=20).extract(
        make_source("image/png"), path
    ).blocks[0].data["width"] == 5
    with pytest.raises(ExtractionError, match="Изображение слишком большое"):
        ImageExtractor(max_image_pixels=19).extract(make_source("image/png"), path)
