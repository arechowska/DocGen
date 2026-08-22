import pytest

from docgen.chat.executors import structural_operations
from docgen.chat.intents import route_intent
from docgen.documents.operations import apply_operations
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument


@pytest.fixture
def document() -> WorkingDocument:
    return WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(id="one", kind=NodeKind.PARAGRAPH, text="Первый. Текст."),
            DocumentNode(id="two", kind=NodeKind.PARAGRAPH, text="Второй блок."),
            DocumentNode(id="three", kind=NodeKind.PARAGRAPH, text="Третий блок."),
        ],
    )


def _execute(document: WorkingDocument, message: str) -> WorkingDocument:
    decision = route_intent(message, document)
    return apply_operations(document, structural_operations(document, decision))


def test_delete_block_compiles_without_model(document: WorkingDocument) -> None:
    result = _execute(document, "Удалить второй блок")

    assert [node.id for node in result.nodes] == ["one", "three"]


def test_move_block_compiles_without_model(document: WorkingDocument) -> None:
    result = _execute(document, "Перемести третий блок перед первым")

    assert [node.id for node in result.nodes] == ["three", "one", "two"]


def test_merge_text_blocks_compiles_without_model(document: WorkingDocument) -> None:
    result = _execute(document, "Объедини первый и второй блоки")

    assert [node.id for node in result.nodes] == ["one", "three"]
    assert result.nodes[0].text == "Первый. Текст.\nВторой блок."
    assert result.nodes[0].flags == ["structural-edit"]


def test_split_text_block_compiles_without_model(document: WorkingDocument) -> None:
    result = _execute(document, "Раздели первый блок")

    assert [node.text for node in result.nodes] == [
        "Первый.",
        "Текст.",
        "Второй блок.",
        "Третий блок.",
    ]
    assert result.nodes[0].flags == ["structural-edit"]
