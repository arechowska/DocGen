import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from docgen.ai.client import ModelError
from docgen.chat.schemas import ChatEditOperation, ChatEditPlan, ChatEditRequest
from docgen.chat.service import ChatGroundingError, ChatService, ChatValidationError
from docgen.db import Base
from docgen.documents.models import ProjectArtifact
from docgen.documents.operations import InsertNode, MoveNode, UpdateData, UpdateText, find_node
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import BlockKind, NormalizedBlock
from docgen.jobs.models import Job
from docgen.projects.models import Project
from docgen.sources.models import Source


class FakeModel:
    def __init__(self) -> None:
        self.calls = 0
        self.error: ModelError | None = None
        self.result: ChatEditPlan | None = None

    def generate_json(self, system: str, user: str, schema):
        self.calls += 1
        assert "русском языке" in system
        assert "Оператор" in user
        assert schema is ChatEditPlan
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=(Project.__table__, Source.__table__, ProjectArtifact.__table__, Job.__table__),
    )
    database_session = Session(engine)
    project = Project(id="p1", name="Проект")
    database_session.add(project)
    database_session.flush()
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(id="actor", kind=NodeKind.PARAGRAPH, text="Оператор"),
            DocumentNode(id="limit", kind=NodeKind.PARAGRAPH, text="Лимит"),
        ],
    )
    repository = DocumentRepository(database_session)
    repository.save_document(project.id, document)
    repository.save_document(project.id, document)
    database_session.commit()
    yield database_session
    database_session.close()
    Base.metadata.drop_all(
        engine,
        tables=(Job.__table__, ProjectArtifact.__table__, Source.__table__, Project.__table__),
    )
    engine.dispose()


@pytest.fixture
def fake_model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def chat_service(session: Session, fake_model: FakeModel) -> ChatService:
    return ChatService(
        documents=DocumentRepository(session),
        model=fake_model,
        source_blocks=_source_blocks,
    )


def test_chat_applies_grounded_plan(chat_service: ChatService, fake_model: FakeModel) -> None:
    fake_model.result = ChatEditPlan(
        summary="Уточнён актор",
        operations=[
            ChatEditOperation(
                operation=UpdateText(
                    node_id="actor",
                    text="Оператор подтверждает заявку",
                ),
                evidence_block_ids=["s1:b2"],
            )
        ],
    )

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="Уточни актора", expected_revision=2),
    )

    assert result.revision == 3
    assert find_node(result.document, "actor").text == "Оператор подтверждает заявку"


def test_chat_noop_plan_preserves_document_revision(
    chat_service: ChatService,
    fake_model: FakeModel,
    session: Session,
) -> None:
    fake_model.result = ChatEditPlan(summary="Нет правок", operations=[])

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="как дела", expected_revision=2),
    )

    assert result.summary == "Нет правок"
    assert result.revision == 2
    assert find_node(result.document, "actor").text == "Оператор"
    stored = DocumentRepository(session).get_document_with_revision("p1")
    assert stored is not None
    _, persisted_revision = stored
    assert persisted_revision == 2


def test_chat_applies_formatting_command_when_model_returns_noop(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.result = ChatEditPlan(summary="Нет правок", operations=[])

    result = chat_service.edit(
        "p1",
        ChatEditRequest(
            message="первый абзац сделай жирный шрифт синий цвет",
            expected_revision=2,
        ),
    )

    assert result.summary == "Применено форматирование"
    assert result.revision == 3
    assert find_node(result.document, "actor").data == {
        "style": {
            "color": "blue",
            "font-weight": "700",
        }
    }


def test_chat_adds_user_text_as_manual_paragraph_when_model_returns_noop(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.result = ChatEditPlan(summary="Нет правок", operations=[])

    result = chat_service.edit(
        "p1",
        ChatEditRequest(
            message="добавь вопрос как пройти в библиотеку ответ прямо и направо",
            expected_revision=2,
        ),
    )

    assert result.summary == "Добавлен текст пользователя"
    assert result.revision == 3
    inserted = result.document.nodes[-1]
    assert inserted.kind is NodeKind.PARAGRAPH
    assert inserted.text == "Вопрос: как пройти в библиотеку\nОтвет: прямо и направо"
    assert inserted.flags == ["manual-edit"]


def test_chat_adds_user_text_at_document_start_when_requested(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.result = ChatEditPlan(summary="Нет правок", operations=[])

    result = chat_service.edit(
        "p1",
        ChatEditRequest(
            message="допиши в начало документа: я поэт зовусь незнайка от меня вам балалайка",
            expected_revision=2,
        ),
    )

    inserted = result.document.nodes[0]
    assert inserted.kind is NodeKind.PARAGRAPH
    assert inserted.text == "я поэт зовусь незнайка от меня вам балалайка"
    assert result.document.nodes[1].id == "actor"
    assert result.document.nodes[2].id == "limit"


def test_chat_positioned_manual_insert_bypasses_model_and_source_lookup(
    session: Session,
    fake_model: FakeModel,
) -> None:
    source_lookup_calls = 0

    def source_blocks(project_id: str) -> list[NormalizedBlock]:
        nonlocal source_lookup_calls
        source_lookup_calls += 1
        return _source_blocks(project_id)

    service = ChatService(
        documents=DocumentRepository(session),
        model=fake_model,
        source_blocks=source_blocks,
    )

    result = service.edit(
        "p1",
        ChatEditRequest(
            message="допиши перед вторым абзацем: Новый текст",
            expected_revision=2,
        ),
    )

    assert fake_model.calls == 0
    assert source_lookup_calls == 0
    assert [node.text for node in result.document.nodes] == [
        "Оператор",
        "Новый текст",
        "Лимит",
    ]
    assert result.summary == "Добавлен текст пользователя"


def test_chat_rejects_empty_authored_text_without_model_or_source_lookup(
    session: Session,
    fake_model: FakeModel,
) -> None:
    source_lookup_calls = 0

    def source_blocks(project_id: str) -> list[NormalizedBlock]:
        nonlocal source_lookup_calls
        source_lookup_calls += 1
        return _source_blocks(project_id)

    service = ChatService(
        documents=DocumentRepository(session),
        model=fake_model,
        source_blocks=source_blocks,
    )

    with pytest.raises(ChatValidationError, match="Укажите текст для добавления"):
        service.edit(
            "p1",
            ChatEditRequest(message="допиши в начало документа:", expected_revision=2),
        )

    assert fake_model.calls == 0
    assert source_lookup_calls == 0
    stored = DocumentRepository(session).get_document_with_revision("p1")
    assert stored is not None
    document, revision = stored
    assert revision == 2
    assert [node.text for node in document.nodes] == ["Оператор", "Лимит"]


def test_chat_missing_visual_target_preserves_document(
    chat_service: ChatService,
    fake_model: FakeModel,
    session: Session,
) -> None:
    with pytest.raises(ChatValidationError, match="Абзац 9 не найден"):
        chat_service.edit(
            "p1",
            ChatEditRequest(
                message="допиши после девятого абзаца: Новый текст",
                expected_revision=2,
            ),
        )

    stored = DocumentRepository(session).get_document_with_revision("p1")
    assert stored is not None
    document, revision = stored
    assert revision == 2
    assert [node.text for node in document.nodes] == ["Оператор", "Лимит"]
    assert fake_model.calls == 0


def test_chat_unpositioned_manual_insert_keeps_model_first_fallback(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.result = ChatEditPlan(summary="Нет правок", operations=[])

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="добавь авторский текст", expected_revision=2),
    )

    assert fake_model.calls == 1
    assert result.document.nodes[-1].text == "авторский текст"


def test_chat_unpositioned_manual_insert_survives_model_error(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.error = ModelError("Модель недоступна")

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="добавь авторский текст", expected_revision=2),
    )

    assert fake_model.calls == 1
    assert result.summary == "Добавлен текст пользователя"
    assert result.revision == 3
    inserted = result.document.nodes[-1]
    assert inserted.kind is NodeKind.PARAGRAPH
    assert inserted.text == "авторский текст"


def test_chat_rejects_unknown_evidence(
    chat_service: ChatService, fake_model: FakeModel
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Добавлен лимит",
        operations=[
            ChatEditOperation(
                operation=UpdateText(node_id="limit", text="10 000"),
                evidence_block_ids=["unknown"],
            )
        ],
    )

    with pytest.raises(
        ChatGroundingError,
        match="Для этой правки нет подтверждения в источниках",
    ):
        chat_service.edit(
            "p1",
            ChatEditRequest(message="Добавь лимит", expected_revision=2),
        )


def test_chat_rejects_known_but_irrelevant_evidence_and_preserves_document(
    chat_service: ChatService,
    fake_model: FakeModel,
    session: Session,
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Добавлен лимит",
        operations=[
            ChatEditOperation(
                operation=UpdateText(
                    node_id="limit", text="Лимит 10 000 рублей"
                ),
                evidence_block_ids=["s1:b2"],
            )
        ],
    )

    with pytest.raises(
        ChatGroundingError,
        match="Для этой правки нет подтверждения в источниках",
    ):
        chat_service.edit(
            "p1",
            ChatEditRequest(message="Добавь лимит", expected_revision=2),
        )

    stored = DocumentRepository(session).get_document_with_revision("p1")
    assert stored is not None
    document, revision = stored
    assert revision == 2
    assert find_node(document, "limit").text == "Лимит"


def test_chat_accepts_relevant_operation_evidence(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Добавлен лимит",
        operations=[
            ChatEditOperation(
                operation=UpdateText(
                    node_id="limit", text="Лимит 10 000 рублей"
                ),
                evidence_block_ids=["s1:b3"],
            )
        ],
    )

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="Добавь лимит", expected_revision=2),
    )

    assert find_node(result.document, "limit").text == "Лимит 10 000 рублей"


@pytest.mark.parametrize(
    ("text", "evidence_block_id"),
    [
        ("Температура -5 °C", "s1:b4"),
        ("Коэффициент 2,5", "s1:b5"),
        ("Лимит 5%", "s1:b7"),
        ("Порог 5 и резерв 5", "s1:b8"),
    ],
)
def test_chat_rejects_inexact_or_insufficient_numeric_evidence(
    chat_service: ChatService,
    fake_model: FakeModel,
    text: str,
    evidence_block_id: str,
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Изменено числовое значение",
        operations=[
            ChatEditOperation(
                operation=UpdateText(node_id="limit", text=text),
                evidence_block_ids=[evidence_block_id],
            )
        ],
    )

    with pytest.raises(
        ChatGroundingError,
        match="Для этой правки нет подтверждения в источниках",
    ):
        chat_service.edit(
            "p1",
            ChatEditRequest(message="Измени значение", expected_revision=2),
        )


def test_chat_accepts_equivalent_signed_decimal_range_and_currency_literals(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Добавлены точные значения",
        operations=[
            ChatEditOperation(
                operation=UpdateText(
                    node_id="limit",
                    text=(
                        "Комиссия -2.5%. Диапазон -5-10 %. "
                        "Сумма 2,5 ₽."
                    ),
                ),
                evidence_block_ids=["s1:b6"],
            )
        ],
    )

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="Добавь значения", expected_revision=2),
    )

    assert find_node(result.document, "limit").text == (
        "Комиссия -2.5%. Диапазон -5-10 %. Сумма 2,5 ₽."
    )


def test_chat_allows_non_factual_structural_operation_without_evidence(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Переставлены блоки",
        operations=[
            ChatEditOperation(
                operation=MoveNode(node_id="limit", index=0),
            )
        ],
    )

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="Переставь блоки", expected_revision=2),
    )

    assert [node.id for node in result.document.nodes] == ["limit", "actor"]


def test_chat_allows_grounded_generated_questions_from_source(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Добавлены вопросы",
        operations=[
            ChatEditOperation(
                operation=InsertNode(
                    index=2,
                    node=DocumentNode(
                        id="questions",
                        kind=NodeKind.LIST,
                        data={"items": ["Как оператор подтверждает заявку?"]},
                    ),
                ),
                evidence_block_ids=["s1:b2"],
            )
        ],
    )

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="Добавь больше вопросов", expected_revision=2),
    )

    assert result.document.nodes[-1].data["items"] == [
        "Как оператор подтверждает заявку?"
    ]


def test_chat_allows_style_only_data_operation_without_evidence(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Выровнен блок",
        operations=[
            ChatEditOperation(
                operation=UpdateData(
                    node_id="actor",
                    data={"alignment": "center", "width": 80},
                ),
            )
        ],
    )

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="Выровняй блок", expected_revision=2),
    )

    assert find_node(result.document, "actor").data == {
        "alignment": "center",
        "width": 80,
    }


def test_chat_allows_text_formatting_data_operation_without_evidence(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Formatted first paragraph",
        operations=[
            ChatEditOperation(
                operation=UpdateData(
                    node_id="actor",
                    data={
                        "style": {
                            "color": "blue",
                            "font-weight": "700",
                            "margin-left": "24px",
                        }
                    },
                ),
            )
        ],
    )

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="make first paragraph bold blue and indented", expected_revision=2),
    )

    assert find_node(result.document, "actor").data == {
        "style": {
            "color": "blue",
            "font-weight": "700",
            "margin-left": "24px",
        }
    }


def test_chat_merges_partial_formatting_data_with_existing_node_data(
    chat_service: ChatService,
    fake_model: FakeModel,
    session: Session,
) -> None:
    repository = DocumentRepository(session)
    document = repository.get_document("p1")
    assert document is not None
    nodes = [
        node.model_copy(update={"data": {"items": ["Existing item"]}})
        if node.id == "actor"
        else node
        for node in document.nodes
    ]
    repository.save_document("p1", document.model_copy(update={"nodes": nodes}))
    session.commit()
    fake_model.result = ChatEditPlan.model_validate(
        {
            "summary": "Formatted first paragraph",
            "operations": [
                {
                    "operation": {
                        "kind": "update_data",
                        "node_id": "actor",
                        "data.style": {
                            "color": "blue",
                            "font-weight": "700",
                        },
                    },
                    "evidence_block_ids": [],
                }
            ],
        }
    )

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="make first paragraph bold blue", expected_revision=3),
    )

    assert find_node(result.document, "actor").data == {
        "items": ["Existing item"],
        "style": {
            "color": "blue",
            "font-weight": "700",
        },
    }


def test_chat_prompt_documents_formatting_operations() -> None:
    from docgen.chat.service import CHAT_SYSTEM_PROMPT

    assert "update_data" in CHAT_SYSTEM_PROMPT
    assert "font-weight" in CHAT_SYSTEM_PROMPT
    assert "margin-left" in CHAT_SYSTEM_PROMPT


def _source_blocks(project_id: str) -> list[NormalizedBlock]:
    assert project_id == "p1"
    return [
        NormalizedBlock(
            id="s1:b2",
            kind=BlockKind.TEXT,
            text="Заявка подтверждается оператором",
            confidence=1,
        ),
        NormalizedBlock(
            id="s1:b3",
            kind=BlockKind.TEXT,
            text="Максимальный лимит составляет 10 000 рублей",
            confidence=1,
        ),
        NormalizedBlock(
            id="s1:b4",
            kind=BlockKind.TEXT,
            text="Температура составляет 5 °C",
            confidence=1,
        ),
        NormalizedBlock(
            id="s1:b5",
            kind=BlockKind.TEXT,
            text="Коэффициент включает значения 2 и 5",
            confidence=1,
        ),
        NormalizedBlock(
            id="s1:b6",
            kind=BlockKind.TEXT,
            text=(
                "Комиссия −2,5 %. Диапазон −5–10%. "
                "Сумма 2.5₽."
            ),
            confidence=1,
        ),
        NormalizedBlock(
            id="s1:b7",
            kind=BlockKind.TEXT,
            text="Лимит составляет 5",
            confidence=1,
        ),
        NormalizedBlock(
            id="s1:b8",
            kind=BlockKind.TEXT,
            text="Порог и резерв равны 5",
            confidence=1,
        ),
    ]
