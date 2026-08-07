from __future__ import annotations

from docgen.ai.grounding import GroundingValidator
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import Provenance


def test_grounding_rejects_unknown_block_reference() -> None:
    errors = GroundingValidator().validate(document_with_source("missing"), {"known"})

    assert errors == ["Узел n1 ссылается на неизвестный блок missing"]


def test_grounding_requires_a_known_block_for_content_nodes() -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Текст")],
    )

    assert GroundingValidator().validate(document, {"known"}) == [
        "Узел n1 не содержит ссылку на исходный блок"
    ]


def test_grounding_allows_empty_gap_only_when_flagged() -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[DocumentNode(id="gap-1", kind=NodeKind.GAP, flags=["missing-source-data"])],
    )

    assert GroundingValidator().validate(document, set()) == []


def test_grounding_rejects_gap_with_text_or_missing_flag() -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[DocumentNode(id="gap-1", kind=NodeKind.GAP, text="Придуманный факт")],
    )

    assert GroundingValidator().validate(document, {"known"}) == [
        "Узел gap-1 типа gap должен иметь флаг missing-source-data",
        "Узел gap-1 типа gap не должен содержать текст или данные",
    ]


def test_grounding_validates_nested_nodes() -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(
                id="parent",
                kind=NodeKind.HEADING,
                text="Заголовок",
                provenance=[Provenance(source_id="known", locator="p1")],
                children=[DocumentNode(id="child", kind=NodeKind.PARAGRAPH, text="Текст")],
            )
        ],
    )

    assert GroundingValidator().validate(document, {"known"}) == [
        "Узел child не содержит ссылку на исходный блок"
    ]


def document_with_source(block_id: str) -> WorkingDocument:
    return WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(
                id="n1",
                kind=NodeKind.PARAGRAPH,
                text="Текст",
                provenance=[Provenance(source_id=block_id, locator="строка 1")],
            )
        ],
    )
