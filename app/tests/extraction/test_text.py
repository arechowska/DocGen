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
