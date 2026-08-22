"""Deterministic source conversion helpers that never persist editor state."""

from __future__ import annotations

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import BlockKind, NormalizedBlock
from docgen.templates_catalog.loader import NO_TEMPLATE_ID


def conversion_document(
    blocks: list[NormalizedBlock], title: str
) -> WorkingDocument:
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
        if block.kind is BlockKind.LIST and "items" not in data:
            data["items"] = [block.text]
        nodes.append(
            DocumentNode(
                id=f"document-node:{block.id}",
                kind=node_kinds[block.kind],
                text=block.text,
                data=data,
                provenance=list(block.provenance),
            )
        )
    return WorkingDocument(title=title, template_id=NO_TEMPLATE_ID, nodes=nodes)


__all__ = ["conversion_document"]
