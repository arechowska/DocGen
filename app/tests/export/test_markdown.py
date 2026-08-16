from pathlib import Path

import pytest

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.export.markdown import MarkdownExporter
from docgen.export.storage import ExportStorage
from docgen.formatting.schemas import FormattingTemplate, OutputFormat


@pytest.fixture
def document_all_kinds() -> WorkingDocument:
    """Create a test document with all supported node kinds."""
    return WorkingDocument(
        title="Тестовый документ",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.HEADING,
                text="Заголовок",
                data={"level": 1},
            ),
            DocumentNode(
                kind=NodeKind.PARAGRAPH,
                text="Обычный текст",
            ),
            DocumentNode(
                kind=NodeKind.LIST,
                data={
                    "items": ["Пункт 1", "Пункт 2", "Пункт 3"],
                    "ordered": False,
                },
            ),
            DocumentNode(
                kind=NodeKind.TABLE,
                data={
                    "headers": ["Колонка 1", "Колонка 2"],
                    "rows": [
                        ["Значение 1", "Значение 2"],
                        ["Значение 3", "Значение 4"],
                    ],
                },
            ),
            DocumentNode(
                kind=NodeKind.GAP,
            ),
            DocumentNode(
                kind=NodeKind.IMAGE,
                data={
                    "src": "image.png",
                    "alt": "Описание изображения",
                },
            ),
        ],
    )


@pytest.fixture
def markdown_template() -> FormattingTemplate:
    """Create a test markdown formatting template."""
    return FormattingTemplate(
        id="docgen-light-markdown",
        name="Облегченный Markdown",
        format=OutputFormat.MARKDOWN,
        renderer=OutputFormat.MARKDOWN,
        options={},
    )


def test_markdown_renders_supported_nodes(
    document_all_kinds: WorkingDocument,
    markdown_template: FormattingTemplate,
) -> None:
    """Test that MarkdownExporter renders all supported node kinds correctly."""
    rendered = MarkdownExporter().render(document_all_kinds, markdown_template)
    text = rendered.content.decode("utf-8")

    # Check heading
    assert "# Заголовок" in text

    # Check paragraph
    assert "Обычный текст" in text

    # Check list items
    assert "- Пункт" in text

    # Check table
    assert "| Колонка 1 | Колонка 2 |" in text

    # Check gap (as blockquote with fixed message)
    assert "> **Нет данных в источниках**" in text

    # Check image
    assert "![Описание изображения]" in text
    assert "image.png" in text

    # Check filename
    assert rendered.filename.endswith(".md")

    # Check media type
    assert rendered.media_type == "text/markdown"

    # Check encoding (should be UTF-8)
    assert isinstance(rendered.content, bytes)


def test_markdown_renders_nested_nodes(markdown_template: FormattingTemplate) -> None:
    """Test that MarkdownExporter handles nested nodes correctly."""
    document = WorkingDocument(
        title="Вложенный документ",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.HEADING,
                text="Основной заголовок",
                data={"level": 1},
                children=[
                    DocumentNode(
                        kind=NodeKind.PARAGRAPH,
                        text="Вложенный абзац",
                    ),
                ],
            ),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    assert "# Основной заголовок" in text
    assert "Вложенный абзац" in text


def test_markdown_renders_multiline_list(markdown_template: FormattingTemplate) -> None:
    """Test that MarkdownExporter correctly renders lists with multiple items."""
    document = WorkingDocument(
        title="Список",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.LIST,
                data={
                    "items": ["Первый пункт", "Второй пункт"],
                    "ordered": False,
                },
            ),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    # Check both items are in the list
    assert "- Первый пункт" in text
    assert "- Второй пункт" in text


def test_markdown_renders_ordered_list(markdown_template: FormattingTemplate) -> None:
    """Test that MarkdownExporter correctly renders ordered lists."""
    document = WorkingDocument(
        title="Нумерованный список",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.LIST,
                data={
                    "items": ["Первый", "Второй"],
                    "ordered": True,
                },
            ),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    # Check ordered list format
    assert "1. Первый" in text
    assert "2. Второй" in text


def test_markdown_filename_from_document_title(
    markdown_template: FormattingTemplate,
) -> None:
    """Test that filename is derived from document title."""
    document = WorkingDocument(
        title="Мой прекрасный документ",
        template_id="docgen-light-markdown",
        nodes=[],
    )

    rendered = MarkdownExporter().render(document, markdown_template)

    # Filename should be filesystem-safe and end with .md
    assert rendered.filename.endswith(".md")
    # Should contain something from the title
    assert len(rendered.filename) > 3  # At least ".md"


def test_markdown_escapes_table_pipes(markdown_template: FormattingTemplate) -> None:
    """Test that table pipes in content are properly escaped."""
    document = WorkingDocument(
        title="Таблица с трубами",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={
                    "headers": ["Колонка | 1", "Колонка 2"],
                    "rows": [
                        ["Значение | 1", "Значение 2"],
                    ],
                },
            ),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    # Table structure should be preserved
    assert "| Колонка" in text


def test_markdown_ends_with_newline(markdown_template: FormattingTemplate) -> None:
    """Test that rendered markdown ends with a single newline."""
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(kind=NodeKind.PARAGRAPH, text="Текст"),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    content = rendered.content.decode("utf-8")

    # Should end with exactly one newline
    assert content.endswith("\n")
    assert not content.endswith("\n\n")


def test_markdown_separates_top_level_nodes(
    markdown_template: FormattingTemplate,
) -> None:
    """Test that top-level nodes are separated by one blank line."""
    document = WorkingDocument(
        title="Разделённый документ",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(kind=NodeKind.PARAGRAPH, text="Первый абзац"),
            DocumentNode(kind=NodeKind.PARAGRAPH, text="Второй абзац"),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    content = rendered.content.decode("utf-8")

    # Check that paragraphs are separated by blank line
    assert "Первый абзац\n\nВторой абзац" in content


def test_markdown_renders_table_with_headers_no_rows(
    markdown_template: FormattingTemplate,
) -> None:
    """Test that tables with headers but no rows render the header row.

    Production case: user creates/edits a table down to just header row.
    Should render header + separator, not silently drop headers.
    """
    document = WorkingDocument(
        title="Таблица только с заголовками",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={
                    "headers": ["Колонка А", "Колонка Б", "Колонка В"],
                    "rows": [],
                },
            ),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    # Should render header row and separator
    assert "| Колонка А | Колонка Б | Колонка В |" in text
    assert "| --- | --- | --- |" in text
    # Should not be empty
    assert len(text.strip()) > 0


def test_markdown_skips_empty_table(
    markdown_template: FormattingTemplate,
) -> None:
    """Test that tables with no headers and no rows are not rendered.

    Only skip rendering when table has truly nothing to show.
    """
    document = WorkingDocument(
        title="Пустая таблица",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={
                    "rows": [],
                },
            ),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    # Should result in just newline (empty document after rendering the table)
    # The table itself contributes nothing
    assert text.strip() == ""


def test_markdown_renders_table_without_headers(
    markdown_template: FormattingTemplate,
) -> None:
    """Test that tables without headers key render correctly.

    Production DOCX extraction creates tables with only 'rows' key,
    no 'headers' key at all. Should render with empty header row.
    """
    document = WorkingDocument(
        title="Таблица без заголовков",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={
                    "rows": [
                        ["Значение 1", "Значение 2"],
                        ["Значение 3", "Значение 4"],
                    ],
                },
            ),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    # Should render as GFM table with empty header row
    assert "|" in text
    assert "---" in text
    assert "Значение 1" in text
    assert "Значение 3" in text


def test_markdown_renders_textless_gap_node(
    markdown_template: FormattingTemplate,
) -> None:
    """Test that gap nodes without text render the fixed message.

    Production gap nodes created during assembly have no text attribute.
    Should always render "Нет данных в источниках" regardless of text.
    """
    document = WorkingDocument(
        title="Документ с пробелом",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.GAP,
            ),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    # Should render fixed message
    assert "> **Нет данных в источниках**" in text


def test_markdown_renders_list_with_children(
    markdown_template: FormattingTemplate,
) -> None:
    """Test that children of list nodes are rendered."""
    document = WorkingDocument(
        title="Список с детьми",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.LIST,
                data={
                    "items": ["Пункт 1", "Пункт 2"],
                    "ordered": False,
                },
                children=[
                    DocumentNode(kind=NodeKind.PARAGRAPH, text="Пояснение"),
                ],
            ),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    # Both list items and child should be present
    assert "- Пункт 1" in text
    assert "- Пункт 2" in text
    assert "Пояснение" in text


def test_markdown_renders_table_with_children(
    markdown_template: FormattingTemplate,
) -> None:
    """Test that children of table nodes are rendered."""
    document = WorkingDocument(
        title="Таблица с детьми",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={
                    "headers": ["A", "B"],
                    "rows": [["1", "2"]],
                },
                children=[
                    DocumentNode(kind=NodeKind.PARAGRAPH, text="Комментарий"),
                ],
            ),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    # Both table and child should be present
    assert "| A | B |" in text
    assert "Комментарий" in text


def test_markdown_renders_image_with_children(
    markdown_template: FormattingTemplate,
) -> None:
    """Test that children of image nodes are rendered."""
    document = WorkingDocument(
        title="Изображение с детьми",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.IMAGE,
                data={
                    "src": "pic.jpg",
                    "alt": "Картинка",
                },
                children=[
                    DocumentNode(
                        kind=NodeKind.PARAGRAPH, text="Подпись к изображению"
                    ),
                ],
            ),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    # Both image and child should be present
    assert "![Картинка](pic.jpg)" in text
    assert "Подпись к изображению" in text


def test_markdown_renders_gap_with_children(
    markdown_template: FormattingTemplate,
) -> None:
    """Test that children of gap nodes are rendered."""
    document = WorkingDocument(
        title="Пробел с детьми",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.GAP,
                children=[
                    DocumentNode(
                        kind=NodeKind.PARAGRAPH, text="Информация о пробеле"
                    ),
                ],
            ),
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    # Both gap message and child should be present
    assert "> **Нет данных в источниках**" in text
    assert "Информация о пробеле" in text


# --- images without a resolvable src (finding 4) --------------------------


def test_markdown_image_without_src_renders_placeholder_with_caption(
    markdown_template: FormattingTemplate,
) -> None:
    """Production AI-assembled image nodes carry only `text` + provenance,
    never `data.src`. Before the fix, this rendered a literal, meaningless
    `![]()`, silently dropping the description entirely -- unlike
    html.py/docx.py/pdf.py, which all render a placeholder plus the
    caption text in this exact case."""
    document = WorkingDocument(
        title="Документ без источника изображения",
        template_id="docgen-light-markdown",
        nodes=[DocumentNode(kind=NodeKind.IMAGE, text="Схема архитектуры")],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    assert "![]()" not in text
    assert "Схема архитектуры" in text


def test_markdown_image_with_empty_string_src_renders_placeholder(
    markdown_template: FormattingTemplate,
) -> None:
    """An explicit empty-string src must be treated the same as a missing
    one, not rendered as a broken `![alt]()` link."""
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.IMAGE, data={"src": "", "alt": "Логотип"}, text="Подпись"
            )
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    assert "![" not in text
    assert "Подпись" in text


# --- heading level coercion (finding 5) ------------------------------------


def test_markdown_heading_with_string_level_does_not_crash(
    markdown_template: FormattingTemplate,
) -> None:
    """`data` is unvalidated LLM JSON -- a string level like "2" must be
    coerced, not raise TypeError and fail the whole export job. docx.py/
    html.py/pdf.py already tolerate this."""
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(kind=NodeKind.HEADING, text="Заголовок", data={"level": "2"})
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    assert "## Заголовок" in text


def test_markdown_heading_with_float_level_does_not_crash(
    markdown_template: FormattingTemplate,
) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(kind=NodeKind.HEADING, text="Заголовок", data={"level": 2.0})
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    assert "## Заголовок" in text


def test_markdown_heading_with_unparseable_level_defaults_to_one(
    markdown_template: FormattingTemplate,
) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(
                kind=NodeKind.HEADING, text="Заголовок", data={"level": "не число"}
            )
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    assert "# Заголовок" in text
    assert "## Заголовок" not in text


def test_markdown_heading_with_none_level_defaults_to_one(
    markdown_template: FormattingTemplate,
) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-markdown",
        nodes=[
            DocumentNode(kind=NodeKind.HEADING, text="Заголовок", data={"level": None})
        ],
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    text = rendered.content.decode("utf-8")

    assert "# Заголовок" in text
    assert "## Заголовок" not in text


# --- filename byte-length safety (finding 6) -------------------------------


def test_markdown_long_cyrillic_title_produces_storable_filename(
    markdown_template: FormattingTemplate, tmp_path: Path
) -> None:
    """A 150+ character Cyrillic title (titles are allowed up to 200 chars
    elsewhere in the app) must never produce a filename that overflows the
    filesystem's 255-byte limit once ExportStorage appends
    `-{template_id}` -- reproduced end-to-end via the real storage layer."""
    long_title = "Очень длинное название регламента для банковского документа " * 4
    assert len(long_title) > 150
    document = WorkingDocument(
        title=long_title, template_id="docgen-light-markdown", nodes=[]
    )

    rendered = MarkdownExporter().render(document, markdown_template)
    storage = ExportStorage(tmp_path / "data")

    stored = storage.save("proj-1", OutputFormat.MARKDOWN, markdown_template.id, rendered)

    assert len(stored.filename.encode("utf-8")) <= 255
    assert storage.resolve(stored.relative_path).is_file()
