import pytest

from docgen.chat.intents import IntentKind, StructureAction, route_intent
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument


@pytest.fixture
def document() -> WorkingDocument:
    return WorkingDocument(
        title="Регламент",
        template_id="use-case",
        nodes=[DocumentNode(id="body", kind=NodeKind.PARAGRAPH, text="Старый текст")],
    )


@pytest.mark.parametrize(
    "message",
    [
        "Добавь в конец документа: авторское примечание",
        "Добавь согласованный текст команды",
        "Замени Старый текст на Новый текст",
        "Название должно быть TrackCare",
    ],
)
def test_router_recognizes_general_authored_edits(
    document: WorkingDocument, message: str
) -> None:
    assert route_intent(message, document).kind is IntentKind.AUTHORED_EDIT


def test_router_recognizes_grounded_fact_edit(document: WorkingDocument) -> None:
    decision = route_intent("Уточни лимит строго по источнику", document)

    assert decision.kind is IntentKind.GROUNDED_EDIT
    assert decision.retrieval_query == "лимит"


def test_router_keeps_faq_action_template_specific(document: WorkingDocument) -> None:
    faq = document.model_copy(
        update={"template_id": "faq", "build_template_id": "faq"}
    )

    assert route_intent("Добавь вопрос о лимите", faq).kind is IntentKind.TEMPLATE_ACTION
    assert route_intent("Добавь вопрос о лимите", document).kind is IntentKind.CLARIFICATION


def test_router_sends_question_and_answer_phrasing_to_faq_action(
    document: WorkingDocument,
) -> None:
    faq = document.model_copy(
        update={"template_id": "faq", "build_template_id": "faq"}
    )

    decision = route_intent("Добавь вопрос и ответ: Что такое AMS?", faq)

    assert decision.kind is IntentKind.TEMPLATE_ACTION


@pytest.mark.parametrize(
    ("message", "action"),
    [
        ("Раздели документ на разделы", StructureAction.SECTIONIZE),
        ("Удалить второй блок", StructureAction.DELETE),
        ("Перемести третий блок перед первым", StructureAction.MOVE),
        ("Объедини первый и второй блоки", StructureAction.MERGE),
        ("Раздели второй блок", StructureAction.SPLIT),
    ],
)
def test_router_uses_shared_structural_vocabulary(
    document: WorkingDocument,
    message: str,
    action: StructureAction,
) -> None:
    decision = route_intent(message, document)

    assert decision.kind is IntentKind.STRUCTURE
    assert decision.structure_action is action


def test_router_does_not_treat_negated_action_word_as_a_command(
    document: WorkingDocument,
) -> None:
    """"нельзя удалить" (can't be deleted) is explaining a constraint, not
    asking to delete -- the message should reach grounded editing instead of
    silently deleting the first document block."""
    decision = route_intent(
        "Дополни ответ почему нельзя удалить первое условие фильтрации",
        document,
    )

    assert decision.kind is IntentKind.GROUNDED_EDIT


def test_router_recognizes_formatting_without_sources(document: WorkingDocument) -> None:
    decision = route_intent("Сделай второй абзац жирным и синим", document)

    assert decision.kind is IntentKind.FORMAT


def test_router_asks_for_clarification_instead_of_calling_model(
    document: WorkingDocument,
) -> None:
    decision = route_intent("Сделай документ получше", document)

    assert decision.kind is IntentKind.CLARIFICATION
    assert "уточни" in decision.clarification.casefold()
