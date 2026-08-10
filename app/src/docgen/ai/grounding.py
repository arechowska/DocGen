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
        errors: list[str] = []
        for node in _walk(document.nodes):
            if node.kind is NodeKind.GAP:
                self._validate_gap(node, errors)
            else:
                self._validate_content(node, source_blocks, errors)
        return errors

    @staticmethod
    def _validate_gap(node: DocumentNode, errors: list[str]) -> None:
        if "missing-source-data" not in node.flags:
            errors.append(f"Узел {node.id} типа gap должен иметь флаг missing-source-data")
        if node.text not in (None, "") or node.data or node.children:
            errors.append(f"Узел {node.id} типа gap не должен содержать текст или данные")

    @staticmethod
    def _validate_content(
        node: DocumentNode,
        source_blocks: Mapping[str, NormalizedBlock],
        errors: list[str],
    ) -> None:
        if not node.provenance:
            errors.append(f"Узел {node.id} не содержит ссылку на исходный блок")
            return
        for provenance in node.provenance:
            block = source_blocks.get(provenance.source_id)
            if block is None:
                errors.append(
                    f"Узел {node.id} ссылается на неизвестный блок {provenance.source_id}"
                )
                continue
            known_locators = {item.locator for item in block.provenance}
            if provenance.locator not in known_locators:
                errors.append(
                    f"Узел {node.id} ссылается на неизвестный локатор "
                    f"{provenance.locator} блока {provenance.source_id}"
                )
                continue
            quote = provenance.quote
            if quote is None or not quote.strip():
                errors.append(
                    f"Узел {node.id} не содержит точную цитату из блока "
                    f"{provenance.source_id}"
                )
                continue
            if quote not in block.text:
                errors.append(
                    f"Цитата узла {node.id} отсутствует в блоке {provenance.source_id}"
                )


def _walk(nodes: Iterable[DocumentNode]) -> Iterable[DocumentNode]:
    for node in nodes:
        yield node
        yield from _walk(node.children)
