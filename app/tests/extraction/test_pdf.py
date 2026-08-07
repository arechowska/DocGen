from pathlib import Path

import pymupdf

from docgen.extraction.pdf import PdfExtractor
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
