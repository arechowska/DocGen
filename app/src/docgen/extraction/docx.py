from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
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
        for element in document.element.body.iterchildren():
            if element.tag.endswith("}p"):
                paragraph_index += 1
                paragraph = Paragraph(element, document)
                if paragraph.text.strip():
                    blocks.append(self._paragraph_block(source, paragraph, paragraph_index))
            elif element.tag.endswith("}tbl"):
                table_index += 1
                blocks.append(self._table_block(source, Table(element, document), table_index))
        return ExtractionResult(
            blocks=blocks,
            page_units=VirtualPageCalculator().from_blocks(blocks),
            warnings=[],
        )

    @staticmethod
    def _paragraph_block(source: Source, paragraph: Paragraph, index: int) -> NormalizedBlock:
        style_name = paragraph.style.name
        heading_match = _HEADING_STYLE_ID.match(paragraph.style.style_id)
        if heading_match:
            kind = BlockKind.HEADING
            data = {"level": int(heading_match.group(1))}
        elif (outline_level := _outline_level(paragraph)) is not None:
            kind = BlockKind.HEADING
            data = {"level": outline_level}
        elif _has_list_style_id(paragraph) or _has_numbering(paragraph):
            kind = BlockKind.LIST
            data = {"style": style_name}
        else:
            kind = BlockKind.TEXT
            data = {}
        return NormalizedBlock(
            id=stable_block_id(source.id, kind, f"paragraph:{index}", paragraph.text.strip()),
            kind=kind,
            text=paragraph.text.strip(),
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
