from pathlib import Path

import pymupdf
import pytest

from docgen.extraction.pdf import PdfExtractor
from docgen.extraction.registry import ExtractionError
from docgen.models import Source, SourceKind


def make_source() -> Source:
    return Source(
        id="s1",
        project_id="p1",
        kind=SourceKind.FILE,
        display_name="input.pdf",
        media_type="application/pdf",
        size_bytes=1,
        storage_path="projects/p1/sources/s1.pdf",
        status="stored",
    )


def test_pdf_uses_page_block_locators_and_warns_for_empty_pages(tmp_path: Path) -> None:
    path = tmp_path / "input.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Первый блок")
    document.new_page()
    document.save(path)
    document.close()

    result = PdfExtractor().extract(make_source(), path)

    assert result.page_units == 2
    assert [block.provenance[0].locator for block in result.blocks] == ["page:1/block:1"]
    assert result.warnings == ["Страница 2 не содержит извлекаемого текста"]


def test_pdf_extraction_has_repeatable_block_ids(tmp_path: Path) -> None:
    path = tmp_path / "input.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Повторяемый текст")
    document.save(path)
    document.close()
    extractor = PdfExtractor()

    first_result = extractor.extract(make_source(), path)
    second_result = extractor.extract(make_source(), path)

    assert [block.id for block in first_result.blocks] == [block.id for block in second_result.blocks]


def test_pdf_returns_safe_error_when_file_cannot_be_read(tmp_path: Path) -> None:
    with pytest.raises(ExtractionError, match="Не удалось прочитать PDF-файл"):
        PdfExtractor().extract(make_source(), tmp_path / "missing.pdf")
