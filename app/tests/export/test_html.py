import base64
from io import BytesIO
from pathlib import Path

import pytest

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.export.html import HtmlExporter, local_storage_image_loader
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
def html_template() -> FormattingTemplate:
    """The real docgen-light HTML template/CSS pair, loaded from the catalog dir."""
    return FormattingTemplate(
        id="docgen-light",
        name="Облегченный HTML",
        format=OutputFormat.HTML,
        renderer=OutputFormat.HTML,
        assets=["docgen-light.html.j2", "docgen-light.css"],
    )


@pytest.fixture
def document_with_image() -> WorkingDocument:
    """A document combining an XSS-shaped text node with a resolvable image.

    Exercises both requirements of the brief's safety/completeness test in
    one document: text must be escaped, and images with a resolvable src
    must be embedded as data: URLs.
    """
    return WorkingDocument(
        title="Документ с изображением",
        template_id="docgen-light-html",
        nodes=[
            DocumentNode(
                kind=NodeKind.PARAGRAPH,
                text="<script>alert(1)</script>",
            ),
            DocumentNode(
                kind=NodeKind.IMAGE,
                data={"src": "images/logo.png", "alt": "Логотип"},
            ),
        ],
    )


def test_html_is_standalone_and_escaped(
    document_with_image: WorkingDocument, html_template: FormattingTemplate
) -> None:
    rendered = HtmlExporter(image_loader=fake_image_loader).render(
        document_with_image, html_template
    )
    html = rendered.content.decode("utf-8")
    assert "<style>" in html
    assert "data:image/png;base64," in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_html_image_without_src_renders_placeholder(
    html_template: FormattingTemplate,
) -> None:
    """Production AI-assembled image nodes carry no data.src at all.

    Must not fabricate or attempt an embed; must render the same
    placeholder treatment the app's own result.html view already uses.
    """
    document = WorkingDocument(
        title="Документ без источника изображения",
        template_id="docgen-light-html",
        nodes=[
            DocumentNode(
                kind=NodeKind.IMAGE,
                text="Схема архитектуры",
            ),
        ],
    )

    rendered = HtmlExporter(image_loader=fake_image_loader).render(document, html_template)
    html = rendered.content.decode("utf-8")

    assert "data:image" not in html
    assert "Изображение или схема" in html
    assert "Схема архитектуры" in html


def test_html_image_with_unresolvable_src_renders_placeholder(
    html_template: FormattingTemplate,
) -> None:
    """A src the loader cannot resolve (e.g. deleted file) falls back safely."""
    document = WorkingDocument(
        title="Документ с недоступным изображением",
        template_id="docgen-light-html",
        nodes=[
            DocumentNode(
                kind=NodeKind.IMAGE,
                data={"src": "images/missing.png"},
            ),
        ],
    )

    rendered = HtmlExporter(image_loader=fake_image_loader).render(document, html_template)
    html = rendered.content.decode("utf-8")

    assert "data:image" not in html
    assert "Изображение или схема" in html


def test_html_renders_without_image_loader(html_template: FormattingTemplate) -> None:
    """HtmlExporter must work with no image_loader at all (defaults to placeholders)."""
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-html",
        nodes=[
            DocumentNode(kind=NodeKind.IMAGE, data={"src": "images/logo.png"}),
        ],
    )

    rendered = HtmlExporter().render(document, html_template)
    html = rendered.content.decode("utf-8")

    assert "data:image" not in html
    assert "Изображение или схема" in html


def test_html_renders_table_with_headers_no_rows(html_template: FormattingTemplate) -> None:
    """Production case: user edits a table down to just the header row."""
    document = WorkingDocument(
        title="Таблица только с заголовками",
        template_id="docgen-light-html",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={"headers": ["Колонка А", "Колонка Б"], "rows": []},
            ),
        ],
    )

    rendered = HtmlExporter().render(document, html_template)
    html = rendered.content.decode("utf-8")

    assert "<table>" in html
    assert "<th>Колонка А</th>" in html
    assert "<th>Колонка Б</th>" in html


def test_html_renders_table_without_headers(html_template: FormattingTemplate) -> None:
    """Production DOCX extraction creates tables with only a 'rows' key."""
    document = WorkingDocument(
        title="Таблица без заголовков",
        template_id="docgen-light-html",
        nodes=[
            DocumentNode(
                kind=NodeKind.TABLE,
                data={"rows": [["Значение 1", "Значение 2"]]},
            ),
        ],
    )

    rendered = HtmlExporter().render(document, html_template)
    html = rendered.content.decode("utf-8")

    assert "<thead>" not in html
    assert "<td>Значение 1</td>" in html
    assert "<td>Значение 2</td>" in html


def test_html_skips_empty_table(html_template: FormattingTemplate) -> None:
    """A table with neither headers nor rows contributes nothing to the body."""
    document = WorkingDocument(
        title="Пустая таблица",
        template_id="docgen-light-html",
        nodes=[
            DocumentNode(kind=NodeKind.TABLE, data={"rows": []}),
        ],
    )

    rendered = HtmlExporter().render(document, html_template)
    html = rendered.content.decode("utf-8")

    assert "<table>" not in html


def test_html_renders_textless_gap_node(html_template: FormattingTemplate) -> None:
    """Production gap nodes created during assembly have no text attribute."""
    document = WorkingDocument(
        title="Документ с пробелом",
        template_id="docgen-light-html",
        nodes=[DocumentNode(kind=NodeKind.GAP)],
    )

    rendered = HtmlExporter().render(document, html_template)
    html = rendered.content.decode("utf-8")

    assert "Нет данных в источниках" in html
    assert '<aside class="dg-gap">' in html


def test_html_renders_nested_children_for_every_kind(
    html_template: FormattingTemplate,
) -> None:
    """Every node kind may carry children; all must be rendered."""
    document = WorkingDocument(
        title="Документ с вложенными узлами",
        template_id="docgen-light-html",
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

    rendered = HtmlExporter().render(document, html_template)
    html = rendered.content.decode("utf-8")

    assert "Заголовок" in html
    assert "Вложенный абзац" in html
    assert "<li>А</li>" in html
    assert "<td>1</td>" in html
    assert "Нет данных в источниках" in html
    assert "Вложенное изображение" in html


def test_html_heading_level_produces_semantic_tag(html_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-html",
        nodes=[
            DocumentNode(kind=NodeKind.HEADING, text="Раздел", data={"level": 3}),
        ],
    )

    rendered = HtmlExporter().render(document, html_template)
    html = rendered.content.decode("utf-8")

    assert "<h3>Раздел</h3>" in html


def test_html_heading_level_is_clamped(html_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-html",
        nodes=[
            DocumentNode(kind=NodeKind.HEADING, text="Слишком глубоко", data={"level": 99}),
        ],
    )

    rendered = HtmlExporter().render(document, html_template)
    html = rendered.content.decode("utf-8")

    assert "<h6>Слишком глубоко</h6>" in html


def test_html_ordered_list_renders_ol(html_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-html",
        nodes=[
            DocumentNode(
                kind=NodeKind.LIST,
                data={"items": ["Первый", "Второй"], "ordered": True},
            ),
        ],
    )

    rendered = HtmlExporter().render(document, html_template)
    html = rendered.content.decode("utf-8")

    assert "<ol>" in html
    assert "<li>Первый</li>" in html


def test_html_title_and_meta_charset(
    document_with_image: WorkingDocument, html_template: FormattingTemplate
) -> None:
    rendered = HtmlExporter().render(document_with_image, html_template)
    html = rendered.content.decode("utf-8")

    assert '<meta charset="utf-8">' in html
    assert "<title>Документ с изображением</title>" in html


def test_html_has_no_external_resources(
    document_with_image: WorkingDocument, html_template: FormattingTemplate
) -> None:
    rendered = HtmlExporter(image_loader=fake_image_loader).render(
        document_with_image, html_template
    )
    html = rendered.content.decode("utf-8")

    assert "http://" not in html
    assert "https://" not in html
    assert "<link" not in html


def test_html_filename_and_media_type(html_template: FormattingTemplate) -> None:
    document = WorkingDocument(
        title="Мой прекрасный документ",
        template_id="docgen-light-html",
        nodes=[],
    )

    rendered = HtmlExporter().render(document, html_template)

    assert rendered.filename.endswith(".html")
    assert rendered.media_type == "text/html"


def test_html_css_is_embedded_unescaped(html_template: FormattingTemplate) -> None:
    """The <style> block is a raw-text element: browsers do not decode HTML
    entities inside it, so the CSS asset must be inserted verbatim (marked
    safe), not run through autoescape like ordinary node text.

    Regression test: an earlier version escaped the CSS asset's own
    apostrophe into "&#39;", which would have silently corrupted any CSS
    value containing a quote or apostrophe (e.g. quoted font-family names).
    """
    document = WorkingDocument(title="Документ", template_id="docgen-light-html", nodes=[])

    rendered = HtmlExporter().render(document, html_template)
    html = rendered.content.decode("utf-8")

    assert "&#39;" not in html
    assert "&quot;" not in html


def test_html_uses_design_constraint_colors(html_template: FormattingTemplate) -> None:
    """CSS asset must use the exact DocGen Light colors from the plan."""
    document = WorkingDocument(title="Документ", template_id="docgen-light-html", nodes=[])

    rendered = HtmlExporter().render(document, html_template)
    html = rendered.content.decode("utf-8")

    for color in ("#ffffff", "#f5f5f7", "#1d1d1f", "#707070", "#0071e3"):
        assert color in html
    assert "960px" in html
    assert "Inter, system-ui, sans-serif" in html


# --- local_storage_image_loader (the production ImageLoader implementation) ---


def test_local_storage_image_loader_resolves_stored_image(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    saved = storage.save("project-1", "source-1", "logo.png", BytesIO(_PNG_BYTES))
    loader = local_storage_image_loader(storage)

    result = loader(saved.relative_path)

    assert result is not None
    content, media_type = result
    assert content == _PNG_BYTES
    assert media_type == "image/png"


def test_local_storage_image_loader_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    loader = local_storage_image_loader(storage)

    assert loader("../../etc/passwd") is None


def test_local_storage_image_loader_returns_none_for_missing_file(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    loader = local_storage_image_loader(storage)

    assert loader("projects/does-not/exist.png") is None


def test_local_storage_image_loader_rejects_non_image_extension(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    saved = storage.save("project-1", "source-1", "notes.txt", BytesIO(b"hello"))
    loader = local_storage_image_loader(storage)

    assert loader(saved.relative_path) is None


# --- filename byte-length safety (finding 6) -------------------------------


def test_html_long_cyrillic_title_produces_storable_filename(
    html_template: FormattingTemplate, tmp_path: Path
) -> None:
    """A 150+ character Cyrillic title must never produce a filename that
    overflows the filesystem's 255-byte limit once ExportStorage appends
    `-{template_id}` -- reproduced end-to-end via the real storage layer."""
    long_title = "Очень длинное название регламента для банковского документа " * 4
    assert len(long_title) > 150
    document = WorkingDocument(title=long_title, template_id="docgen-light", nodes=[])

    rendered = HtmlExporter().render(document, html_template)
    storage = ExportStorage(tmp_path / "data")

    stored = storage.save("proj-1", OutputFormat.HTML, html_template.id, rendered)

    assert len(stored.filename.encode("utf-8")) <= 255
    assert storage.resolve(stored.relative_path).is_file()
