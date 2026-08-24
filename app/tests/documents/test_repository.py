import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from docgen.db import Base
from docgen.documents.models import CheckReportRecord, ProjectArtifact
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
        tables=(
            Project.__table__,
            Source.__table__,
            ProjectArtifact.__table__,
            CheckReportRecord.__table__,
            Job.__table__,
        ),
    )
    session = Session(engine)
    yield session
    session.close()
    Base.metadata.drop_all(
        engine,
        tables=(
            Job.__table__,
            CheckReportRecord.__table__,
            ProjectArtifact.__table__,
            Source.__table__,
            Project.__table__,
        ),
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


def test_create_document_only_writes_first_revision(artifact_session: Session) -> None:
    project = ProjectRepository(artifact_session).create("Проект")
    repository = DocumentRepository(artifact_session)
    first = WorkingDocument(
        title="Первый документ",
        template_id="no-template",
        nodes=[],
    )
    replacement = first.model_copy(update={"title": "Нельзя заменить"})

    assert repository.create_document(project.id, first) == 1
    assert repository.create_document(project.id, replacement) is None

    assert repository.get_document_with_revision(project.id) == (first, 1)


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


def test_repository_keeps_independent_check_history_for_two_profiles(
    artifact_session: Session,
) -> None:
    project = ProjectRepository(artifact_session).create("Проект")
    repository = DocumentRepository(artifact_session)
    document = WorkingDocument(
        title="Импортированный документ",
        template_id="no-template",
        nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Текст")],
    )
    repository.save_document(project.id, document)

    repository.save_report(
        project.id,
        CheckReport(template_id="use-case", passed_rule_ids=["uc-rule"]),
        expected_document_revision=1,
        target_source_id="source-1",
    )
    repository.save_report(
        project.id,
        CheckReport(template_id="faq", passed_rule_ids=["faq-rule"]),
        expected_document_revision=1,
        target_source_id="source-1",
    )

    history = repository.list_check_reports(project.id, document_revision=1)
    assert [item.check_profile_id for item in history] == ["use-case", "faq"]
    assert history[0].report.passed_rule_ids == ("uc-rule",)
    assert history[1].report.passed_rule_ids == ("faq-rule",)


def test_workspace_save_uses_revision_and_stores_html(
    artifact_session: Session,
) -> None:
    project = ProjectRepository(artifact_session).create("Проект")
    repository = DocumentRepository(artifact_session)
    first = WorkingDocument(
        title="Первая версия",
        template_id="faq",
        nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Первая")],
    )
    saved = first.model_copy(update={"title": "Ручная правка"})
    repository.save_document(project.id, first)

    revision = repository.save_workspace(
        project.id,
        expected_revision=1,
        document=saved,
        html='<p data-node-id="n1">Ручная правка</p>',
    )

    assert revision == 2
    assert repository.get_document(project.id) == saved
    assert repository.get_workspace_html(project.id) == (
        '<p data-node-id="n1">Ручная правка</p>'
    )

    stale_revision = repository.save_workspace(
        project.id,
        expected_revision=1,
        document=first,
        html="<p>Устаревшее</p>",
    )

    assert stale_revision is None
    assert repository.get_document(project.id) == saved
    assert repository.get_workspace_html(project.id) == (
        '<p data-node-id="n1">Ручная правка</p>'
    )


def test_structured_document_writes_clear_stale_workspace_html(
    artifact_session: Session,
) -> None:
    project = ProjectRepository(artifact_session).create("Проект")
    repository = DocumentRepository(artifact_session)
    first = WorkingDocument(
        title="Первая версия",
        template_id="faq",
        nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Первая")],
    )
    second = first.model_copy(update={"title": "Вторая версия"})
    third = first.model_copy(update={"title": "Третья версия"})
    repository.save_document(project.id, first)
    repository.save_workspace(project.id, 1, first, "<p>Снимок 1</p>")

    assert repository.save_document(project.id, second) == 3
    assert repository.get_workspace_html(project.id) is None

    repository.save_workspace(project.id, 3, second, "<p>Снимок 2</p>")
    assert repository.replace_document(project.id, 4, third) == 5
    assert repository.get_workspace_html(project.id) is None


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
