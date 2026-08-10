import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from docgen.chat.schemas import ChatEditPlan, ChatEditRequest
from docgen.chat.service import ChatGroundingError, ChatService
from docgen.db import Base
from docgen.documents.models import ProjectArtifact
from docgen.documents.operations import UpdateText, find_node
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import BlockKind, NormalizedBlock
from docgen.jobs.models import Job
from docgen.projects.models import Project
from docgen.sources.models import Source


class FakeModel:
    def __init__(self) -> None:
        self.result: ChatEditPlan | None = None

    def generate_json(self, system: str, user: str, schema):
        assert "русском языке" in system
        assert "Оператор" in user
        assert schema is ChatEditPlan
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
        operations=[UpdateText(node_id="actor", text="Оператор")],
        evidence_block_ids=["s1:b2"],
    )

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="Уточни актора", expected_revision=2),
    )

    assert result.revision == 3
    assert find_node(result.document, "actor").text == "Оператор"


def test_chat_rejects_unknown_evidence(
    chat_service: ChatService, fake_model: FakeModel
) -> None:
    fake_model.result = ChatEditPlan(
        summary="Добавлен лимит",
        operations=[UpdateText(node_id="limit", text="10 000")],
        evidence_block_ids=["unknown"],
    )

    with pytest.raises(
        ChatGroundingError,
        match="Для этой правки нет подтверждения в источниках",
    ):
        chat_service.edit(
            "p1",
            ChatEditRequest(message="Добавь лимит", expected_revision=2),
        )


def _source_blocks(project_id: str) -> list[NormalizedBlock]:
    assert project_id == "p1"
    return [
        NormalizedBlock(
            id="s1:b2",
            kind=BlockKind.TEXT,
            text="Актор: Оператор",
            confidence=1,
        )
    ]
