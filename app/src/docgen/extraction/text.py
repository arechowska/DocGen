from __future__ import annotations

from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

from docgen.extraction.page_units import VirtualPageCalculator
from docgen.extraction.registry import ExtractionError, ExtractionResult, stable_block_id
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.models import Source


class TextExtractor:
    def extract(self, source: Source, path: Path) -> ExtractionResult:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as error:
            raise ExtractionError("Не удалось прочитать текстовый файл в UTF-8") from error

        if self._is_markdown(source, path):
            blocks = self._extract_markdown(source, text)
        else:
            blocks = self._extract_text(source, text)
        return ExtractionResult(
            blocks=blocks,
            page_units=VirtualPageCalculator().from_blocks(blocks),
            warnings=[],
        )

    @staticmethod
    def _is_markdown(source: Source, path: Path) -> bool:
        return (source.media_type or "").lower() in {
            "text/markdown",
            "text/x-markdown",
            "text/md",
        } or path.suffix.lower() in {".md", ".markdown", ".mdown"}

    @staticmethod
    def _extract_text(source: Source, text: str) -> list[NormalizedBlock]:
        return [
            _block(source, BlockKind.TEXT, line, f"lines:{line_number}-{line_number}")
            for line_number, line in enumerate(text.splitlines(), start=1)
            if line.strip()
        ]

    def _extract_markdown(self, source: Source, text: str) -> list[NormalizedBlock]:
        tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(text)
        blocks: list[NormalizedBlock] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.type == "heading_open":
                inline = tokens[index + 1]
                level = int(token.tag[1:])
                blocks.append(
                    _block(
                        source,
                        BlockKind.HEADING,
                        inline.content,
                        _line_locator(token),
                        {"level": level},
                    )
                )
                index += 3
                continue
            if token.type in {"bullet_list_open", "ordered_list_open"}:
                end_index = _closing_token_index(tokens, index)
                items = [
                    item.content
                    for item in tokens[index + 1 : end_index]
                    if item.type == "inline" and item.content
                ]
                blocks.append(
                    _block(
                        source,
                        BlockKind.LIST,
                        "\n".join(items),
                        _line_locator(token),
                        {"ordered": token.type == "ordered_list_open", "items": items},
                    )
                )
                index = end_index + 1
                continue
            if token.type == "table_open":
                end_index = _closing_token_index(tokens, index)
                rows = _table_rows(tokens[index + 1 : end_index])
                blocks.append(
                    _block(
                        source,
                        BlockKind.TABLE,
                        "\n".join("\t".join(row) for row in rows),
                        _line_locator(token),
                        {"rows": rows},
                    )
                )
                index = end_index + 1
                continue
            if token.type == "paragraph_open":
                inline = tokens[index + 1]
                if inline.content:
                    blocks.append(
                        _block(source, BlockKind.TEXT, inline.content, _line_locator(token))
                    )
                index += 3
                continue
            index += 1
        return blocks


def _block(
    source: Source,
    kind: BlockKind,
    text: str,
    locator: str,
    data: dict | None = None,
) -> NormalizedBlock:
    return NormalizedBlock(
        id=stable_block_id(source.id, kind, locator, text),
        kind=kind,
        text=text,
        data=data or {},
        provenance=[Provenance(source_id=source.id, locator=locator)],
        confidence=1.0,
    )


def _line_locator(token: Token) -> str:
    if token.map is None:
        return "lines:1-1"
    start, end = token.map
    return f"lines:{start + 1}-{max(start + 1, end)}"


def _closing_token_index(tokens: list[Token], opening_index: int) -> int:
    depth = 0
    for index in range(opening_index, len(tokens)):
        if tokens[index].nesting == 1:
            depth += 1
        elif tokens[index].nesting == -1:
            depth -= 1
            if depth == 0:
                return index
    raise ExtractionError("Некорректная структура Markdown")


def _table_rows(tokens: list[Token]) -> list[list[str]]:
    rows: list[list[str]] = []
    current_row: list[str] | None = None
    for index, token in enumerate(tokens):
        if token.type == "tr_open":
            current_row = []
            rows.append(current_row)
        elif token.type in {"th_open", "td_open"} and current_row is not None:
            inline = tokens[index + 1]
            if inline.type == "inline":
                current_row.append(inline.content)
    return rows
