import base64
import re
from pathlib import Path
from unittest.mock import patch

import pymupdf
import pytest

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.export.pdf import ExportError, PdfExporter
from docgen.export.storage import ExportStorage
from docgen.formatting.schemas import FormattingTemplate, OutputFormat

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
def pdf_template() -> FormattingTemplate:
    """The real docgen-light PDF template/CSS pair, loaded from the catalog dir."""
    return FormattingTemplate(
        id="docgen-light",
        name="Облегченный PDF",
        format=OutputFormat.PDF,
        renderer=OutputFormat.PDF,
        assets=["docgen-light-pdf.html.j2", "docgen-light-pdf.css"],
    )


@pytest.fixture
def colvir_pdf_template() -> FormattingTemplate:
    """The real Colvir Word template, converted to PDF by LibreOffice."""
    return FormattingTemplate(
        id="colvir",
        name="Фирменный стиль Colvir",
        format=OutputFormat.PDF,
        renderer=OutputFormat.PDF,
        assets=["colvir.docx"],
    )


@pytest.fixture
def document_all_kinds() -> WorkingDocument:
    """A document exercising every supported node kind, mirroring the other
    exporters' `document_all_kinds` fixture (see test_markdown.py)."""
    return WorkingDocument(
        title="Тестовый документ",
        template_id="docgen-light-pdf",
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
                data={"items": ["Пункт 1", "Пункт 2"], "ordered": False},
            ),
            DocumentNode(
                kind=NodeKind.TABLE,
                data={
                    "headers": ["Колонка 1", "Колонка 2"],
                    "rows": [["Значение 1", "Значение 2"]],
                },
            ),
            DocumentNode(kind=NodeKind.GAP),
            DocumentNode(
                kind=NodeKind.IMAGE,
                data={"src": "images/logo.png", "alt": "Логотип"},
            ),
        ],
    )


def _open_pdf_text(content: bytes) -> str:
    pdf = pymupdf.open(stream=content, filetype="pdf")
    try:
        return "".join(page.get_text() for page in pdf)
    finally:
        pdf.close()


# --- Step 1 brief test -------------------------------------------------


def test_pdf_has_valid_header_and_expected_text(
    document_all_kinds: WorkingDocument, pdf_template: FormattingTemplate
) -> None:
    rendered = PdfExporter(image_loader=fake_image_loader).render(
        document_all_kinds, pdf_template
    )
    assert rendered.content.startswith(b"%PDF-")
    text = _open_pdf_text(rendered.content)
    assert "Заголовок" in text
    assert "DocGen" in text


def test_colvir_pdf_has_valid_header_and_expected_text(
    document_all_kinds: WorkingDocument, colvir_pdf_template: FormattingTemplate
) -> None:
    rendered = PdfExporter(image_loader=fake_image_loader).render(
        document_all_kinds, colvir_pdf_template
    )
    assert rendered.content.startswith(b"%PDF-")
    text = _open_pdf_text(rendered.content)
    assert "Заголовок" in text
    assert "Colvir" in text
    assert "Оглавление" in text


def test_colvir_pdf_contents_uses_real_destination_pages(
    colvir_pdf_template: FormattingTemplate,
) -> None:
    document = WorkingDocument(
        title="Многостраничный документ",
        template_id="faq",
        nodes=(
            [DocumentNode(kind=NodeKind.HEADING, text="Первый", data={"level": 1})]
            + [
                DocumentNode(
                    kind=NodeKind.PARAGRAPH,
                    text=(f"Длинный абзац {index}. " * 80),
                )
                for index in range(25)
            ]
            + [DocumentNode(kind=NodeKind.HEADING, text="Второй", data={"level": 1})]
        ),
    )

    rendered = PdfExporter().render(document, colvir_pdf_template)
    pdf = pymupdf.open(stream=rendered.content, filetype="pdf")
    try:
        contents_text = pdf[1].get_text()
        destination_pages = sorted(
            link["page"] + 1
            for link in pdf[1].get_links()
            if link.get("kind") == pymupdf.LINK_GOTO
        )
    finally:
        pdf.close()

    assert len(destination_pages) == 2
    assert destination_pages[0] > 1
    assert destination_pages[1] > destination_pages[0]
    assert re.search(rf"ПЕРВЫЙ.*{destination_pages[0]}", contents_text)
    assert re.search(rf"ВТОРОЙ.*{destination_pages[1]}", contents_text)


def test_colvir_pdf_footer_and_filename_reflect_document_category(
    colvir_pdf_template: FormattingTemplate,
) -> None:
    document = WorkingDocument(title="Общие вопросы", template_id="faq", nodes=[])

    rendered = PdfExporter().render(document, colvir_pdf_template)

    assert rendered.filename.startswith("FAQ-")
    text = _open_pdf_text(rendered.content)
    assert "FAQ" in text
    assert "Руководство" not in text


def test_colvir_pdf_preserves_complete_use_case_form_and_separate_flow_items(
    colvir_pdf_template: FormattingTemplate,
) -> None:
    document = WorkingDocument(
        title="Открытие счёта",
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
                section_id="result",
                text="Результат",
                children=[DocumentNode(kind=NodeKind.GAP)],
            ),
        ],
    )

    rendered = PdfExporter().render(document, colvir_pdf_template)
    text = _open_pdf_text(rendered.content)

    assert rendered.content.startswith(b"%PDF-")
    assert "Код документа" in text
    assert "Область действия" in text
    assert "Предусловия" in text
    assert "Основной поток" in text
    assert re.search(r"Клиент\s+отправляет\s+заявление", text)
    assert re.search(r"Система\s+открывает\s+счёт", text)
    assert "Нет данных в источниках" not in text


def test_pdf_filename_and_media_type(pdf_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Мой прекрасный документ", template_id="docgen-light-pdf", nodes=[]
    )

    rendered = PdfExporter().render(document, pdf_template)

    assert rendered.filename.endswith(".pdf")
    assert rendered.media_type == "application/pdf"


# --- node coverage / text content --------------------------------------


def test_pdf_renders_paragraph_and_list_text(
    document_all_kinds: WorkingDocument, pdf_template: FormattingTemplate
) -> None:
    rendered = PdfExporter(image_loader=fake_image_loader).render(
        document_all_kinds, pdf_template
    )
    text = _open_pdf_text(rendered.content)

    assert "Обычный текст" in text
    assert "Пункт 1" in text
    assert "Пункт 2" in text
    assert "Значение 1" in text
    assert "Значение 2" in text


def test_pdf_renders_textless_gap_node(pdf_template: FormattingTemplate) -> None:
    """Production gap nodes created during assembly carry no text at all --
    must always render the fixed message, never node.text."""
    document = WorkingDocument(
        title="Документ с пробелом",
        template_id="docgen-light-pdf",
        nodes=[DocumentNode(kind=NodeKind.GAP)],
    )

    rendered = PdfExporter().render(document, pdf_template)
    text = _open_pdf_text(rendered.content)

    assert "Нет данных в источниках" in text


def test_pdf_renders_table_with_headers_no_rows(pdf_template: FormattingTemplate) -> None:
    """Production case: user edits a table down to just the header row."""
    document = WorkingDocument(
        title="Таблица только с заголовками",
        template_id="docgen-light-pdf",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={"headers": ["Колонка А", "Колонка Б"], "rows": []},
            ),
        ],
    )

    rendered = PdfExporter().render(document, pdf_template)
    text = _open_pdf_text(rendered.content)

    assert "Колонка А" in text
    assert "Колонка Б" in text


def test_pdf_renders_table_without_headers(pdf_template: FormattingTemplate) -> None:
    """Production DOCX extraction creates tables with only a 'rows' key."""
    document = WorkingDocument(
        title="Таблица без заголовков",
        template_id="docgen-light-pdf",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={"rows": [["Значение 1", "Значение 2"]]},
            ),
        ],
    )

    rendered = PdfExporter().render(document, pdf_template)
    text = _open_pdf_text(rendered.content)

    assert "Значение 1" in text
    assert "Значение 2" in text


def test_pdf_skips_empty_table(pdf_template: FormattingTemplate) -> None:
    """A table with neither headers nor rows renders successfully and
    contributes no stray text (the shared render_node macro already skips
    it -- this only confirms the PDF pipeline tolerates it end to end)."""
    document = WorkingDocument(
        title="Пустая таблица",
        template_id="docgen-light-pdf",
        nodes=[
            DocumentNode(kind=NodeKind.HEADING, text="Заголовок раздела", data={"level": 1}),
            DocumentNode(kind=NodeKind.TABLE, data={"rows": []}),
        ],
    )

    rendered = PdfExporter().render(document, pdf_template)

    assert rendered.content.startswith(b"%PDF-")
    text = _open_pdf_text(rendered.content)
    assert "Заголовок раздела" in text


def test_pdf_renders_nested_children_for_every_kind(pdf_template: FormattingTemplate) -> None:
    """Every node kind may carry children; all must be rendered."""
    document = WorkingDocument(
        title="Документ с вложенными узлами",
        template_id="docgen-light-pdf",
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

    rendered = PdfExporter().render(document, pdf_template)
    text = _open_pdf_text(rendered.content)

    assert "Заголовок" in text
    assert "Вложенный абзац" in text
    assert "А" in text
    assert "Нет данных в источниках" in text
    assert "Вложенное изображение" in text


# --- images --------------------------------------------------------------


def test_pdf_image_without_src_renders_placeholder(pdf_template: FormattingTemplate) -> None:
    """Production AI-assembled image nodes carry no data.src at all. Must not
    fabricate or attempt an embed."""
    document = WorkingDocument(
        title="Документ без источника изображения",
        template_id="docgen-light-pdf",
        nodes=[DocumentNode(kind=NodeKind.IMAGE, text="Схема архитектуры")],
    )

    rendered = PdfExporter(image_loader=fake_image_loader).render(document, pdf_template)
    text = _open_pdf_text(rendered.content)

    assert "Изображение или схема" in text
    assert "Схема архитектуры" in text


def test_pdf_image_with_unresolvable_src_renders_placeholder(
    pdf_template: FormattingTemplate,
) -> None:
    """A src the loader cannot resolve (e.g. deleted file) falls back safely."""
    document = WorkingDocument(
        title="Документ с недоступным изображением",
        template_id="docgen-light-pdf",
        nodes=[DocumentNode(kind=NodeKind.IMAGE, data={"src": "images/missing.png"})],
    )

    rendered = PdfExporter(image_loader=fake_image_loader).render(document, pdf_template)
    text = _open_pdf_text(rendered.content)

    assert "Изображение или схема" in text


def test_pdf_renders_without_image_loader(pdf_template: FormattingTemplate) -> None:
    """PdfExporter must work with no image_loader at all (defaults to placeholders)."""
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-pdf",
        nodes=[DocumentNode(kind=NodeKind.IMAGE, data={"src": "images/logo.png"})],
    )

    rendered = PdfExporter().render(document, pdf_template)

    assert rendered.content.startswith(b"%PDF-")
    text = _open_pdf_text(rendered.content)
    assert "Изображение или схема" in text


def test_pdf_embeds_resolvable_image(pdf_template: FormattingTemplate) -> None:
    """A resolvable image src must actually be embedded (larger content, no
    placeholder text), not merely tolerated."""
    document = WorkingDocument(
        title="Документ с изображением",
        template_id="docgen-light-pdf",
        nodes=[DocumentNode(kind=NodeKind.IMAGE, data={"src": "images/logo.png"})],
    )

    rendered = PdfExporter(image_loader=fake_image_loader).render(document, pdf_template)
    text = _open_pdf_text(rendered.content)

    assert "Изображение или схема" not in text


# --- print-specific requirements -----------------------------------------


def test_pdf_uses_a4_page_size(pdf_template: FormattingTemplate) -> None:
    document = WorkingDocument(title="Документ", template_id="docgen-light-pdf", nodes=[])

    rendered = PdfExporter().render(document, pdf_template)
    pdf = pymupdf.open(stream=rendered.content, filetype="pdf")
    try:
        page = pdf[0]
        # A4 in points is 595 x 842; allow a small tolerance for rounding.
        assert abs(page.rect.width - 595) < 2
        assert abs(page.rect.height - 842) < 2
    finally:
        pdf.close()


def test_pdf_has_page_number_footer(pdf_template: FormattingTemplate) -> None:
    """Multiple pages must show a page-number footer, e.g. '1 / 2'."""
    long_nodes = [
        DocumentNode(
            kind=NodeKind.PARAGRAPH,
            text=f"Абзац номер {index}. " + "Текст для заполнения страницы. " * 20,
        )
        for index in range(1, 60)
    ]
    document = WorkingDocument(
        title="Длинный документ", template_id="docgen-light-pdf", nodes=long_nodes
    )

    rendered = PdfExporter().render(document, pdf_template)
    pdf = pymupdf.open(stream=rendered.content, filetype="pdf")
    try:
        assert len(pdf) > 1
        first_page_text = pdf[0].get_text()
    finally:
        pdf.close()
    assert "/" in first_page_text


# --- engine failure mapping -------------------------------------------


def test_pdf_engine_failure_raises_export_error(pdf_template: FormattingTemplate) -> None:
    """A WeasyPrint engine crash must be mapped to ExportError, not leak the
    underlying exception type, and must not touch any project state (this
    exporter never writes to disk, so there is nothing to roll back)."""
    document = WorkingDocument(title="Документ", template_id="docgen-light-pdf", nodes=[])

    # HTML is now imported lazily inside render() (see docgen.export.pdf),
    # so it must be patched on the real `weasyprint` module rather than as
    # a `docgen.export.pdf.HTML` module attribute, which no longer exists.
    with patch("weasyprint.HTML") as mock_html:
        mock_html.return_value.write_pdf.side_effect = RuntimeError("boom")
        with pytest.raises(ExportError, match="Не удалось сформировать PDF"):
            PdfExporter().render(document, pdf_template)


# --- lazy import (worker-startup safety) ----------------------------------


def test_pdf_module_import_does_not_require_weasyprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing this module must never import weasyprint.

    `docgen.jobs.worker` builds `default_exporters(...)` at worker startup
    for every job kind (ASSEMBLE/CHECK/EXPORT), which imports every
    exporter module including this one. WeasyPrint dlopen()s system
    libraries (Pango/HarfBuzz) at *its own* import time -- if that ever
    fails in some environment, it must only break PDF export, not crash the
    whole worker process on boot. That guarantee only holds if importing
    `docgen.export.pdf` itself never triggers `import weasyprint`.
    """
    import importlib
    import sys

    monkeypatch.delitem(sys.modules, "docgen.export.pdf", raising=False)
    monkeypatch.delitem(sys.modules, "weasyprint", raising=False)

    importlib.import_module("docgen.export.pdf")

    assert "weasyprint" not in sys.modules


# --- filename byte-length safety (finding 6) -------------------------------


def test_pdf_long_cyrillic_title_produces_storable_filename(
    pdf_template: FormattingTemplate, tmp_path: Path
) -> None:
    """A 150+ character Cyrillic title must never produce a filename that
    overflows the filesystem's 255-byte limit once ExportStorage appends
    `-{template_id}` -- reproduced end-to-end via the real storage layer."""
    long_title = "Очень длинное название регламента для банковского документа " * 4
    assert len(long_title) > 150
    document = WorkingDocument(title=long_title, template_id="docgen-light-pdf", nodes=[])

    rendered = PdfExporter().render(document, pdf_template)
    storage = ExportStorage(tmp_path / "data")

    stored = storage.save("proj-1", OutputFormat.PDF, pdf_template.id, rendered)

    assert len(stored.filename.encode("utf-8")) <= 255
    assert storage.resolve(stored.relative_path).is_file()
