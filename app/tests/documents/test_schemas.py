import pytest
from pydantic import ValidationError

from docgen.documents.schemas import CheckReport, DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance


def test_document_round_trip_preserves_provenance() -> None:
    document = WorkingDocument(
        title="Use Case",
        template_id="use-case",
        nodes=[
            DocumentNode(
                id="n1",
                kind=NodeKind.PARAGRAPH,
                text="Оплата",
                provenance=[Provenance(source_id="s1", locator="page:2")],
            )
        ],
    )

    assert WorkingDocument.model_validate_json(document.model_dump_json()) == document


def test_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        NormalizedBlock(id="b1", kind=BlockKind.TEXT, text="x", confidence=1.1)


def test_document_nodes_are_immutable() -> None:
    node = DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Оплата")

    with pytest.raises(ValidationError):
        node.text = "Изменено"


def test_passed_rule_ids_are_immutable() -> None:
    report = CheckReport(template_id="use-case", passed_rule_ids=["rule-1"])

    assert report.passed_rule_ids == ("rule-1",)
    with pytest.raises(TypeError):
        report.passed_rule_ids[0] = "rule-2"  # type: ignore[index]
