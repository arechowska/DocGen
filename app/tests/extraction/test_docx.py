from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from docx import Document

from docgen.extraction.docx import DocxExtractor
from docgen.extraction.registry import ExtractionError
from docgen.extraction.schemas import BlockKind
from docgen.models import Source, SourceKind


def make_source() -> Source:
    return Source(
        id="s1",
        project_id="p1",
        kind=SourceKind.FILE,
        display_name="input.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=1,
        storage_path="projects/p1/sources/s1.docx",
        status="stored",
    )


def test_docx_preserves_document_order_and_structure(tmp_path: Path) -> None:
    path = tmp_path / "input.docx"
    document = Document()
    document.add_heading("Раздел", level=1)
    document.add_paragraph("Обычный текст")
    document.add_paragraph("Пункт", style="List Bullet")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Ключ"
    table.cell(0, 1).text = "Значение"
    document.save(path)

    result = DocxExtractor().extract(make_source(), path)

    assert [block.kind for block in result.blocks] == [
        BlockKind.HEADING,
        BlockKind.TEXT,
        BlockKind.LIST,
        BlockKind.TABLE,
    ]
    assert [block.provenance[0].locator for block in result.blocks] == [
        "paragraph:1",
        "paragraph:2",
        "paragraph:3",
        "table:1",
    ]
    assert result.blocks[0].data == {"level": 1}
    assert result.blocks[2].data == {"style": "List Bullet"}
    assert result.blocks[3].data == {"rows": [["Ключ", "Значение"]]}


def test_docx_extraction_has_repeatable_block_ids(tmp_path: Path) -> None:
    path = tmp_path / "input.docx"
    document = Document()
    document.add_paragraph("Повторяемый текст")
    document.save(path)
    extractor = DocxExtractor()

    first_result = extractor.extract(make_source(), path)
    second_result = extractor.extract(make_source(), path)

    assert [block.id for block in first_result.blocks] == [block.id for block in second_result.blocks]


def test_docx_counts_virtual_page_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "input.docx"
    extractor = DocxExtractor()
    document = Document()
    document.add_paragraph("a" * 1800)
    document.save(path)

    exact_page = extractor.extract(make_source(), path)

    document = Document()
    document.add_paragraph("a" * 1801)
    document.save(path)
    next_page = extractor.extract(make_source(), path)

    assert exact_page.page_units == 1
    assert next_page.page_units == 2


def test_docx_returns_safe_error_when_file_cannot_be_read(tmp_path: Path) -> None:
    with pytest.raises(ExtractionError, match="Не удалось прочитать DOCX-файл"):
        DocxExtractor().extract(make_source(), tmp_path / "missing.docx")


def test_docx_returns_safe_error_for_zip_missing_ooxml_members(tmp_path: Path) -> None:
    path = tmp_path / "missing-members.docx"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("placeholder.txt", "not an OOXML package")

    with pytest.raises(ExtractionError, match="Не удалось прочитать DOCX-файл"):
        DocxExtractor().extract(make_source(), path)


def test_docx_returns_safe_error_for_malformed_package_xml(tmp_path: Path) -> None:
    path = tmp_path / "malformed-xml.docx"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types>")
        archive.writestr("_rels/.rels", "<Relationships>")
        archive.writestr("word/document.xml", "<document>")

    with pytest.raises(ExtractionError, match="Не удалось прочитать DOCX-файл"):
        DocxExtractor().extract(make_source(), path)
