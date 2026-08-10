import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from docgen.db import Base
from docgen.documents.edit_service import DocumentEditService, EditConflict, EditValidationError
from docgen.documents.models import ProjectArtifact
from docgen.documents.operations import DeleteNode, UpdateText, find_node
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.jobs.models import Job
from docgen.projects.models import Project
from docgen.sources.models import Source


@pytest.fixture
def artifact_session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=(Project.__table__, Source.__table__, ProjectArtifact.__table__, Job.__table__),
    )
    session = Session(engine)
    yield session
    session.close()
    Base.metadata.drop_all(
        engine,
        tables=(Job.__table__, ProjectArtifact.__table__, Source.__table__, Project.__table__),
    )
    engine.dispose()


@pytest.fixture
def saved_document(artifact_session: Session) -> WorkingDocument:
    project = Project(id="p1", name="Проект")
    artifact_session.add(project)
    artifact_session.flush()
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Первый текст"),
            DocumentNode(id="n2", kind=NodeKind.PARAGRAPH, text="Второй текст"),
        ],
    )
    repository = DocumentRepository(artifact_session)
    assert repository.save_document(project.id, document) == 1
    assert repository.save_document(project.id, document) == 2
    assert repository.save_document(project.id, document) == 3
    return document


@pytest.fixture
def document_repository(artifact_session: Session) -> DocumentRepository:
    return DocumentRepository(artifact_session)


@pytest.fixture
def edit_service(document_repository: DocumentRepository) -> DocumentEditService:
    return DocumentEditService(document_repository)


def test_apply_updates_text_and_increments_revision(
    edit_service: DocumentEditService, saved_document: WorkingDocument
) -> None:
    result = edit_service.apply(
        "p1",
        3,
        [UpdateText(node_id="n1", text="Новый текст")],
    )

    assert find_node(result.document, "n1").text == "Новый текст"
    assert result.revision == 4


def test_invalid_second_operation_rolls_back_first(
    edit_service: DocumentEditService,
    document_repository: DocumentRepository,
    saved_document: WorkingDocument,
) -> None:
    with pytest.raises(EditValidationError, match="Блок missing не найден"):
        edit_service.apply(
            "p1",
            3,
            [
                UpdateText(node_id="n1", text="Изменено"),
                DeleteNode(node_id="missing"),
            ],
        )

    assert document_repository.get_document("p1") == saved_document


def test_stale_revision_is_rejected(
    edit_service: DocumentEditService, saved_document: WorkingDocument
) -> None:
    with pytest.raises(EditConflict, match="Документ уже изменён"):
        edit_service.apply(
            "p1",
            2,
            [UpdateText(node_id="n1", text="Поздняя правка")],
        )
