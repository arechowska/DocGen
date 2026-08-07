import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from docgen.db import Base
from docgen.documents.models import ProjectArtifact
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import CheckReport, DocumentNode, NodeKind, WorkingDocument
from docgen.projects.models import Project
from docgen.projects.repository import ProjectRepository
from docgen.sources.models import Source


@pytest.fixture
def artifact_session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=(Project.__table__, Source.__table__, ProjectArtifact.__table__)
    )
    session = Session(engine)
    yield session
    session.close()
    Base.metadata.drop_all(
        engine, tables=(ProjectArtifact.__table__, Source.__table__, Project.__table__)
    )
    engine.dispose()


def test_repository_replaces_current_document_for_project(artifact_session: Session) -> None:
    project = ProjectRepository(artifact_session).create("Проект")
    repository = DocumentRepository(artifact_session)
    first = WorkingDocument(
        title="Первая версия",
        template_id="use-case",
        nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Первая")],
    )
    latest = WorkingDocument(
        title="Последняя версия",
        template_id="use-case",
        nodes=[DocumentNode(id="n2", kind=NodeKind.PARAGRAPH, text="Последняя")],
    )

    repository.save_document(project.id, first)
    repository.save_document(project.id, latest)

    assert repository.get_document(project.id) == latest


def test_repository_persists_report_independently_of_document(artifact_session: Session) -> None:
    project = ProjectRepository(artifact_session).create("Проект")
    repository = DocumentRepository(artifact_session)
    report = CheckReport(template_id="use-case", unchecked_rules=["rule-1"])

    repository.save_report(project.id, report)

    assert repository.get_document(project.id) is None
    assert repository.get_report(project.id) == report


def test_deleting_project_removes_current_document_and_report(artifact_session: Session) -> None:
    project_repository = ProjectRepository(artifact_session)
    project = project_repository.create("Проект")
    document_repository = DocumentRepository(artifact_session)
    document = WorkingDocument(
        title="Use Case",
        template_id="use-case",
        nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Оплата")],
    )
    report = CheckReport(template_id="use-case", unchecked_rules=["rule-1"])
    document_repository.save_document(project.id, document)
    document_repository.save_report(project.id, report)

    assert project_repository.delete(project.id) is True

    assert document_repository.get_document(project.id) is None
    assert document_repository.get_report(project.id) is None
