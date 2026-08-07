from __future__ import annotations

import re
from pathlib import Path
from zipfile import BadZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError

from docgen.extraction.registry import ExtractionError, ExtractionResult, stable_block_id
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.models import Source

_HEADING_STYLE = re.compile(r"^Heading\s+(\d+)$", re.IGNORECASE)


class DocxExtractor:
    def extract(self, source: Source, path: Path) -> ExtractionResult:
        try:
            document = Document(path)
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
        return ExtractionResult(blocks=blocks, page_units=1, warnings=[])

    @staticmethod
    def _paragraph_block(source: Source, paragraph: Paragraph, index: int) -> NormalizedBlock:
        style_name = paragraph.style.name
        heading_match = _HEADING_STYLE.match(style_name)
        if heading_match:
            kind = BlockKind.HEADING
            data = {"level": int(heading_match.group(1))}
        elif style_name.lower().startswith("list ") or _has_numbering(paragraph):
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


def _has_numbering(paragraph: Paragraph) -> bool:
    paragraph_properties = paragraph._p.pPr
    return paragraph_properties is not None and paragraph_properties.numPr is not None
