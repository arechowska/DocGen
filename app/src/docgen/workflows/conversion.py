"""Deterministic source conversion helpers that never persist editor state."""

from __future__ import annotations

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import BlockKind, NormalizedBlock
from docgen.templates_catalog.loader import NO_TEMPLATE_ID


def conversion_document(
    blocks: list[NormalizedBlock],
    title: str,
    *,
    rebase_heading_levels: bool = False,
) -> WorkingDocument:
    heading_levels = [
        int(block.data.get("level", 1))
        for block in blocks
        if block.kind is BlockKind.HEADING
    ]
    heading_offset = min(heading_levels, default=1) - 1 if rebase_heading_levels else 0
    node_kinds = {
        BlockKind.TEXT: NodeKind.PARAGRAPH,
        BlockKind.HEADING: NodeKind.HEADING,
        BlockKind.LIST: NodeKind.LIST,
        BlockKind.TABLE: NodeKind.TABLE,
        BlockKind.IMAGE: NodeKind.IMAGE,
    }
    nodes: list[DocumentNode] = []
    for block in blocks:
        data = dict(block.data)
        node_text = block.text
        if block.kind is BlockKind.HEADING and rebase_heading_levels:
            level = int(data.get("level", 1)) - heading_offset
            data["level"] = max(1, min(6, level))
        if block.kind is BlockKind.LIST:
            if "items" not in data:
                data["items"] = [block.text]
            if "items_html" in data:
                node_text = None
        nodes.append(
            DocumentNode(
                id=f"document-node:{block.id}",
                kind=node_kinds[block.kind],
                text=node_text,
                data=data,
                provenance=list(block.provenance),
            )
        )
    return WorkingDocument(title=title, template_id=NO_TEMPLATE_ID, nodes=nodes)


__all__ = ["conversion_document"]
