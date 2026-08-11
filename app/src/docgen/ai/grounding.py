from __future__ import annotations

from collections.abc import Iterable, Mapping

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import NormalizedBlock


class GroundingValidator:
    def validate(
        self,
        document: WorkingDocument,
        source_blocks: Mapping[str, NormalizedBlock],
    ) -> list[str]:
        del source_blocks
        errors: list[str] = []
        for node in _walk(document.nodes):
            if node.kind is NodeKind.GAP:
                self._validate_gap(node, errors)
        return errors

    @staticmethod
    def _validate_gap(node: DocumentNode, errors: list[str]) -> None:
        if "missing-source-data" not in node.flags:
            errors.append(f"Узел {node.id} типа gap должен иметь флаг missing-source-data")
        if node.text not in (None, "") or node.data or node.children:
            errors.append(f"Узел {node.id} типа gap не должен содержать текст или данные")

def _walk(nodes: Iterable[DocumentNode]) -> Iterable[DocumentNode]:
    for node in nodes:
        yield node
        yield from _walk(node.children)
