from __future__ import annotations

from collections.abc import Iterable

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument


class GroundingValidator:
    def validate(self, document: WorkingDocument, source_block_ids: set[str]) -> list[str]:
        errors: list[str] = []
        for node in _walk(document.nodes):
            if node.kind is NodeKind.GAP:
                self._validate_gap(node, errors)
            else:
                self._validate_content(node, source_block_ids, errors)
        return errors

    @staticmethod
    def _validate_gap(node: DocumentNode, errors: list[str]) -> None:
        if "missing-source-data" not in node.flags:
            errors.append(f"Узел {node.id} типа gap должен иметь флаг missing-source-data")
        if node.text not in (None, "") or node.data or node.children:
            errors.append(f"Узел {node.id} типа gap не должен содержать текст или данные")

    @staticmethod
    def _validate_content(
        node: DocumentNode, source_block_ids: set[str], errors: list[str]
    ) -> None:
        if not node.provenance:
            errors.append(f"Узел {node.id} не содержит ссылку на исходный блок")
            return
        for provenance in node.provenance:
            if provenance.source_id not in source_block_ids:
                errors.append(
                    f"Узел {node.id} ссылается на неизвестный блок {provenance.source_id}"
                )


def _walk(nodes: Iterable[DocumentNode]) -> Iterable[DocumentNode]:
    for node in nodes:
        yield node
        yield from _walk(node.children)
