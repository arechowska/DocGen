import pytest

from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.workflows.assemble import WorkflowError
from docgen.workflows.structure import prepare_assembled_document


def test_use_case_adapter_adds_headings_and_named_required_gaps() -> None:
    template = TemplateCatalog().get("use-case")
    document = WorkingDocument(
        title="Открытие счёта",
        template_id="use-case",
        nodes=[
            DocumentNode(
                kind=NodeKind.LIST,
                section_id="main-flow",
                data={"items": ["Клиент отправляет заявление", "Система открывает счёт"]},
            )
        ],
    )

    prepared = prepare_assembled_document(document, template)

    sections = {node.section_id: node for node in prepared.nodes}
    assert sections["main-flow"].kind is NodeKind.HEADING
    assert sections["main-flow"].text == "Основной поток"
    assert sections["main-flow"].children[0].kind is NodeKind.LIST
    assert sections["main-flow"].children[0].data["ordered"] is True
    for required_id in ("preconditions", "main-flow", "result"):
        assert required_id in sections
    preconditions_gap = sections["preconditions"].children[0]
    assert preconditions_gap.kind is NodeKind.GAP
    assert preconditions_gap.data == {}
    assert preconditions_gap.text is None
    assert sections["preconditions"].text == "Предусловия"


def test_use_case_adapter_rejects_main_flow_collapsed_into_paragraph() -> None:
    template = TemplateCatalog().get("use-case")
    document = WorkingDocument(
        title="Открытие счёта",
        template_id="use-case",
        nodes=[
            DocumentNode(
                kind=NodeKind.PARAGRAPH,
                section_id="main-flow",
                text="1. Клиент отправляет заявление. 2. Система открывает счёт.",
            )
        ],
    )

    with pytest.raises(WorkflowError, match="Основной поток"):
        prepare_assembled_document(document, template)


def test_use_case_adapter_never_leaves_bare_missing_data_paragraph() -> None:
    template = TemplateCatalog().get("use-case")
    document = WorkingDocument(
        title="Открытие счёта",
        template_id="use-case",
        nodes=[
            DocumentNode(
                kind=NodeKind.PARAGRAPH,
                section_id="preconditions",
                text="Нет данных в источниках",
            )
        ],
    )

    with pytest.raises(WorkflowError, match="Нет данных"):
        prepare_assembled_document(document, template)
