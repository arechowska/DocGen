import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from docgen.db import Base
from docgen.documents.models import ProjectArtifact
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import CheckReport, DocumentNode, NodeKind, WorkingDocument
from docgen.jobs.models import Job
from docgen.projects.models import Project
from docgen.projects.repository import ProjectRepository
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


def test_replacing_document_increments_revision_and_invalidates_report(
    artifact_session: Session,
) -> None:
    project = ProjectRepository(artifact_session).create("Проект")
    repository = DocumentRepository(artifact_session)
    first = WorkingDocument(
        title="Первая версия",
        template_id="use-case",
        nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Первая")],
    )
    latest = first.model_copy(update={"title": "Последняя версия"})
    report = CheckReport(template_id="use-case", passed_rule_ids=["rule-1"])

    assert repository.save_document(project.id, first) == 1
    repository.save_report(project.id, report)
    assert repository.get_report(project.id) == report
    assert repository.save_document(project.id, latest) == 2

    assert repository.get_document(project.id) == latest
    assert repository.get_report(project.id) is None


def test_report_is_hidden_when_bound_revision_does_not_match_document(
    artifact_session: Session,
) -> None:
    project = ProjectRepository(artifact_session).create("Проект")
    repository = DocumentRepository(artifact_session)
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Текст")],
    )
    report = CheckReport(template_id="use-case", passed_rule_ids=["rule-1"])
    repository.save_document(project.id, document)
    repository.save_report(project.id, report)
    artifact = artifact_session.get(ProjectArtifact, project.id)
    assert artifact is not None
    artifact.report_revision = artifact.document_revision + 1
    artifact_session.flush()

    assert repository.get_report(project.id) is None


def test_standalone_publish_binds_document_and_report_to_one_revision(
    artifact_session: Session,
) -> None:
    project = ProjectRepository(artifact_session).create("Проект")
    repository = DocumentRepository(artifact_session)
    document = WorkingDocument(
        title="Загруженный документ",
        template_id="use-case",
        nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Текст")],
    )
    report = CheckReport(template_id="use-case", passed_rule_ids=["rule-1"])

    revision = repository.save_document_and_report(project.id, document, report)
    artifact = artifact_session.get(ProjectArtifact, project.id)

    assert revision == 1
    assert artifact is not None
    assert artifact.document_revision == artifact.report_revision == 1
    assert repository.get_document_at_revision(project.id, 1) == document
    assert repository.get_report_at_revision(project.id, 1) == report


def test_report_compare_and_set_rejects_replaced_document(
    artifact_session: Session,
) -> None:
    project = ProjectRepository(artifact_session).create("Проект")
    repository = DocumentRepository(artifact_session)
    first = WorkingDocument(
        title="A", template_id="use-case", nodes=[DocumentNode(kind=NodeKind.GAP)]
    )
    second = first.model_copy(update={"title": "B"})
    checked_revision = repository.save_document(project.id, first)
    repository.save_document(project.id, second)

    with pytest.raises(ValueError, match="изменён"):
        repository.save_report(
            project.id,
            CheckReport(template_id="use-case", passed_rule_ids=["rule-1"]),
            expected_document_revision=checked_revision,
        )

    assert repository.get_report(project.id) is None


def test_deleting_project_removes_current_document_and_report(artifact_session: Session) -> None:
    project_repository = ProjectRepository(artifact_session)
    project = project_repository.create("Проект")
    document_repository = DocumentRepository(artifact_session)
    document = WorkingDocument(
        title="Use Case",
        template_id="use-case",
        nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Оплата")],
    )
    report = CheckReport(template_id="use-case", passed_rule_ids=["rule-1"])
    document_repository.save_document(project.id, document)
    document_repository.save_report(project.id, report)

    assert project_repository.delete(project.id) is True

    assert document_repository.get_document(project.id) is None
    assert document_repository.get_report(project.id) is None
