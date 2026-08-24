from docgen.documents.schemas import NodeKind
from docgen.extraction.schemas import BlockKind, NormalizedBlock
from docgen.workflows.conversion import conversion_document


def test_conversion_rebases_imported_heading_levels_when_requested() -> None:
    blocks = [
        NormalizedBlock(id="heading-2", kind=BlockKind.HEADING, text="Guide", data={"level": 2}, confidence=1.0),
        NormalizedBlock(id="heading-3", kind=BlockKind.HEADING, text="Scope", data={"level": 3}, confidence=1.0),
    ]

    document = conversion_document(blocks, "Guide", rebase_heading_levels=True)

    assert [node.data["level"] for node in document.nodes if node.kind is NodeKind.HEADING] == [1, 2]


def test_conversion_keeps_imported_heading_levels_by_default() -> None:
    blocks = [NormalizedBlock(id="heading-2", kind=BlockKind.HEADING, text="Guide", data={"level": 2}, confidence=1.0)]

    document = conversion_document(blocks, "Guide")

    assert document.nodes[0].data["level"] == 2
