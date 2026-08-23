from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from docgen.ai.client import ModelError, ModelResponseFormatError
from docgen.chat.errors import ChatError, ChatErrorCode
from docgen.chat.retrieval import SourceSnapshot
from docgen.chat.schemas import (
    ChatEditOperation,
    ChatEditPlan,
    ChatEditRequest,
    FaqEntryDraft,
    FaqPlacement,
)
from docgen.chat.service import (
    ChatGroundingError,
    ChatService,
    ChatValidationError,
)
from docgen.db import Base
from docgen.documents.models import ProjectArtifact
from docgen.documents.operations import DeleteNode, InsertNode, UpdateData, UpdateText, find_node
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import (
    CheckFinding,
    DocumentNode,
    NodeKind,
    Severity,
    WorkingDocument,
)
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.jobs.models import Job
from docgen.projects.models import Project
from docgen.sources.models import Source
from docgen.templates_catalog.loader import TemplateCatalog


class FakeModel:
    def __init__(self) -> None:
        self.calls = 0
        self.error: ModelError | None = None
        self.result: Any = None
        self.results: list[Any] = []
        self.systems: list[str] = []
        self.users: list[str] = []
        self.schemas: list[type] = []

    def generate_json(self, system: str, user: str, schema):
        self.calls += 1
        self.systems.append(system)
        self.users.append(user)
        self.schemas.append(schema)
        assert "Оператор" in user
        if self.error is not None:
            raise self.error
        if self.results:
            return self.results.pop(0)
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
        ChatEditRequest(
            message="Уточни подтверждение заявки по источнику",
            expected_revision=2,
        ),
    )

    assert result.revision == 3
    assert find_node(result.document, "actor").text == "Оператор подтверждает заявку"


def test_apply_finding_fix_runs_through_grounded_edit_pipeline(
    chat_service: ChatService, fake_model: FakeModel
) -> None:
    """A finding's suggested fix must go through the same source-grounded
    plan/validate path as a normal chat message, not bypass it."""
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
    finding = CheckFinding(
        code="c1",
        severity=Severity.WARNING,
        confidence=0.9,
        message="Не указано, что делает актор",
        evidence="Заявка подтверждается оператором",
        suggestion="Уточни, что оператор подтверждает заявку",
        node_id="actor",
        rule_id="structure-1",
    )

    result = chat_service.apply_finding_fix("p1", finding, expected_revision=2)

    assert result.revision == 3
    assert find_node(result.document, "actor").text == "Оператор подтверждает заявку"


def test_propose_finding_fix_does_not_save_and_is_limited_to_finding_node(
    chat_service: ChatService, fake_model: FakeModel, session: Session
) -> None:
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
    finding = CheckFinding(
        code="c1",
        severity=Severity.WARNING,
        confidence=0.9,
        message="Не указано действие актора",
        evidence="Заявка подтверждается оператором",
        suggestion="Уточни действие оператора",
        node_id="actor",
        rule_id="use-case-structure",
    )

    proposal = chat_service.propose_finding_fix("p1", finding, expected_revision=2)

    assert find_node(proposal.document, "actor").text == "Оператор подтверждает заявку"
    stored = DocumentRepository(session).get_document_with_revision("p1")
    assert stored is not None
    assert stored[1] == 2
    assert find_node(stored[0], "actor").text == "Оператор"


def test_propose_finding_fix_rejects_change_to_another_node(
    chat_service: ChatService, fake_model: FakeModel
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Изменён другой блок",
        operations=[
            ChatEditOperation(
                operation=UpdateText(
                    node_id="limit",
                    text="Оператор подтверждает заявку",
                ),
                evidence_block_ids=["s1:b2"],
            )
        ],
    )
    finding = CheckFinding(
        code="c1",
        severity=Severity.WARNING,
        confidence=0.9,
        message="Не указано действие актора",
        evidence="Заявка подтверждается оператором",
        suggestion="Уточни действие оператора",
        node_id="actor",
        rule_id="use-case-structure",
    )

    with pytest.raises(ChatValidationError, match="не относится к узлу"):
        chat_service.propose_finding_fix("p1", finding, expected_revision=2)


def test_apply_finding_fix_rejects_finding_without_suggestion(
    chat_service: ChatService, fake_model: FakeModel
) -> None:
    finding = CheckFinding(
        code="c1",
        severity=Severity.WARNING,
        confidence=0.9,
        message="Не указано, что делает актор",
        node_id="actor",
        rule_id="structure-1",
    )

    with pytest.raises(ChatValidationError):
        chat_service.apply_finding_fix("p1", finding, expected_revision=2)

    assert fake_model.calls == 0


def test_apply_finding_fix_rejects_stale_revision(
    chat_service: ChatService, fake_model: FakeModel
) -> None:
    finding = CheckFinding(
        code="c1",
        severity=Severity.WARNING,
        confidence=0.9,
        message="Не указано, что делает актор",
        suggestion="Уточни, что оператор подтверждает заявку",
        node_id="actor",
        rule_id="structure-1",
    )

    with pytest.raises(ChatError) as caught:
        chat_service.apply_finding_fix("p1", finding, expected_revision=1)

    assert caught.value.code is ChatErrorCode.REVISION_CONFLICT
    assert fake_model.calls == 0


def test_apply_finding_fix_rejects_whole_form_autofix_without_calling_model(
    chat_service: ChatService, fake_model: FakeModel
) -> None:
    """A structure-check finding names a missing corporate-form heading or
    table -- a structural fact, not a claim needing source evidence -- so
    it must be resolved deterministically, never through the LLM."""
    template = TemplateCatalog().get("use-case")
    contract = template.structure_check
    assert contract is not None
    finding = CheckFinding(
        code="template-structure-mismatch",
        severity=Severity.ERROR,
        confidence=1.0,
        message="В документе отсутствуют разделы формы",
        suggestion="Добавить недостающие разделы и таблицы формы",
        rule_id=contract.rule_id,
    )

    with pytest.raises(ChatValidationError, match="нельзя безопасно"):
        chat_service.apply_finding_fix(
            "p1", finding, expected_revision=2, template=template
        )
    assert fake_model.calls == 0


def test_chat_noop_plan_is_not_reported_as_a_successful_edit(
    chat_service: ChatService,
    fake_model: FakeModel,
    session: Session,
) -> None:
    fake_model.result = ChatEditPlan(summary="Нет правок", operations=[])

    with pytest.raises(ChatError) as caught:
        chat_service.edit(
            "p1",
            ChatEditRequest(message="как дела", expected_revision=2),
        )

    assert caught.value.code is ChatErrorCode.CLARIFICATION
    assert fake_model.calls == 0
    stored = DocumentRepository(session).get_document_with_revision("p1")
    assert stored is not None
    _, persisted_revision = stored
    assert persisted_revision == 2


@pytest.mark.parametrize(
    ("snapshot", "expected_code"),
    [
        (SourceSnapshot(configured_source_count=0), ChatErrorCode.SOURCES_MISSING),
        (
            SourceSnapshot(
                configured_source_count=1,
                warnings=("Confluence HTTP 503",),
            ),
            ChatErrorCode.SOURCE_UNAVAILABLE,
        ),
    ],
)
def test_chat_reports_source_state_before_model_call(
    session: Session,
    fake_model: FakeModel,
    snapshot: SourceSnapshot,
    expected_code: ChatErrorCode,
) -> None:
    service = ChatService(
        documents=DocumentRepository(session),
        model=fake_model,
        source_blocks=lambda _project_id: snapshot,
    )

    with pytest.raises(ChatError) as caught:
        service.edit(
            "p1",
            ChatEditRequest(message="Уточни лимит по источнику", expected_revision=2),
        )

    assert caught.value.code is expected_code
    assert fake_model.calls == 0


def test_chat_reports_missing_thematic_fragment_before_model_call(
    session: Session,
    fake_model: FakeModel,
) -> None:
    service = ChatService(
        documents=DocumentRepository(session),
        model=fake_model,
        source_blocks=lambda _project_id: SourceSnapshot(
            configured_source_count=1,
            blocks=_source_blocks("p1"),
        ),
    )

    with pytest.raises(ChatError) as caught:
        service.edit(
            "p1",
            ChatEditRequest(
                message="Уточни биометрическую идентификацию по источнику",
                expected_revision=2,
            ),
        )

    assert caught.value.code is ChatErrorCode.RELEVANT_FRAGMENT_MISSING
    assert fake_model.calls == 0


def test_chat_reports_invalid_model_json_and_preserves_document(
    chat_service: ChatService,
    fake_model: FakeModel,
    session: Session,
) -> None:
    fake_model.error = ModelResponseFormatError("raw model output must stay private")

    with pytest.raises(ChatError) as caught:
        chat_service.edit(
            "p1",
            ChatEditRequest(
                message="Уточни подтверждение заявки по источнику",
                expected_revision=2,
            ),
        )

    assert caught.value.code is ChatErrorCode.MODEL_INVALID_JSON
    assert "raw model output" not in caught.value.message
    assert fake_model.calls == 2
    stored = DocumentRepository(session).get_document_with_revision("p1")
    assert stored is not None and stored[1] == 2


def test_chat_rejects_invalid_model_operation_atomically(
    chat_service: ChatService,
    fake_model: FakeModel,
    session: Session,
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Невалидная правка",
        operations=[
            ChatEditOperation(
                operation=UpdateText(node_id="missing", text="Новый факт"),
                evidence_block_ids=["s1:b2"],
            )
        ],
    )

    with pytest.raises(ChatError) as caught:
        chat_service.edit(
            "p1",
            ChatEditRequest(
                message="Уточни подтверждение заявки по источнику",
                expected_revision=2,
            ),
        )

    assert caught.value.code is ChatErrorCode.INVALID_OPERATION
    stored = DocumentRepository(session).get_document_with_revision("p1")
    assert stored is not None and stored[1] == 2


def test_grounded_intent_rejects_model_structural_operation(
    chat_service: ChatService,
    fake_model: FakeModel,
    session: Session,
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Удалён блок",
        operations=[
            ChatEditOperation(
                operation=DeleteNode(node_id="actor"),
                evidence_block_ids=["s1:b2"],
            )
        ],
    )

    with pytest.raises(ChatError) as caught:
        chat_service.edit(
            "p1",
            ChatEditRequest(
                message="Уточни подтверждение заявки по источнику",
                expected_revision=2,
            ),
        )

    assert caught.value.code is ChatErrorCode.INVALID_OPERATION
    stored = DocumentRepository(session).get_document_with_revision("p1")
    assert stored is not None
    assert [node.id for node in stored[0].nodes] == ["actor", "limit"]
    assert stored[1] == 2


def test_chat_structures_document_without_model_or_sources(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="раздели на разделы", expected_revision=2),
    )

    assert fake_model.calls == 0
    assert [node.kind for node in result.document.nodes] == [
        NodeKind.HEADING,
        NodeKind.HEADING,
    ]
    assert [node.children[0].id for node in result.document.nodes] == ["actor", "limit"]


def test_chat_ambiguous_move_requests_target_details_without_model(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    with pytest.raises(ChatError) as caught:
        chat_service.edit(
            "p1",
            ChatEditRequest(message="перемести блоки", expected_revision=2),
        )

    assert caught.value.code is ChatErrorCode.CLARIFICATION
    assert fake_model.calls == 0


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
    assert fake_model.calls == 0
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

    assert result.summary == "Применена авторская правка"
    assert result.revision == 3
    inserted = result.document.nodes[-1]
    assert inserted.kind is NodeKind.PARAGRAPH
    assert inserted.text == "Вопрос: как пройти в библиотеку\nОтвет: прямо и направо"
    assert inserted.flags == ["manual-edit"]


def test_chat_applies_explicit_product_name_correction_without_model(
    chat_service: ChatService,
    fake_model: FakeModel,
    session: Session,
) -> None:
    repository = DocumentRepository(session)
    stored = repository.get_document_with_revision("p1")
    assert stored is not None
    document, revision = stored
    repository.save_document(
        "p1",
        document.model_copy(
            update={
                "title": "Руководство TracksCare",
                "nodes": [
                    DocumentNode(id="actor", kind=NodeKind.PARAGRAPH, text="TracksCare"),
                    DocumentNode(
                        id="faq",
                        kind=NodeKind.LIST,
                        data={"items": ["Как настроить TracksCare?"]},
                    ),
                ]
            }
        ),
    )

    result = chat_service.edit(
        "p1",
        ChatEditRequest(
            message="Наоборот, продукт должен быть TrackCare", expected_revision=revision + 1
        ),
    )

    assert fake_model.calls == 0
    assert result.summary == "Применена авторская правка"
    assert result.document.title == "Руководство TrackCare"
    assert result.document.nodes[0].text == "TrackCare"
    assert result.document.nodes[1].data["items"] == ["Как настроить TrackCare?"]
    assert result.document.nodes[0].flags == ["manual-edit"]
    assert result.document.nodes[1].flags == ["manual-edit"]


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
    assert result.summary == "Применена авторская правка"


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


def test_chat_unpositioned_manual_insert_bypasses_model(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.result = ChatEditPlan(summary="Нет правок", operations=[])

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="добавь авторский текст", expected_revision=2),
    )

    assert fake_model.calls == 0
    assert result.document.nodes[-1].text == "авторский текст"


def test_chat_unpositioned_manual_insert_does_not_depend_on_model(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.error = ModelError("Модель недоступна")

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="добавь авторский текст", expected_revision=2),
    )

    assert fake_model.calls == 0
    assert result.summary == "Применена авторская правка"
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

    with pytest.raises(ChatGroundingError) as caught:
        chat_service.edit(
            "p1",
            ChatEditRequest(message="Добавь лимит", expected_revision=2),
        )
    assert caught.value.code is ChatErrorCode.EVIDENCE_MISSING


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

    with pytest.raises(ChatGroundingError) as caught:
        chat_service.edit(
            "p1",
            ChatEditRequest(message="Добавь лимит", expected_revision=2),
        )
    assert caught.value.code is ChatErrorCode.EVIDENCE_MISSING

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

    topic = text.split()[0]
    with pytest.raises(ChatGroundingError) as caught:
        chat_service.edit(
            "p1",
            ChatEditRequest(
                message=f"Уточни {topic} по источнику",
                expected_revision=2,
            ),
        )
    assert caught.value.code is ChatErrorCode.GROUNDING_FAILED


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
        ChatEditRequest(message="Уточни комиссию по источнику", expected_revision=2),
    )

    assert find_node(result.document, "limit").text == (
        "Комиссия -2.5%. Диапазон -5-10 %. Сумма 2,5 ₽."
    )


def test_chat_allows_grounded_generated_questions_from_source(
    chat_service: ChatService,
    fake_model: FakeModel,
    session: Session,
) -> None:
    revision = _set_template(session, "faq")
    fake_model.result = FaqEntryDraft(
        question="Кто подтверждает заявку?",
        answer="Заявку подтверждает оператор.",
        placement=FaqPlacement(index=2),
        evidence_block_ids=["s1:b2"],
    )

    result = chat_service.edit(
        "p1",
        ChatEditRequest(
            message="Добавь вопрос кто подтверждает заявку?",
            expected_revision=revision,
        ),
    )

    assert result.document.nodes[-1].data["items"] == [
        "Вопрос: Кто подтверждает заявку?\nОтвет: Заявку подтверждает оператор."
    ]
    assert result.document.nodes[-1].provenance == [
        Provenance(
            source_id="s1",
            locator="paragraph-2",
            quote="Заявка подтверждается оператором",
        )
    ]


def test_chat_retries_faq_after_unknown_evidence_with_source_focused_prompt(
    chat_service: ChatService,
    fake_model: FakeModel,
    session: Session,
) -> None:
    revision = _set_template(session, "faq")
    fake_model.results = [
        FaqEntryDraft(
            question="Кто подтверждает заявку?",
            answer="Заявку подтверждает оператор.",
            placement=FaqPlacement(index=2),
            evidence_block_ids=["unknown"],
        ),
        FaqEntryDraft(
            question="Кто подтверждает заявку?",
            answer="Заявку подтверждает оператор.",
            placement=FaqPlacement(index=2),
            evidence_block_ids=["s1:b2"],
        ),
    ]

    result = chat_service.edit(
        "p1",
        ChatEditRequest(
            message="Добавь вопрос Кто подтверждает заявку?",
            expected_revision=revision,
        ),
    )

    assert fake_model.calls == 2
    assert fake_model.schemas == [FaqEntryDraft, FaqEntryDraft]
    assert "проверку источников" in fake_model.systems[-1]
    assert result.document.nodes[-1].provenance[0].source_id == "s1"


def test_chat_retries_grounded_request_after_invalid_evidence(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    invalid = ChatEditPlan(
        summary="Добавлены вопросы",
        operations=[
            ChatEditOperation(
                operation=InsertNode(
                    index=2,
                    node=DocumentNode(
                        id="questions-invalid",
                        kind=NodeKind.LIST,
                        data={"items": ["Как оператор подтверждает заявку?"]},
                    ),
                ),
            )
        ],
    )
    grounded = ChatEditPlan(
        summary="Добавлены вопросы",
        operations=[
            ChatEditOperation(
                operation=InsertNode(
                    index=2,
                    node=DocumentNode(
                        id="questions-grounded",
                        kind=NodeKind.LIST,
                        data={"items": ["Как оператор подтверждает заявку?"]},
                    ),
                ),
                evidence_block_ids=["s1:b2"],
            )
        ],
    )
    fake_model.results = [invalid, grounded]

    result = chat_service.edit(
        "p1",
        ChatEditRequest(
            message="Уточни подтверждение заявки по источнику",
            expected_revision=2,
        ),
    )

    assert fake_model.calls == 2
    assert "не прошёл проверку источников" in fake_model.systems[-1].casefold()
    assert result.document.nodes[-1].data["items"] == [
        "Как оператор подтверждает заявку?"
    ]
    assert result.document.nodes[-1].provenance[0].source_id == "s1"


def test_chat_rejects_empty_grounded_plan(
    chat_service: ChatService, fake_model: FakeModel
) -> None:
    fake_model.result = ChatEditPlan(summary="Нет правок")

    with pytest.raises(ChatGroundingError) as caught:
        chat_service.edit(
            "p1",
            ChatEditRequest(
                message="Уточни подтверждение заявки по источнику",
                expected_revision=2,
            ),
        )
    assert caught.value.code is ChatErrorCode.EVIDENCE_MISSING


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
        "style": {"text-align": "center"}
    }
    assert fake_model.calls == 0


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
    assert fake_model.calls == 0


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
    assert fake_model.calls == 0


def test_chat_prompt_documents_formatting_operations() -> None:
    from docgen.chat.service import CHAT_SYSTEM_PROMPT

    assert "update_data" in CHAT_SYSTEM_PROMPT
    assert "font-weight" in CHAT_SYSTEM_PROMPT
    assert "margin-left" in CHAT_SYSTEM_PROMPT
    assert "Структурные команды" in CHAT_SYSTEM_PROMPT


def _set_template(session: Session, template_id: str) -> int:
    repository = DocumentRepository(session)
    stored = repository.get_document_with_revision("p1")
    assert stored is not None
    document, revision = stored
    repository.save_document(
        "p1",
        document.model_copy(
            update={
                "template_id": template_id,
                "build_template_id": template_id,
            }
        ),
    )
    session.commit()
    return revision + 1


def _source_blocks(project_id: str) -> list[NormalizedBlock]:
    assert project_id == "p1"
    return [
        NormalizedBlock(
            id="s1:b2",
            kind=BlockKind.TEXT,
            text="Заявка подтверждается оператором",
            provenance=[
                Provenance(
                    source_id="s1",
                    locator="paragraph-2",
                    quote="Заявка подтверждается оператором",
                )
            ],
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
