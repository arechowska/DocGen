from __future__ import annotations

import pytest

from docgen.ai.grounding import GroundingValidator
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance


def test_grounding_allows_unknown_block_reference_for_model_generated_content() -> None:
    errors = GroundingValidator().validate(
        document_with_source("missing", quote="Текст"), {"known": _block("known", "Текст")}
    )

    assert errors == []


def test_grounding_allows_content_nodes_without_provenance() -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Текст")],
    )

    assert GroundingValidator().validate(document, {"known": _block("known", "Текст")}) == []


def test_grounding_allows_empty_gap_only_when_flagged() -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[DocumentNode(id="gap-1", kind=NodeKind.GAP, flags=["missing-source-data"])],
    )

    assert GroundingValidator().validate(document, {}) == []


@pytest.mark.parametrize("text", [None, ""])
def test_grounding_allows_none_or_empty_text_for_flagged_gap(text: str | None) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(
                id="gap-1", kind=NodeKind.GAP, text=text, flags=["missing-source-data"]
            )
        ],
    )

    assert GroundingValidator().validate(document, {}) == []


def test_grounding_rejects_gap_with_whitespace_text() -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(
                id="gap-1", kind=NodeKind.GAP, text=" ", flags=["missing-source-data"]
            )
        ],
    )

    assert GroundingValidator().validate(document, {}) == [
        "Узел gap-1 типа gap не должен содержать текст или данные"
    ]


def test_grounding_rejects_gap_with_text_or_missing_flag() -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[DocumentNode(id="gap-1", kind=NodeKind.GAP, text="Придуманный факт")],
    )

    assert GroundingValidator().validate(document, {"known": _block("known", "Текст")}) == [
        "Узел gap-1 типа gap должен иметь флаг missing-source-data",
        "Узел gap-1 типа gap не должен содержать текст или данные",
    ]


def test_grounding_allows_nested_content_nodes_without_provenance() -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(
                id="parent",
                kind=NodeKind.HEADING,
                text="Заголовок",
                provenance=[Provenance(source_id="known", locator="paragraph:1", quote="Текст")],
                children=[DocumentNode(id="child", kind=NodeKind.PARAGRAPH, text="Текст")],
            )
        ],
    )

    assert GroundingValidator().validate(document, {"known": _block("known", "Текст")}) == []


def test_grounding_allows_identifier_only_citation() -> None:
    document = document_with_source("known")

    assert GroundingValidator().validate(document, {"known": _block("known", "Текст")}) == []


def test_grounding_allows_non_verbatim_quote() -> None:
    document = document_with_source("known", quote="Выдуманный факт")

    assert GroundingValidator().validate(document, {"known": _block("known", "Текст")}) == []


def test_grounding_allows_locator_not_owned_by_block() -> None:
    document = document_with_source("known", quote="Текст", locator="paragraph:2")

    assert GroundingValidator().validate(document, {"known": _block("known", "Текст")}) == []


def document_with_source(
    block_id: str,
    *,
    quote: str | None = None,
    locator: str = "paragraph:1",
) -> WorkingDocument:
    return WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(
                id="n1",
                kind=NodeKind.PARAGRAPH,
                text="Текст",
                provenance=[Provenance(source_id=block_id, locator=locator, quote=quote)],
            )
        ],
    )


def _block(block_id: str, text: str) -> NormalizedBlock:
    return NormalizedBlock(
        id=block_id,
        kind=BlockKind.TEXT,
        text=text,
        provenance=[Provenance(source_id="source-1", locator="paragraph:1")],
        confidence=1,
    )
