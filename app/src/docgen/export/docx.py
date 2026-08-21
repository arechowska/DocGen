"""DOCX exporter for rendering WorkingDocuments as styled Word files.

Renders onto a fresh in-memory copy of the real Colvir corporate template
(``formatting/templates/colvir.docx``, built by
``tools/build_default_docx_template.py`` from the user-supplied
``colvir_v3.dotx``). Every render loads the base asset from disk and never
writes back to it -- the shared template file is never mutated in place.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Any

import docx
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Pt

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.export._naming import make_safe_filename
from docgen.export.html import ImageAsset, ImageLoader, local_storage_image_loader
from docgen.export.protocol import RenderedFile
from docgen.formatting.schemas import FormattingTemplate

__all__ = ["DocxExporter", "ImageAsset", "ImageLoader", "local_storage_image_loader"]

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "formatting" / "templates"

_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Fixed conventions shared with markdown.py/html.py: gap nodes never carry
# real text in production (assembled from missing-source detection), and the
# image placeholder text mirrors the HTML template's wording exactly so the
# three formats read consistently.
_GAP_MESSAGE = "Нет данных в источниках"
_IMAGE_PLACEHOLDER_MESSAGE = "Изображение или схема"

# Style names are the real Colvir corporate styles inventoried from
# colvir_v3.dotx's styles.xml (see task-4-report.md for the full mapping
# table and the rationale for each choice).
_HEADING_STYLE_NAMES = {
    1: "Heading 1",
    2: "Heading 2",
    3: "Heading 3",
    4: "Heading 4",
    5: "Heading 5",
    6: "Heading 6",
}
_TITLE_STYLE = "Colvir_Обложка_Название"
_PARAGRAPH_STYLE = "Colvir_Абзац"
_SECTION_STYLE = "Colvir_Подзаголовок"
_LIST_STYLE = "Colvir_Стиль_М1"
_GAP_STYLE = "Colvir_Внимание"
_CAPTION_STYLE = "Colvir_Рисунок_Подпись"
_TABLE_STYLE = "Colvir_сетка_таблицы"
_TABLE_HEADER_CELL_STYLE = "Colvir_Таблица_заголовок"
_TABLE_BODY_CELL_STYLE = "Colvir_Таблица_текст"

_LIST_INDENT_LEFT_TWIPS = 720
_LIST_INDENT_HANGING_TWIPS = 360
_FAQ_ITEM_PATTERN = re.compile(
    r"^\s*Вопрос\s*:\s*(?P<question>.+?)\s+Ответ\s*:\s*(?P<answer>.+?)\s*$",
    re.DOTALL,
)


class DocxExporter:
    """Exports WorkingDocuments to styled Word (.docx) files.

    Loads a fresh in-memory copy of the template's base `.docx` asset for
    every render and appends the document's nodes to it using the Colvir
    corporate paragraph/table/list styles. The template's existing
    header/footer, numbering, and styles are preserved untouched; only the
    body's sample/placeholder content is cleared before rendering.
    """

    def __init__(
        self,
        image_loader: ImageLoader | None = None,
        templates_dir: Path | None = None,
    ) -> None:
        """Create a DocxExporter.

        Args:
            image_loader: Resolves a node's image src to bytes + MIME type.
                When None (the default), all images render as placeholders.
            templates_dir: Directory containing the `.docx` assets named by
                a FormattingTemplate's `assets` list. Defaults to the
                built-in formatting/templates catalog directory.
        """
        self._image_loader = image_loader
        self._templates_dir = (templates_dir or _TEMPLATES_DIR).resolve()

    def render(
        self, document: WorkingDocument, template: FormattingTemplate
    ) -> RenderedFile:
        """Render a document to a styled DOCX file.

        Args:
            document: The WorkingDocument to render.
            template: The FormattingTemplate naming the `.docx` base asset
                to load.

        Returns:
            RenderedFile with the DOCX content.
        """
        asset_name = self._asset_named(template, ".docx")
        base_bytes = self._read_asset_bytes(asset_name)

        docx_document = docx.Document(BytesIO(base_bytes))
        self._prepare_template_body(docx_document, _cover_title(document.title))
        self._prepare_headers(docx_document, _cover_title(document.title))
        docx_document.core_properties.title = document.title
        self._render_contents(docx_document, document)
        docx_document.add_page_break()

        list_num_ids: dict[bool, int] = {}
        for node in document.nodes:
            self._render_node(docx_document, node, list_num_ids)

        buffer = BytesIO()
        docx_document.save(buffer)

        return RenderedFile(
            filename=make_safe_filename(
                document.title, ".docx", reserved_suffix=f"-{template.id}"
            ),
            media_type=_MEDIA_TYPE,
            content=buffer.getvalue(),
        )

    # --- asset loading -----------------------------------------------

    def _asset_named(self, template: FormattingTemplate, suffix: str) -> str:
        """Find the first declared asset ending with `suffix`.

        Only assets listed on the template are ever loaded -- this exporter
        never reads an arbitrary path, only ones the catalog already
        validated for this template.
        """
        matches = [asset for asset in template.assets if asset.endswith(suffix)]
        if not matches:
            raise ValueError(
                f"Шаблон {template.id} не содержит ассет с суффиксом {suffix}"
            )
        return matches[0]

    def _read_asset_bytes(self, name: str) -> bytes:
        """Read a catalog-declared asset file, enforcing containment."""
        path = (self._templates_dir / name).resolve()
        if not path.is_relative_to(self._templates_dir):
            raise ValueError("Недопустимый путь ассета")
        return path.read_bytes()

    # --- body setup ----------------------------------------------------

    def _prepare_template_body(
        self, docx_document: docx.document.Document, title: str
    ) -> None:
        """Retain the corporate cover and remove only template sample content.

        The Colvir template is a designed document, not a blank style
        container. Its first cover paragraphs establish the visual identity;
        the remainder is demonstration text and a sample table of contents.
        Keeping the cover lets the exported DOCX preserve the supplied layout,
        page setup, header and footer rather than recreating an approximation.
        """
        paragraphs = list(docx_document.paragraphs)
        title_index = next(
            (
                index
                for index, paragraph in enumerate(paragraphs)
                if paragraph.style.name == _TITLE_STYLE
            ),
            None,
        )
        if title_index is None:
            raise ValueError("В шаблоне Colvir не найден стиль названия обложки")

        # The supplied template's cover consists of two title lines, the
        # title itself, a subtitle and an introductory paragraph.
        cover_paragraphs = paragraphs[: title_index + 3]
        cover_elements = set()
        for paragraph in cover_paragraphs:
            cover_elements.add(paragraph._p)
            paragraph.text = title if paragraph.style.name == _TITLE_STYLE else ""

        body = docx_document.element.body
        sect_pr = body.find(qn("w:sectPr"))
        for child in list(body):
            if child is not sect_pr and child not in cover_elements:
                body.remove(child)

    def _prepare_headers(
        self, docx_document: docx.document.Document, title: str
    ) -> None:
        """Keep the template header inside the printable area for PDF export."""
        for section in docx_document.sections:
            for paragraph in section.header.paragraphs:
                if "Colvir Banking System" not in paragraph.text:
                    continue
                paragraph.paragraph_format.tab_stops.clear_all()
                paragraph.paragraph_format.tab_stops.add_tab_stop(
                    section.page_width - section.left_margin - section.right_margin,
                    WD_TAB_ALIGNMENT.RIGHT,
                )
                paragraph.text = f"Colvir Banking System\t{title}"

    def _render_contents(
        self, docx_document: docx.document.Document, document: WorkingDocument
    ) -> None:
        """Fill the template's dedicated post-cover page with a contents list.

        A Word TOC field relies on a client-side field update and therefore
        becomes empty in automated LibreOffice conversion. Building the entries
        from the same document nodes makes Word and PDF deterministic.
        """
        title = docx_document.add_paragraph("Оглавление", style=_SECTION_STYLE)
        title.paragraph_format.page_break_before = False

        for heading, level in self._iter_headings(document.nodes):
            entry = docx_document.add_paragraph(heading, style=_PARAGRAPH_STYLE)
            entry.paragraph_format.left_indent = Cm(0.6 * max(0, level - 1))

    def _iter_headings(
        self, nodes: list[DocumentNode]
    ) -> Iterator[tuple[str, int]]:
        for node in nodes:
            if node.kind is NodeKind.HEADING and node.text:
                level = node.data.get("level", 1)
                yield node.text, level if isinstance(level, int) else 1
            yield from self._iter_headings(node.children)

    # --- node dispatch ---------------------------------------------------

    def _render_node(
        self,
        docx_document: docx.document.Document,
        node: DocumentNode,
        list_num_ids: dict[bool, int],
    ) -> None:
        """Render a single node and then all of its children.

        Word documents are a flat sequence of block-level elements, so
        (unlike the HTML exporter's nested DOM) children are appended in
        document order immediately after their parent's own content --
        every node kind may carry children, and all must render.
        """
        if node.kind == NodeKind.HEADING:
            self._render_heading(docx_document, node)
        elif node.kind == NodeKind.PARAGRAPH:
            self._render_paragraph(docx_document, node)
        elif node.kind == NodeKind.LIST:
            self._render_list(docx_document, node, list_num_ids)
        elif node.kind == NodeKind.TABLE:
            self._render_table(docx_document, node)
        elif node.kind == NodeKind.IMAGE:
            self._render_image(docx_document, node)
        elif node.kind == NodeKind.GAP:
            self._render_gap(docx_document, node)

        for child in node.children:
            self._render_node(docx_document, child, list_num_ids)

    def _render_heading(
        self, docx_document: docx.document.Document, node: DocumentNode
    ) -> None:
        level = node.data.get("level", 1)
        if not isinstance(level, int):
            level = 1
        level = max(1, min(6, level))
        # FAQ sections are authored as h2 blocks in the editor. The template's
        # Heading 2 carries outline numbering, while its corporate subtitle is
        # the blue, unnumbered visual used by the supplied FAQ layout.
        style = _SECTION_STYLE if level == 2 else _HEADING_STYLE_NAMES[level]
        paragraph = docx_document.add_paragraph(node.text or "", style=style)
        if level == 2:
            # The cover uses this style as a page-level block. FAQ sections
            # need the same typography without starting a blank page first.
            paragraph.paragraph_format.page_break_before = False

    def _render_paragraph(
        self, docx_document: docx.document.Document, node: DocumentNode
    ) -> None:
        paragraph = docx_document.add_paragraph(node.text or "", style=_PARAGRAPH_STYLE)
        self._apply_node_style(paragraph, node)

    def _render_list(
        self,
        docx_document: docx.document.Document,
        node: DocumentNode,
        list_num_ids: dict[bool, int],
    ) -> None:
        items = node.data.get("items", [])
        if not isinstance(items, list):
            items = []
        if not items:
            return
        if node.text:
            section = docx_document.add_paragraph(node.text, style=_SECTION_STYLE)
            self._apply_node_style(section, node)
        ordered = bool(node.data.get("ordered", False))
        num_id = self._ensure_list_num_id(docx_document, ordered, list_num_ids)
        item_styles = node.data.get("item_styles")
        for index, item in enumerate(items):
            text = item if isinstance(item, str) else str(item)
            faq_item = _FAQ_ITEM_PATTERN.match(text)
            if faq_item is not None:
                self._render_faq_item(docx_document, faq_item)
                continue
            paragraph = docx_document.add_paragraph(text, style=_LIST_STYLE)
            self._apply_node_style(paragraph, node)
            if isinstance(item_styles, list) and index < len(item_styles):
                self._apply_style_attribute(paragraph, item_styles[index])
            self._apply_list_numbering(paragraph, num_id)

    def _render_faq_item(
        self, docx_document: docx.document.Document, match: re.Match[str]
    ) -> None:
        for label, value in (("Вопрос", match.group("question")), ("Ответ", match.group("answer"))):
            paragraph = docx_document.add_paragraph(style=_PARAGRAPH_STYLE)
            paragraph.add_run(f"{label}: ").bold = True
            paragraph.add_run(value)
            paragraph.paragraph_format.space_after = Pt(8 if label == "Вопрос" else 14)

    def _render_table(
        self, docx_document: docx.document.Document, node: DocumentNode
    ) -> None:
        headers = node.data.get("headers", [])
        rows = node.data.get("rows", [])
        if not isinstance(headers, list):
            headers = []
        if not isinstance(rows, list):
            rows = []

        # Only skip rendering if both headers and rows are empty, matching
        # the markdown/html exporters' convention.
        if not headers and not rows:
            return

        if headers:
            col_count = len(headers)
        elif rows and isinstance(rows[0], list) and rows[0]:
            col_count = len(rows[0])
        else:
            col_count = 1

        total_rows = (1 if headers else 0) + len(rows)
        table = docx_document.add_table(rows=total_rows, cols=col_count)
        table.style = _TABLE_STYLE

        row_index = 0
        if headers:
            self._mark_header_row(table.rows[0])
            for col_index in range(col_count):
                text = headers[col_index] if col_index < len(headers) else ""
                self._set_cell_text(
                    table.cell(0, col_index), text, _TABLE_HEADER_CELL_STYLE
                )
            row_index = 1

        for row in rows:
            row_values = row if isinstance(row, list) else []
            padded = list(row_values) + [""] * (col_count - len(row_values))
            padded = padded[:col_count]
            for col_index, value in enumerate(padded):
                self._set_cell_text(
                    table.cell(row_index, col_index), value, _TABLE_BODY_CELL_STYLE
                )
            row_index += 1

    def _render_image(
        self, docx_document: docx.document.Document, node: DocumentNode
    ) -> None:
        src = node.data.get("src")
        alt_text = node.data.get("alt") or node.text or ""
        asset = self._resolve_image(src)

        if asset is not None:
            content, _media_type = asset
            section = docx_document.sections[0]
            usable_width = (
                section.page_width - section.left_margin - section.right_margin
            )
            paragraph = docx_document.add_paragraph()
            run = paragraph.add_run()
            shape = run.add_picture(BytesIO(content), width=usable_width)
            self._set_alt_text(shape, alt_text)
        else:
            docx_document.add_paragraph(_IMAGE_PLACEHOLDER_MESSAGE, style=_PARAGRAPH_STYLE)

        if node.text:
            docx_document.add_paragraph(node.text, style=_CAPTION_STYLE)

    def _render_gap(
        self, docx_document: docx.document.Document, node: DocumentNode
    ) -> None:
        """Render a gap node using the fixed message, never `node.text`.

        Production gap nodes created during assembly carry no text at all
        -- following the same convention markdown.py/html.py already
        established, the message is always the fixed
        "Нет данных в источниках", never node.text.
        """
        docx_document.add_paragraph(_GAP_MESSAGE, style=_GAP_STYLE)

    def _apply_node_style(self, paragraph: Any, node: DocumentNode) -> None:
        self._apply_style_mapping(paragraph, node.data.get("style"))

    def _apply_style_attribute(self, paragraph: Any, value: object) -> None:
        if not isinstance(value, str):
            return
        style = {
            property_name.strip(): property_value.strip()
            for declaration in value.split(";")
            if ":" in declaration
            for property_name, property_value in [declaration.split(":", 1)]
        }
        self._apply_style_mapping(paragraph, style)

    def _apply_style_mapping(self, paragraph: Any, value: object) -> None:
        if not isinstance(value, dict):
            return
        style = value
        if style.get("font-weight") in {"700", "bold"}:
            for run in paragraph.runs:
                run.bold = True
        if style.get("font-style") == "italic":
            for run in paragraph.runs:
                run.italic = True
        if style.get("text-decoration") == "underline":
            for run in paragraph.runs:
                run.underline = True
        if style.get("text-align") in {"left", "center", "right", "justify"}:
            from docx.enum.text import WD_ALIGN_PARAGRAPH

            paragraph.alignment = getattr(
                WD_ALIGN_PARAGRAPH, str(style["text-align"]).upper()
            )

    # --- images ------------------------------------------------------

    def _resolve_image(self, src: object) -> ImageAsset | None:
        """Resolve a node's image src to bytes + MIME type, or None.

        Only locally-storage-resolvable references are embedded. A missing,
        unresolvable, or non-image reference returns None so the caller
        renders the standard placeholder instead of fabricating content or
        fetching an external resource.
        """
        if not isinstance(src, str) or not src or self._image_loader is None:
            return None
        asset = self._image_loader(src)
        if asset is None:
            return None
        _content, media_type = asset
        if not media_type or not media_type.startswith("image/"):
            return None
        return asset

    def _set_alt_text(self, shape: Any, alt_text: str) -> None:
        """Set the drawing's alt text (accessible description) if any."""
        if not alt_text:
            return
        doc_pr = shape._inline.find(qn("wp:docPr"))
        if doc_pr is not None:
            doc_pr.set("descr", alt_text)

    # --- tables --------------------------------------------------------

    def _mark_header_row(self, row: Any) -> None:
        """Mark a table row to repeat as the header row on each page."""
        tr_pr = row._tr.get_or_add_trPr()
        tbl_header = OxmlElement("w:tblHeader")
        tbl_header.set(qn("w:val"), "true")
        tr_pr.append(tbl_header)

    def _set_cell_text(self, cell: Any, value: object, style_name: str) -> None:
        text = value if isinstance(value, str) else str(value)
        paragraph = cell.paragraphs[0]
        paragraph.style = style_name
        paragraph.add_run(text)

    # --- lists / numbering -----------------------------------------------

    def _ensure_list_num_id(
        self,
        docx_document: docx.document.Document,
        ordered: bool,
        cache: dict[bool, int],
    ) -> int:
        """Return a numId for bulleted/numbered list items, creating one if needed.

        The Colvir template ships no generic bullet/numbered *paragraph*
        list style: its one custom "numbering" style (Colvir_Стиль1) is
        wired to the same multilevel numbering used by the heading styles
        (large bold blue numerals), and its Н1/Н2/Н3 styles are unused by
        the template's own sample content (see task-4-report.md). Rather
        than repurpose either of those or inject python-docx's English-
        named "List Bullet"/"List Number" styles into a Cyrillic-branded
        template, this creates a dedicated bullet/decimal numbering
        definition at render time and applies it directly to
        Colvir_Стиль_М1 paragraphs via `w:numPr` -- real Word-native list
        numbering, scoped to this render's in-memory document only.
        """
        if ordered in cache:
            return cache[ordered]

        numbering_element = docx_document.part.numbering_part.element
        existing_abstract_ids = [
            int(element.get(qn("w:abstractNumId")))
            for element in numbering_element.findall(qn("w:abstractNum"))
        ]
        new_abstract_id = max(existing_abstract_ids, default=-1) + 1

        num_fmt = "decimal" if ordered else "bullet"
        lvl_text = "%1." if ordered else "•"
        abstract_xml = (
            f'<w:abstractNum {nsdecls("w")} w:abstractNumId="{new_abstract_id}">'
            "<w:multiLevelType w:val=\"singleLevel\"/>"
            '<w:lvl w:ilvl="0">'
            '<w:start w:val="1"/>'
            f'<w:numFmt w:val="{num_fmt}"/>'
            f'<w:lvlText w:val="{lvl_text}"/>'
            '<w:lvlJc w:val="left"/>'
            "<w:pPr>"
            f'<w:ind w:left="{_LIST_INDENT_LEFT_TWIPS}" w:hanging="{_LIST_INDENT_HANGING_TWIPS}"/>'
            "</w:pPr>"
            "</w:lvl>"
            "</w:abstractNum>"
        )
        abstract_num_element = parse_xml(abstract_xml)

        first_num_element = numbering_element.find(qn("w:num"))
        if first_num_element is not None:
            first_num_element.addprevious(abstract_num_element)
        else:
            numbering_element.append(abstract_num_element)

        num_element = numbering_element.add_num(new_abstract_id)
        num_id = num_element.numId
        cache[ordered] = num_id
        return num_id

    def _apply_list_numbering(self, paragraph: Any, num_id: int, ilvl: int = 0) -> None:
        p_pr = paragraph._p.get_or_add_pPr()
        num_pr = OxmlElement("w:numPr")
        ilvl_element = OxmlElement("w:ilvl")
        ilvl_element.set(qn("w:val"), str(ilvl))
        num_id_element = OxmlElement("w:numId")
        num_id_element.set(qn("w:val"), str(num_id))
        num_pr.append(ilvl_element)
        num_pr.append(num_id_element)
        p_pr.append(num_pr)


def _cover_title(title: str) -> str:
    normalized = " ".join(title.split()).strip()
    lowered = normalized.casefold()
    for prefix in ("вопросы и ответы по модулю", "faq по модулю", "faq"):
        if lowered.startswith(prefix):
            normalized = normalized[len(prefix) :].lstrip(" :—-")
            break
    if len(normalized) <= 42:
        return normalized or "Документ"
    shortened = normalized[:42].rsplit(" ", 1)[0].rstrip(" ,:;—-")
    return shortened or normalized[:42].rstrip()
