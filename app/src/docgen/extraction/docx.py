from __future__ import annotations

import html
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.styles.style import BaseStyle
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError

from docgen.extraction.page_units import VirtualPageCalculator
from docgen.extraction.registry import (
    ExtractionError,
    ExtractionResult,
    preflight_file_size,
    stable_block_id,
)
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.models import Source

_HEADING_STYLE_ID = re.compile(r"^Heading([1-9])$", re.IGNORECASE)
_LIST_STYLE_ID = re.compile(r"^List(?:Bullet|Number)(?:[1-9]\d*)?$", re.IGNORECASE)
_TOC_FIELD_INSTRUCTION = re.compile(r"(?:^|\s)TOC(?:\s|$)", re.IGNORECASE)
_TOC_TITLES = frozenset({"оглавление", "содержание"})
_MAX_HEADING_WORDS = 20
_MAX_HEADING_CHARS = 150


class DocxExtractor:
    def __init__(
        self,
        *,
        max_file_bytes: int = 52_428_800,
        max_archive_entries: int = 10_000,
        max_archive_uncompressed_bytes: int = 209_715_200,
    ) -> None:
        self._max_file_bytes = max_file_bytes
        self._max_archive_entries = max_archive_entries
        self._max_archive_uncompressed_bytes = max_archive_uncompressed_bytes

    def extract(self, source: Source, path: Path) -> ExtractionResult:
        preflight_file_size(
            path,
            self._max_file_bytes,
            read_error_message="Не удалось прочитать DOCX-файл",
        )
        try:
            with ZipFile(path) as archive:
                entries = archive.infolist()
                expanded_bytes = sum(entry.file_size for entry in entries)
                if (
                    len(entries) > self._max_archive_entries
                    or expanded_bytes > self._max_archive_uncompressed_bytes
                ):
                    raise ExtractionError("Архив DOCX превышает допустимый объём")
            document = Document(path)
        except ExtractionError:
            raise
        except (BadZipFile, KeyError, OSError, PackageNotFoundError, ValueError, XMLSyntaxError) as error:
            raise ExtractionError("Не удалось прочитать DOCX-файл") from error
        blocks: list[NormalizedBlock] = []
        paragraph_index = 0
        table_index = 0
        skipped_body_indexes = _toc_body_indexes(document)
        for body_index, element in enumerate(document.element.body.iterchildren()):
            if element.tag.endswith("}p"):
                paragraph_index += 1
                if body_index in skipped_body_indexes:
                    continue
                paragraph = Paragraph(element, document)
                if paragraph.text.strip():
                    blocks.append(self._paragraph_block(source, paragraph, paragraph_index))
            elif element.tag.endswith("}tbl"):
                table_index += 1
                if body_index in skipped_body_indexes:
                    continue
                blocks.append(self._table_block(source, Table(element, document), table_index))
        return ExtractionResult(
            blocks=blocks,
            page_units=VirtualPageCalculator().from_blocks(blocks),
            warnings=[],
        )

    def extract_workspace(self, source: Source, path: Path) -> ExtractionResult:
        preflight_file_size(
            path,
            self._max_file_bytes,
            read_error_message="\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u0440\u043e\u0447\u0438\u0442\u0430\u0442\u044c DOCX-\u0444\u0430\u0439\u043b",
        )
        try:
            with ZipFile(path) as archive:
                entries = archive.infolist()
                expanded_bytes = sum(entry.file_size for entry in entries)
                if (
                    len(entries) > self._max_archive_entries
                    or expanded_bytes > self._max_archive_uncompressed_bytes
                ):
                    raise ExtractionError("\u0410\u0440\u0445\u0438\u0432 DOCX \u043f\u0440\u0435\u0432\u044b\u0448\u0430\u0435\u0442 \u0434\u043e\u043f\u0443\u0441\u0442\u0438\u043c\u044b\u0439 \u043e\u0431\u044a\u0451\u043c")
            document = Document(path)
        except ExtractionError:
            raise
        except (BadZipFile, KeyError, OSError, PackageNotFoundError, ValueError, XMLSyntaxError) as error:
            raise ExtractionError("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u0440\u043e\u0447\u0438\u0442\u0430\u0442\u044c DOCX-\u0444\u0430\u0439\u043b") from error

        blocks: list[NormalizedBlock] = []
        paragraph_index = 0
        table_index = 0
        pending_list: _WorkspaceListGroup | None = None
        skipped_body_indexes = _toc_body_indexes(document)
        for body_index, element in enumerate(document.element.body.iterchildren()):
            if element.tag.endswith("}p"):
                paragraph_index += 1
                if body_index in skipped_body_indexes:
                    if pending_list is not None:
                        blocks.append(pending_list.to_block())
                        pending_list = None
                    continue
                paragraph = Paragraph(element, document)
                paragraph_text = _workspace_paragraph_text(paragraph).strip()
                if not paragraph_text:
                    if pending_list is not None:
                        blocks.append(pending_list.to_block())
                        pending_list = None
                    continue
                block = self._paragraph_block(
                    source,
                    paragraph,
                    paragraph_index,
                    text=paragraph_text,
                )
                # Numbered Word headings contain both outline/heading metadata
                # and numPr. Their semantic heading role takes precedence over
                # numbering; otherwise a document outline becomes one large
                # ordered list in the editor.
                if block.kind is BlockKind.HEADING:
                    if pending_list is not None:
                        blocks.append(pending_list.to_block())
                        pending_list = None
                    blocks.append(
                        block.model_copy(
                            update={
                                "data": {
                                    **block.data,
                                    "html": _workspace_paragraph_html(paragraph),
                                }
                            }
                        )
                    )
                    continue
                list_context = _workspace_list_context(paragraph, document)
                if list_context is not None:
                    if pending_list is None:
                        pending_list = _WorkspaceListGroup(source, paragraph_index, list_context)
                    elif not pending_list.accepts(list_context):
                        blocks.append(pending_list.to_block())
                        pending_list = _WorkspaceListGroup(source, paragraph_index, list_context)
                    pending_list.add(
                        list_context,
                        paragraph_text,
                        _workspace_paragraph_html(paragraph),
                        f"paragraph:{paragraph_index}",
                    )
                    continue
                if pending_list is not None:
                    blocks.append(pending_list.to_block())
                    pending_list = None
                blocks.append(
                    block.model_copy(
                        update={"data": {**block.data, "html": _workspace_paragraph_html(paragraph)}}
                    )
                )
            elif element.tag.endswith("}tbl"):
                if pending_list is not None:
                    blocks.append(pending_list.to_block())
                    pending_list = None
                table_index += 1
                if body_index in skipped_body_indexes:
                    continue
                blocks.append(self._table_block(source, Table(element, document), table_index))
        if pending_list is not None:
            blocks.append(pending_list.to_block())
        return ExtractionResult(
            blocks=blocks,
            page_units=VirtualPageCalculator().from_blocks(blocks),
            warnings=[],
        )


    @staticmethod
    def _paragraph_block(
        source: Source,
        paragraph: Paragraph,
        index: int,
        *,
        text: str | None = None,
    ) -> NormalizedBlock:
        text = paragraph.text.strip() if text is None else text
        style_name = paragraph.style.name
        heading_match = _HEADING_STYLE_ID.match(paragraph.style.style_id)
        outline_level = _outline_level(paragraph)
        if (heading_match or outline_level is not None) and _looks_like_heading(text):
            kind = BlockKind.HEADING
            data = {
                "level": int(heading_match.group(1)) if heading_match else outline_level
            }
        elif _has_list_style_id(paragraph) or _has_numbering(paragraph):
            kind = BlockKind.LIST
            data = {"style": style_name}
        else:
            kind = BlockKind.TEXT
            data = {}
        return NormalizedBlock(
            id=stable_block_id(source.id, kind, f"paragraph:{index}", text),
            kind=kind,
            text=text,
            data=data,
            provenance=[Provenance(source_id=source.id, locator=f"paragraph:{index}")],
            confidence=1.0,
        )

    @staticmethod
    def _table_block(source: Source, table: Table, index: int) -> NormalizedBlock:
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        return NormalizedBlock(
            id=stable_block_id(
                source.id,
                BlockKind.TABLE,
                f"table:{index}",
                "\n".join("\t".join(row) for row in rows),
            ),
            kind=BlockKind.TABLE,
            text="\n".join("\t".join(row) for row in rows),
            data={"rows": rows},
            provenance=[Provenance(source_id=source.id, locator=f"table:{index}")],
            confidence=1.0,
        )


def _toc_body_indexes(document: Document) -> set[int]:
    """Return body elements occupied by a cached Word table of contents.

    A complex ``TOC`` field can start in one paragraph and end several
    paragraphs later. Its cached display text must not become editable body
    content, because exports create their own contents from headings.
    """
    body_elements = list(document.element.body.iterchildren())
    skipped: set[int] = set()
    field_stack: list[list[int | bool]] = []

    for body_index, element in enumerate(body_elements):
        for descendant in element.iter():
            if descendant.tag == qn("w:fldSimple"):
                instruction = descendant.get(qn("w:instr"), "")
                if _is_toc_instruction(instruction):
                    skipped.add(body_index)
                continue
            if descendant.tag == qn("w:fldChar"):
                field_type = descendant.get(qn("w:fldCharType"))
                if field_type == "begin":
                    field_stack.append([body_index, False])
                elif field_type == "end" and field_stack:
                    start_index, is_toc = field_stack.pop()
                    if is_toc:
                        skipped.update(range(int(start_index), body_index + 1))
                continue
            if (
                descendant.tag == qn("w:instrText")
                and field_stack
                and _is_toc_instruction(descendant.text or "")
            ):
                field_stack[-1][1] = True

    if not skipped:
        return skipped

    first_toc_index = min(skipped)
    for body_index in range(first_toc_index - 1, -1, -1):
        element = body_elements[body_index]
        if not element.tag.endswith("}p"):
            break
        text = _workspace_paragraph_text(Paragraph(element, document)).strip()
        if not text:
            continue
        if " ".join(text.casefold().split()).strip(".:—–-") in _TOC_TITLES:
            skipped.add(body_index)
        break
    return skipped


def _is_toc_instruction(instruction: str) -> bool:
    return _TOC_FIELD_INSTRUCTION.search(instruction.strip()) is not None


def _looks_like_heading(text: str) -> bool:
    if len(text) > _MAX_HEADING_CHARS:
        return False
    return len(text.split()) <= _MAX_HEADING_WORDS


def _style_chain(paragraph: Paragraph) -> Iterator[BaseStyle]:
    style = paragraph.style
    seen: set[int] = set()
    while style is not None and id(style.element) not in seen:
        seen.add(id(style.element))
        yield style
        style = style.base_style


def _outline_level(paragraph: Paragraph) -> int | None:
    paragraph_properties = paragraph._p.pPr
    properties_chain = [paragraph_properties]
    properties_chain.extend(style.element.pPr for style in _style_chain(paragraph))
    for properties in properties_chain:
        outline = properties.outlineLvl if properties is not None else None
        if outline is not None:
            value = outline.val
            return value + 1 if 0 <= value <= 8 else None
    return None


def _has_list_style_id(paragraph: Paragraph) -> bool:
    return any(_LIST_STYLE_ID.match(style.style_id) for style in _style_chain(paragraph))


def _has_numbering(paragraph: Paragraph) -> bool:
    paragraph_properties = paragraph._p.pPr
    properties_chain = [paragraph_properties]
    properties_chain.extend(style.element.pPr for style in _style_chain(paragraph))
    for properties in properties_chain:
        numbering = properties.numPr if properties is not None else None
        if numbering is None or numbering.numId is None:
            continue
        return numbering.numId.val != 0

    return False
@dataclass(frozen=True)
class _WorkspaceListContext:
    num_id: int
    level: int
    ordered: bool


@dataclass
class _WorkspaceListItem:
    text: str
    html: str
    ordered: bool
    provenance: Provenance
    children: list[_WorkspaceListItem] = field(default_factory=list)

    def rendered_html(self) -> str:
        return self.html + _render_workspace_children(self.children)


class _WorkspaceListGroup:
    def __init__(self, source: Source, index: int, context: _WorkspaceListContext) -> None:
        self._source = source
        self._index = index
        self._num_id = context.num_id
        self._ordered = context.ordered
        self._root_level = context.level
        self._roots: list[_WorkspaceListItem] = []
        self._last_by_level: dict[int, _WorkspaceListItem] = {}

    def accepts(self, context: _WorkspaceListContext) -> bool:
        return context.level > self._root_level or (
            context.num_id == self._num_id and context.ordered == self._ordered
        )

    def add(
        self,
        context: _WorkspaceListContext,
        text: str,
        rich_html: str,
        locator: str,
    ) -> None:
        item = _WorkspaceListItem(
            text=text,
            html=rich_html,
            ordered=context.ordered,
            provenance=Provenance(source_id=self._source.id, locator=locator),
        )
        normalized_level = max(0, context.level - self._root_level)
        parent = next(
            (
                self._last_by_level[level]
                for level in range(normalized_level - 1, -1, -1)
                if level in self._last_by_level
            ),
            None,
        )
        if parent is None:
            self._roots.append(item)
            context_level = 0
        else:
            parent.children.append(item)
            context_level = normalized_level
        self._last_by_level = {
            level: previous for level, previous in self._last_by_level.items() if level < context_level
        }
        self._last_by_level[context_level] = item

    def to_block(self) -> NormalizedBlock:
        items = [item.text for item in self._roots]
        text = "\n".join(item.text for item in self._walk_items())
        return NormalizedBlock(
            id=stable_block_id(self._source.id, BlockKind.LIST, f"paragraph:{self._index}", text),
            kind=BlockKind.LIST,
            text=text,
            data={
                "ordered": self._ordered,
                "items": items,
                "items_html": [item.rendered_html() for item in self._roots],
            },
            provenance=[item.provenance for item in self._walk_items()],
            confidence=1.0,
        )

    def _walk_items(self) -> Iterator[_WorkspaceListItem]:
        for item in self._roots:
            yield item
            yield from _walk_workspace_children(item)


def _walk_workspace_children(item: _WorkspaceListItem) -> Iterator[_WorkspaceListItem]:
    for child in item.children:
        yield child
        yield from _walk_workspace_children(child)


def _render_workspace_children(children: list[_WorkspaceListItem]) -> str:
    rendered: list[str] = []
    current_ordered: bool | None = None
    current_items: list[str] = []
    for child in children:
        if current_ordered is not None and child.ordered != current_ordered:
            tag = "ol" if current_ordered else "ul"
            rendered.append(f"<{tag}>" + "".join(current_items) + f"</{tag}>")
            current_items = []
        current_ordered = child.ordered
        current_items.append(f"<li>{child.rendered_html()}</li>")
    if current_ordered is not None:
        tag = "ol" if current_ordered else "ul"
        rendered.append(f"<{tag}>" + "".join(current_items) + f"</{tag}>")
    return "".join(rendered)


def _workspace_list_context(paragraph: Paragraph, document: Document) -> _WorkspaceListContext | None:
    numbering = _numbering_properties(paragraph)
    if numbering is None:
        return None
    num_id, level = numbering
    return _WorkspaceListContext(
        num_id=num_id,
        level=level,
        ordered=_numbering_format(document, num_id, level) != "bullet",
    )


def _numbering_properties(paragraph: Paragraph) -> tuple[int, int] | None:
    paragraph_properties = paragraph._p.pPr
    properties_chain = [paragraph_properties]
    properties_chain.extend(style.element.pPr for style in _style_chain(paragraph))
    for properties in properties_chain:
        numbering = properties.numPr if properties is not None else None
        if numbering is None or numbering.numId is None:
            continue
        try:
            num_id = int(numbering.numId.val)
        except (TypeError, ValueError):
            continue
        if num_id == 0:
            continue
        level = int(numbering.ilvl.val) if numbering.ilvl is not None else 0
        return num_id, level
    return None


def _numbering_format(document: Document, num_id: int, level: int) -> str:
    numbering = document.part.numbering_part.element
    number_instance = next(
        (element for element in numbering if element.tag == qn("w:num") and element.get(qn("w:numId")) == str(num_id)),
        None,
    )
    if number_instance is None:
        return "bullet"
    abstract_id_element = next(
        (element for element in number_instance if element.tag == qn("w:abstractNumId")),
        None,
    )
    if abstract_id_element is None:
        return "bullet"
    abstract_id = abstract_id_element.get(qn("w:val"))
    abstract_number = next(
        (element for element in numbering if element.tag == qn("w:abstractNum") and element.get(qn("w:abstractNumId")) == abstract_id),
        None,
    )
    if abstract_number is None:
        return "bullet"
    level_element = next(
        (element for element in abstract_number if element.tag == qn("w:lvl") and element.get(qn("w:ilvl")) == str(level)),
        None,
    )
    if level_element is None:
        return "bullet"
    format_element = next((element for element in level_element if element.tag == qn("w:numFmt")), None)
    return format_element.get(qn("w:val"), "bullet") if format_element is not None else "bullet"


def _workspace_paragraph_html(paragraph: Paragraph) -> str:
    return "".join(
        _workspace_element_html(child, paragraph)
        for child in paragraph._p.iterchildren()
        if child.tag != qn("w:pPr")
    ).strip()


def _workspace_paragraph_text(paragraph: Paragraph) -> str:
    return "".join(
        _workspace_element_text(child)
        for child in paragraph._p.iterchildren()
        if child.tag != qn("w:pPr")
    )


def _workspace_element_text(element) -> str:
    if element.tag == qn("w:t"):
        return element.text or ""
    if element.tag == qn("w:tab"):
        return "\t"
    if element.tag in {qn("w:br"), qn("w:cr")}:
        return "\n"
    return "".join(_workspace_element_text(child) for child in element.iterchildren())


def _workspace_element_html(element, paragraph: Paragraph) -> str:
    if element.tag == qn("w:r"):
        return _workspace_run_html(element, paragraph)
    if element.tag == qn("w:hyperlink"):
        content = "".join(
            _workspace_element_html(child, paragraph)
            for child in element.iterchildren()
        )
        href = _safe_hyperlink_url(paragraph, element.get(qn("r:id")))
        if href:
            return f'<a href="{html.escape(href, quote=True)}">{content}</a>'
        return content
    if element.tag == qn("w:t"):
        return html.escape(element.text or "")
    if element.tag == qn("w:tab"):
        return "\t"
    if element.tag in {qn("w:br"), qn("w:cr")}:
        return "<br>"
    return "".join(
        _workspace_element_html(child, paragraph)
        for child in element.iterchildren()
    )


def _workspace_run_html(run, paragraph: Paragraph) -> str:
    fragment = "".join(
        _workspace_element_html(child, paragraph)
        for child in run.iterchildren()
        if child.tag != qn("w:rPr")
    )
    properties_chain = list(_run_properties_chain(run, paragraph))
    if not properties_chain:
        return fragment

    wrappers: list[str] = []
    if _effective_run_property_enabled(properties_chain, "w:b"):
        wrappers.append("strong")
    if _effective_run_property_enabled(properties_chain, "w:i"):
        wrappers.append("em")
    if _effective_run_property_enabled(properties_chain, "w:u"):
        wrappers.append("u")
    if _effective_run_property_enabled(
        properties_chain, "w:strike"
    ) or _effective_run_property_enabled(properties_chain, "w:dstrike"):
        wrappers.append("s")
    vertical_alignment = _effective_run_property_value(
        properties_chain,
        "w:vertAlign",
    )
    if vertical_alignment == "subscript":
        wrappers.append("sub")
    elif vertical_alignment == "superscript":
        wrappers.append("sup")
    color_value = _effective_run_property_value(properties_chain, "w:color") or ""
    if re.fullmatch(r"[0-9a-fA-F]{6}", color_value):
        wrappers.append(f'span style="color:#{color_value.lower()}"')
    for wrapper in reversed(wrappers):
        if wrapper.startswith("span "):
            fragment = f"<{wrapper}>{fragment}</span>"
        else:
            fragment = f"<{wrapper}>{fragment}</{wrapper}>"
    return fragment


def _run_properties_chain(run, paragraph: Paragraph) -> Iterator:
    direct_properties = run.find(qn("w:rPr"))
    if direct_properties is not None:
        yield direct_properties
        style_reference = direct_properties.find(qn("w:rStyle"))
    else:
        style_reference = None
    if style_reference is None:
        return
    style_id = style_reference.get(qn("w:val"))
    if not style_id:
        return
    style = paragraph.part.document.styles.get_by_id(
        style_id,
        WD_STYLE_TYPE.CHARACTER,
    )
    seen: set[int] = set()
    while style is not None and id(style.element) not in seen:
        seen.add(id(style.element))
        if style.element.rPr is not None:
            yield style.element.rPr
        style = style.base_style


def _effective_run_property_enabled(properties_chain: list, name: str) -> bool:
    disabled_values = {"0", "false", "none", "off"}
    for properties in properties_chain:
        element = properties.find(qn(name))
        if element is not None:
            return element.get(qn("w:val"), "1").lower() not in disabled_values
    return False


def _effective_run_property_value(
    properties_chain: list,
    name: str,
) -> str | None:
    for properties in properties_chain:
        element = properties.find(qn(name))
        if element is not None:
            return element.get(qn("w:val"))
    return None


def _safe_hyperlink_url(paragraph: Paragraph, relationship_id: str | None) -> str | None:
    if relationship_id is None:
        return None
    try:
        url = str(paragraph.part.rels[relationship_id].target_ref)
    except KeyError:
        return None
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https", "mailto"} or any(char.isspace() for char in url):
        return None
    return url
