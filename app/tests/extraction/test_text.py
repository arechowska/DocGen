from pathlib import Path

import pytest

from docgen.extraction.registry import ExtractionError
from docgen.extraction.schemas import BlockKind
from docgen.extraction.text import TextExtractor
from docgen.models import Source, SourceKind


def make_source(media_type: str) -> Source:
    return Source(
        id="s1",
        project_id="p1",
        kind=SourceKind.FILE,
        display_name="input",
        media_type=media_type,
        size_bytes=1,
        storage_path="projects/p1/sources/s1",
        status="stored",
    )


def test_txt_has_stable_line_locators(tmp_path: Path) -> None:
    path = tmp_path / "input.txt"
    path.write_text("Первая строка\nВторая строка", encoding="utf-8")

    result = TextExtractor().extract(make_source("text/plain"), path)

    assert [block.provenance[0].locator for block in result.blocks] == ["lines:1-1", "lines:2-2"]


def test_txt_decodes_a_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "input.txt"
    path.write_text("Текст", encoding="utf-8-sig")

    result = TextExtractor().extract(make_source("text/plain"), path)

    assert result.blocks[0].text == "Текст"


def test_text_file_byte_budget_accepts_exact_boundary_and_rejects_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "input.txt"
    path.write_bytes(b"1234")
    assert TextExtractor(max_file_bytes=4).extract(
        make_source("text/plain"), path
    ).blocks[0].text == "1234"

    path.write_bytes(b"12345")
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("oversized text must not be read")
        ),
    )
    with pytest.raises(ExtractionError, match="Файл превышает допустимый размер"):
        TextExtractor(max_file_bytes=4).extract(make_source("text/plain"), path)


def test_txt_extraction_has_repeatable_block_ids(tmp_path: Path) -> None:
    path = tmp_path / "input.txt"
    path.write_text("Повторяемый текст", encoding="utf-8")
    extractor = TextExtractor()

    first_result = extractor.extract(make_source("text/plain"), path)
    second_result = extractor.extract(make_source("text/plain"), path)

    assert [block.id for block in first_result.blocks] == [block.id for block in second_result.blocks]


@pytest.mark.parametrize(("media_type", "suffix"), (("text/plain", ".txt"), ("text/markdown", ".md")))
def test_non_paginated_text_counts_virtual_page_boundaries(
    tmp_path: Path,
    media_type: str,
    suffix: str,
) -> None:
    path = tmp_path / f"input{suffix}"
    extractor = TextExtractor()

    path.write_text("a" * 1800, encoding="utf-8")
    exact_page = extractor.extract(make_source(media_type), path)
    path.write_text("a" * 1801, encoding="utf-8")
    next_page = extractor.extract(make_source(media_type), path)

    assert exact_page.page_units == 1
    assert next_page.page_units == 2


def test_txt_rejects_non_utf8_content(tmp_path: Path) -> None:
    path = tmp_path / "input.txt"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(ExtractionError, match="Не удалось прочитать текстовый файл в UTF-8"):
        TextExtractor().extract(make_source("text/plain"), path)


def test_markdown_preserves_heading_list_and_table_intent(tmp_path: Path) -> None:
    path = tmp_path / "input.md"
    path.write_text(
        "# Заголовок\n\n- Первый\n- Второй\n\n| Колонка | Значение |\n| --- | --- |\n| A | B |\n",
        encoding="utf-8",
    )

    result = TextExtractor().extract(make_source("text/markdown"), path)

    assert [block.kind for block in result.blocks] == [BlockKind.HEADING, BlockKind.LIST, BlockKind.TABLE]
    assert result.blocks[0].data == {"level": 1}
    assert result.blocks[1].data == {"ordered": False, "items": ["Первый", "Второй"]}
    assert result.blocks[2].data == {
        "rows": [["Колонка", "Значение"], ["A", "B"]],
    }
