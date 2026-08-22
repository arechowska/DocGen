import base64
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document as OpenDocx
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.export.docx import DocxExporter, local_storage_image_loader
from docgen.export.storage import ExportStorage
from docgen.formatting.schemas import FormattingTemplate, OutputFormat
from docgen.sources.storage import LocalStorage

# A minimal valid 1x1 transparent PNG, used to exercise the "resolvable src"
# image embedding path without depending on any real asset file.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY"
    "42YAAAAASUVORK5CYII="
)


def fake_image_loader(src: str) -> tuple[bytes, str] | None:
    """Test double: resolves a single known src, mirrors the real contract."""
    if src == "images/logo.png":
        return _PNG_BYTES, "image/png"
    return None


@pytest.fixture
def docx_template() -> FormattingTemplate:
    """The real Colvir DOCX template/asset pair, loaded from the catalog dir."""
    return FormattingTemplate(
        id="colvir",
        name="Фирменный стиль Colvir",
        format=OutputFormat.DOCX,
        renderer=OutputFormat.DOCX,
        assets=["colvir.docx"],
    )


def _open(rendered_content: bytes) -> OpenDocx:
    return OpenDocx(BytesIO(rendered_content))


# --- structural / template-fidelity tests ---------------------------------


def test_docx_uses_template_styles_headers_and_tables(docx_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Полный документ",
        template_id="colvir-docx",
        nodes=[
            DocumentNode(kind=NodeKind.HEADING, text="Введение", data={"level": 1}),
            DocumentNode(kind=NodeKind.PARAGRAPH, text="Основной текст."),
            DocumentNode(
                kind=NodeKind.TABLE,
                data={"headers": ["А", "Б"], "rows": [["1", "2"]]},
            ),
        ],
    )

    rendered = DocxExporter(image_loader=fake_image_loader).render(document, docx_template)
    package = _open(rendered.content)

    styles_used = {p.style.name for p in package.paragraphs}
    assert "Heading 1" in styles_used
    assert "Colvir_Абзац" in styles_used
    assert len(package.tables) == 1
    assert package.tables[0].style.name == "Colvir_сетка_таблицы"
    assert package.core_properties.title == document.title


def test_docx_assembled_use_case_uses_full_corporate_form_with_empty_fields(
    docx_template: FormattingTemplate,
) -> None:
    document = WorkingDocument(
        title="Открытие счёта",
        template_id="use-case",
        nodes=[
            DocumentNode(
                kind=NodeKind.HEADING,
                section_id="main-flow",
                text="Основной поток",
                children=[
                    DocumentNode(
                        kind=NodeKind.LIST,
                        data={
                            "ordered": True,
                            "items": [
                                "Клиент отправляет заявление",
                                "Система открывает счёт",
                            ],
                        },
                    )
                ],
            ),
            DocumentNode(
                kind=NodeKind.HEADING,
                section_id="preconditions",
                text="Предусловия",
                children=[
                    DocumentNode(
                        kind=NodeKind.GAP,
                        flags=["missing-source-data"],
                    )
                ],
            ),
            DocumentNode(
                kind=NodeKind.HEADING,
                section_id="result",
                text="Результат",
                children=[
                    DocumentNode(
                        kind=NodeKind.GAP,
                        flags=["missing-source-data"],
                    )
                ],
            ),
        ],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    assert len(package.tables) == 5
    assert package.tables[0].cell(0, 0).text == "Код документа"
    assert package.tables[0].cell(0, 1).text == ""
    assert package.tables[1].cell(0, 0).text == "Область действия"
    assert package.tables[2].cell(0, 0).text == "Наименование"
    assert package.tables[3].cell(0, 0).text == "Ссылка"
    assert package.tables[4].cell(0, 0).text == "Версия документа"
    assert all(table.style.name == "Colvir_сетка_таблицы" for table in package.tables)
    full_text = "\n".join(paragraph.text for paragraph in package.paragraphs)
    assert "Нет данных в источниках" not in full_text
    numbered = [
        paragraph
        for paragraph in package.tables[1].cell(10, 1).paragraphs
        if paragraph.text
    ]
    assert [paragraph.text for paragraph in numbered] == [
        "Клиент отправляет заявление",
        "Система открывает счёт",
    ]
    assert all(
        paragraph._p.pPr is not None and paragraph._p.pPr.numPr is not None
        for paragraph in numbered
    )


def test_docx_rejects_structurally_invalid_assembled_use_case(
    docx_template: FormattingTemplate,
) -> None:
    document = WorkingDocument(
        title="Склеенный сценарий",
        template_id="use-case",
        nodes=[
            DocumentNode(
                kind=NodeKind.HEADING,
                section_id="preconditions",
                text="Предусловия",
                children=[DocumentNode(kind=NodeKind.GAP)],
            ),
            DocumentNode(
                kind=NodeKind.HEADING,
                section_id="main-flow",
                text="Основной поток",
                children=[
                    DocumentNode(
                        kind=NodeKind.PARAGRAPH,
                        text="1. Клиент отправляет заявку. 2. Система открывает счёт.",
                    )
                ],
            ),
            DocumentNode(
                kind=NodeKind.HEADING,
                section_id="result",
                text="Результат",
                children=[DocumentNode(kind=NodeKind.GAP)],
            ),
        ],
    )

    with pytest.raises(ValueError, match="нумерованным списком"):
        DocxExporter().render(document, docx_template)


def test_docx_reflows_template_header_and_preserves_footer(
    docx_template: FormattingTemplate,
) -> None:
    """The header must fit the printable width in Word and LibreOffice."""
    document = WorkingDocument(title="Документ", template_id="colvir-docx", nodes=[])

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    header_text = package.sections[0].header.paragraphs[0].text
    assert "Colvir Banking System" in header_text
    assert header_text.endswith("\tДокумент")
    tab_stops = package.sections[0].header.paragraphs[0].paragraph_format.tab_stops
    assert len(tab_stops) == 1
    assert tab_stops[0].alignment is WD_TAB_ALIGNMENT.RIGHT
    footer_text = "\n".join(p.text for p in package.sections[0].footer.paragraphs)
    assert "Руководство" in footer_text


def test_docx_replaces_template_sample_body_with_contents(docx_template: FormattingTemplate) -> None:
    """The template sample is removed and its TOC page is regenerated."""
    document = WorkingDocument(title="Документ", template_id="colvir-docx", nodes=[])

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    full_text = "\n".join(p.text for p in package.paragraphs)
    assert "Введите название документа" not in full_text
    assert "Введите заголовок первого уровня" not in full_text
    assert "Оглавление" in full_text


def test_docx_contents_uses_document_headings(docx_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="colvir-docx",
        nodes=[
            DocumentNode(kind=NodeKind.HEADING, text="Общие вопросы", data={"level": 2}),
            DocumentNode(
                kind=NodeKind.HEADING,
                text="Настройки",
                data={"level": 3},
            ),
        ],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    contents = [p for p in package.paragraphs if p.text == "Общие вопросы"]
    assert len(contents) == 2
    assert contents[0].style.name == "Colvir_Абзац"
    assert contents[0].paragraph_format.left_indent.twips == Cm(0.6).twips
    assert len([p for p in package.paragraphs if p.text == "Настройки"]) == 2


def test_docx_sets_title_paragraph_and_metadata(docx_template: FormattingTemplate) -> None:
    document = WorkingDocument(title="Заголовок пакета", template_id="colvir-docx", nodes=[])

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    assert package.core_properties.title == "Заголовок пакета"
    title_paragraphs = [
        p for p in package.paragraphs if p.style.name == "Colvir_Обложка_Название"
    ]
    assert any(p.text == "Заголовок пакета" for p in title_paragraphs)


def test_docx_uses_short_cover_title_and_faq_paragraphs(
    docx_template: FormattingTemplate,
) -> None:
    document = WorkingDocument(
        title="Вопросы и ответы по модулю «Главная Книга» Colvir Banking System",
        template_id="colvir-docx",
        nodes=[
            DocumentNode(kind=NodeKind.HEADING, text="Общие вопросы", data={"level": 2}),
            DocumentNode(
                kind=NodeKind.LIST,
                data={
                    "items": [
                        (
                            "Вопрос: Для чего предназначен модуль? "
                            "Ответ: Для ведения Главной Книги."
                        )
                    ]
                },
            ),
        ],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    title = next(
        p for p in package.paragraphs if p.style.name == "Colvir_Обложка_Название"
    )
    assert title.text == "FAQ «Главная Книга» Colvir Banking System"
    header_text = package.sections[0].header.paragraphs[0].text
    assert header_text.endswith("\tFAQ «Главная Книга» Colvir Banking System")

    heading = next(
        p
        for p in package.paragraphs
        if p.text == "Общие вопросы" and p.style.name == "Colvir_Подзаголовок"
    )
    assert heading.style.name == "Colvir_Подзаголовок"
    assert heading.paragraph_format.page_break_before is False

    question = next(p for p in package.paragraphs if p.text.startswith("Вопрос:"))
    answer = next(p for p in package.paragraphs if p.text.startswith("Ответ:"))
    assert question.style.name == "Colvir_Абзац"
    assert answer.style.name == "Colvir_Абзац"
    assert question.runs[0].bold is True
    assert answer.runs[0].bold is True
    assert question._p.pPr.find(qn("w:numPr")) is None
    assert question.paragraph_format.space_after.twips == Pt(8).twips
    assert answer.paragraph_format.space_after.twips == Pt(28).twips


# --- heading level mapping --------------------------------------------------


@pytest.mark.parametrize(
    "level,expected_style",
    [(1, "Heading 1"), (2, "Colvir_Подзаголовок"), (3, "Heading 3"), (6, "Heading 6")],
)
def test_docx_heading_level_maps_to_style(
    docx_template: FormattingTemplate, level: int, expected_style: str
) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="colvir-docx",
        nodes=[DocumentNode(kind=NodeKind.HEADING, text="Раздел", data={"level": level})],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    matching = [p for p in package.paragraphs if p.text == "Раздел"]
    assert len(matching) == 2
    assert matching[-1].style.name == expected_style


def test_docx_heading_level_is_clamped(docx_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="colvir-docx",
        nodes=[
            DocumentNode(kind=NodeKind.HEADING, text="Слишком глубоко", data={"level": 99}),
        ],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    matching = [p for p in package.paragraphs if p.text == "Слишком глубоко"]
    assert matching[-1].style.name == "Heading 6"


# --- tables: header-less / rows-less edge cases -----------------------------


def test_docx_renders_table_with_headers_no_rows(docx_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Таблица только с заголовками",
        template_id="colvir-docx",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={"headers": ["Колонка А", "Колонка Б"], "rows": []},
            ),
        ],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    assert len(package.tables) == 1
    table = package.tables[0]
    assert len(table.rows) == 1
    assert [c.text for c in table.rows[0].cells] == ["Колонка А", "Колонка Б"]
    assert table.rows[0].cells[0].paragraphs[0].style.name == "Colvir_Таблица_заголовок"


def test_docx_renders_table_without_headers(docx_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Таблица без заголовков",
        template_id="colvir-docx",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={"rows": [["Значение 1", "Значение 2"]]},
            ),
        ],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    table = package.tables[0]
    assert len(table.rows) == 1
    assert [c.text for c in table.rows[0].cells] == ["Значение 1", "Значение 2"]
    assert table.rows[0].cells[0].paragraphs[0].style.name == "Colvir_Таблица_текст"


def test_docx_skips_empty_table(docx_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Пустая таблица",
        template_id="colvir-docx",
        nodes=[DocumentNode(kind=NodeKind.TABLE, data={"rows": []})],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    assert len(package.tables) == 0


def test_docx_pads_ragged_table_rows(docx_template: FormattingTemplate) -> None:
    """Rows shorter/longer than the header count must be padded/truncated."""
    document = WorkingDocument(
        title="Неровная таблица",
        template_id="colvir-docx",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={"headers": ["А", "Б", "В"], "rows": [["1"], ["1", "2", "3", "4"]]},
            ),
        ],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    table = package.tables[0]
    assert len(table.rows) == 3
    assert [c.text for c in table.rows[1].cells] == ["1", "", ""]
    assert [c.text for c in table.rows[2].cells] == ["1", "2", "3"]


# --- gap nodes ---------------------------------------------------------------


def test_docx_renders_textless_gap_node(docx_template: FormattingTemplate) -> None:
    """Production gap nodes created during assembly have no text attribute."""
    document = WorkingDocument(
        title="Документ с пробелом",
        template_id="colvir-docx",
        nodes=[DocumentNode(kind=NodeKind.GAP)],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    gap_paragraphs = [p for p in package.paragraphs if p.style.name == "Colvir_Внимание"]
    assert len(gap_paragraphs) == 1
    assert gap_paragraphs[0].text == "Нет данных в источниках"


def test_docx_gap_node_ignores_its_own_text(docx_template: FormattingTemplate) -> None:
    """Even if a gap node somehow carries text, the fixed message wins."""
    document = WorkingDocument(
        title="Документ",
        template_id="colvir-docx",
        nodes=[DocumentNode(kind=NodeKind.GAP, text="какой-то произвольный текст")],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    full_text = "\n".join(p.text for p in package.paragraphs)
    assert "Нет данных в источниках" in full_text
    assert "какой-то произвольный текст" not in full_text


# --- images: resolution fallback pattern (Task 3 ruling) -------------------


def test_docx_embeds_resolvable_image(docx_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Документ с изображением",
        template_id="colvir-docx",
        nodes=[
            DocumentNode(
                kind=NodeKind.IMAGE,
                data={"src": "images/logo.png", "alt": "Логотип"},
                text="Подпись к рисунку",
            ),
        ],
    )

    rendered = DocxExporter(image_loader=fake_image_loader).render(document, docx_template)
    package = _open(rendered.content)

    assert len(package.inline_shapes) == 1
    caption_paragraphs = [
        p for p in package.paragraphs if p.style.name == "Colvir_Рисунок_Подпись"
    ]
    assert any(p.text == "Подпись к рисунку" for p in caption_paragraphs)


def test_docx_image_without_src_renders_placeholder(docx_template: FormattingTemplate) -> None:
    """Production AI-assembled image nodes carry no data.src at all.

    Must not fabricate or attempt an embed; must render the same
    placeholder wording as the HTML exporter's fallback.
    """
    document = WorkingDocument(
        title="Документ без источника изображения",
        template_id="colvir-docx",
        nodes=[DocumentNode(kind=NodeKind.IMAGE, text="Схема архитектуры")],
    )

    rendered = DocxExporter(image_loader=fake_image_loader).render(document, docx_template)
    package = _open(rendered.content)

    assert len(package.inline_shapes) == 0
    full_text = "\n".join(p.text for p in package.paragraphs)
    assert "Изображение или схема" in full_text
    assert "Схема архитектуры" in full_text


def test_docx_image_with_unresolvable_src_renders_placeholder(
    docx_template: FormattingTemplate,
) -> None:
    """A src the loader cannot resolve (e.g. deleted file) falls back safely."""
    document = WorkingDocument(
        title="Документ с недоступным изображением",
        template_id="colvir-docx",
        nodes=[DocumentNode(kind=NodeKind.IMAGE, data={"src": "images/missing.png"})],
    )

    rendered = DocxExporter(image_loader=fake_image_loader).render(document, docx_template)
    package = _open(rendered.content)

    assert len(package.inline_shapes) == 0
    full_text = "\n".join(p.text for p in package.paragraphs)
    assert "Изображение или схема" in full_text


def test_docx_renders_without_image_loader(docx_template: FormattingTemplate) -> None:
    """DocxExporter must work with no image_loader at all (defaults to placeholders)."""
    document = WorkingDocument(
        title="Документ",
        template_id="colvir-docx",
        nodes=[DocumentNode(kind=NodeKind.IMAGE, data={"src": "images/logo.png"})],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    assert len(package.inline_shapes) == 0


# --- lists -------------------------------------------------------------------


def test_docx_unordered_list_applies_bullet_numbering(docx_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="colvir-docx",
        nodes=[
            DocumentNode(
                kind=NodeKind.LIST, data={"items": ["Первый", "Второй"], "ordered": False}
            ),
        ],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    item_paragraphs = [p for p in package.paragraphs if p.text in ("Первый", "Второй")]
    assert len(item_paragraphs) == 2
    for paragraph in item_paragraphs:
        assert paragraph.style.name == "Colvir_Стиль_М1"
        num_pr = paragraph._p.pPr.find(qn("w:numPr"))
        assert num_pr is not None


def test_docx_ordered_and_unordered_lists_use_distinct_numbering(
    docx_template: FormattingTemplate,
) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="colvir-docx",
        nodes=[
            DocumentNode(kind=NodeKind.LIST, data={"items": ["А"], "ordered": True}),
            DocumentNode(kind=NodeKind.LIST, data={"items": ["Б"], "ordered": False}),
        ],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    def num_id_for(text: str) -> str:
        paragraph = next(p for p in package.paragraphs if p.text == text)
        num_id_element = paragraph._p.pPr.find(qn("w:numPr")).find(qn("w:numId"))
        return num_id_element.get(qn("w:val"))

    assert num_id_for("А") != num_id_for("Б")


def test_docx_empty_list_renders_nothing(docx_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="colvir-docx",
        nodes=[DocumentNode(kind=NodeKind.LIST, data={"items": []})],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    # The cover title and the generated contents title are the only text.
    assert len([p for p in package.paragraphs if p.text.strip()]) == 2


# --- nested children on every node kind -------------------------------------


def test_docx_renders_nested_children_for_every_kind(docx_template: FormattingTemplate) -> None:
    """Every node kind may carry children; all must be rendered."""
    document = WorkingDocument(
        title="Документ с вложенными узлами",
        template_id="colvir-docx",
        nodes=[
            DocumentNode(
                kind=NodeKind.HEADING,
                text="Заголовок",
                data={"level": 1},
                children=[
                    DocumentNode(
                        kind=NodeKind.PARAGRAPH,
                        text="Вложенный абзац",
                        children=[
                            DocumentNode(
                                kind=NodeKind.LIST,
                                data={"items": ["А", "Б"]},
                                children=[
                                    DocumentNode(
                                        kind=NodeKind.TABLE,
                                        data={"rows": [["1", "2"]]},
                                        children=[
                                            DocumentNode(
                                                kind=NodeKind.GAP,
                                                children=[
                                                    DocumentNode(
                                                        kind=NodeKind.IMAGE,
                                                        text="Вложенное изображение",
                                                    )
                                                ],
                                            )
                                        ],
                                    )
                                ],
                            )
                        ],
                    )
                ],
            ),
        ],
    )

    rendered = DocxExporter().render(document, docx_template)
    package = _open(rendered.content)

    full_text = "\n".join(p.text for p in package.paragraphs)
    assert "Заголовок" in full_text
    assert "Вложенный абзац" in full_text
    assert "А" in full_text and "Б" in full_text
    assert len(package.tables) == 1
    assert package.tables[0].rows[0].cells[0].text == "1"
    assert "Нет данных в источниках" in full_text
    assert "Вложенное изображение" in full_text


# --- filename / media type ---------------------------------------------------


def test_docx_filename_and_media_type(docx_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Мой прекрасный документ", template_id="colvir-docx", nodes=[]
    )

    rendered = DocxExporter().render(document, docx_template)

    assert rendered.filename.endswith(".docx")
    assert (
        rendered.media_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


# --- local_storage_image_loader re-export (shared with HtmlExporter) -------


def test_local_storage_image_loader_resolves_stored_image(tmp_path) -> None:
    storage = LocalStorage(tmp_path)
    saved = storage.save("project-1", "source-1", "logo.png", BytesIO(_PNG_BYTES))
    loader = local_storage_image_loader(storage)

    result = loader(saved.relative_path)

    assert result is not None
    content, media_type = result
    assert content == _PNG_BYTES
    assert media_type == "image/png"


# --- filename byte-length safety (finding 6) -------------------------------


def test_docx_long_cyrillic_title_produces_storable_filename(
    docx_template: FormattingTemplate, tmp_path: Path
) -> None:
    """A 150+ character Cyrillic title must never produce a filename that
    overflows the filesystem's 255-byte limit once ExportStorage appends
    `-{template_id}` -- reproduced end-to-end via the real storage layer."""
    long_title = "Очень длинное название регламента для банковского документа " * 4
    assert len(long_title) > 150
    document = WorkingDocument(title=long_title, template_id="colvir", nodes=[])

    rendered = DocxExporter().render(document, docx_template)
    storage = ExportStorage(tmp_path / "data")

    stored = storage.save("proj-1", OutputFormat.DOCX, docx_template.id, rendered)

    assert len(stored.filename.encode("utf-8")) <= 255
    assert storage.resolve(stored.relative_path).is_file()
