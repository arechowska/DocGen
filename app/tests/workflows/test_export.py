from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from docgen.config import Settings
from docgen.db import Base
from docgen.documents.models import ProjectArtifact
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.export.protocol import RenderedFile
from docgen.export.service import ExportService
from docgen.export.storage import ExportStorage
from docgen.formatting.catalog import FormattingCatalog
from docgen.formatting.schemas import OutputFormat
from docgen.jobs.models import Job, JobKind
from docgen.jobs.repository import JobRepository
from docgen.jobs.runner import WorkflowDependencies, build_workflows
from docgen.projects.models import Project
from docgen.projects.repository import ProjectRepository
from docgen.sources.models import Source
from docgen.workflows.assemble import WorkflowError
from docgen.workflows.export import ExportWorkflow


@dataclass
class ProgressSpy:
    events: list[str] = field(default_factory=list)

    def __call__(self, value: int, status_message: str | None = None) -> None:
        del status_message
        self.events.append(f"progress:{value}")

    def checkpoint(self) -> None:
        self.events.append("checkpoint")

    def report_warnings(self, warnings: list[str]) -> None:
        self.events.extend(f"warning:{warning}" for warning in warnings)


class _FakeExporter:
    def __init__(self, rendered: RenderedFile | None = None, error: Exception | None = None) -> None:
        self._rendered = rendered
        self._error = error
        self.calls = 0

    def render(self, document: WorkingDocument, template: object) -> RenderedFile:
        del document, template
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._rendered is not None
        return self._rendered


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=(Project.__table__, Source.__table__, ProjectArtifact.__table__, Job.__table__),
    )
    database_session = Session(engine)
    yield database_session
    database_session.close()
    Base.metadata.drop_all(
        engine,
        tables=(Job.__table__, ProjectArtifact.__table__, Source.__table__, Project.__table__),
    )
    engine.dispose()


@pytest.fixture
def projects(session: Session) -> ProjectRepository:
    return ProjectRepository(session)


@pytest.fixture
def documents(session: Session) -> DocumentRepository:
    return DocumentRepository(session)


@pytest.fixture
def project_id(projects: ProjectRepository) -> str:
    return projects.create("Проект").id


@pytest.fixture
def catalog_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "catalog"
    directory.mkdir()
    (directory / "docgen-light-html.yaml").write_text(
        "id: docgen-light\n"
        "name: Без шаблона\n"
        "format: html\n"
        "renderer: html\n",
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def catalog(catalog_dir: Path) -> FormattingCatalog:
    return FormattingCatalog(catalog_dir)


@pytest.fixture
def storage(tmp_path: Path) -> ExportStorage:
    return ExportStorage(tmp_path / "data")


@pytest.fixture
def jobs(session: Session) -> JobRepository:
    return JobRepository(session, worker_id="export-workflow-test")


def _document() -> WorkingDocument:
    return WorkingDocument(
        title="Документ",
        template_id="docgen-light",
        nodes=[DocumentNode(kind=NodeKind.PARAGRAPH, text="Текст документа")],
    )


def _claimed_export_job(
    jobs: JobRepository, project_id: str, *, revision: int = 1
) -> Job:
    jobs.enqueue(
        project_id,
        JobKind.EXPORT,
        "docgen-light",
        export_format=OutputFormat.HTML,
        requested_document_revision=revision,
    )
    claimed = jobs.claim_next()
    assert claimed is not None
    return claimed


def _workflow(
    projects: ProjectRepository,
    documents: DocumentRepository,
    catalog: FormattingCatalog,
    storage: ExportStorage,
    jobs: JobRepository,
    exporter: _FakeExporter,
) -> ExportWorkflow:
    service = ExportService(documents, catalog, storage, {OutputFormat.HTML: exporter})
    return ExportWorkflow(projects=projects, service=service, jobs=jobs)


def test_export_workflow_renders_and_records_result(
    projects: ProjectRepository,
    documents: DocumentRepository,
    catalog: FormattingCatalog,
    storage: ExportStorage,
    jobs: JobRepository,
    project_id: str,
) -> None:
    source_document = _document()
    documents.save_document(project_id, source_document)
    job = _claimed_export_job(jobs, project_id)
    rendered = RenderedFile(
        filename="document.html", media_type="text/html", content=b"<p>ok</p>"
    )
    exporter = _FakeExporter(rendered=rendered)
    workflow = _workflow(projects, documents, catalog, storage, jobs, exporter)
    progress = ProgressSpy()

    result = workflow.run(job, progress)

    assert exporter.calls == 1
    assert result.document_revision == 1
    assert result.filename == "document-docgen-light.html"
    assert "progress:100" in progress.events
    stored_job = jobs.get(job.id)
    assert stored_job is not None
    assert stored_job.export_relative_path == result.relative_path
    assert stored_job.export_filename == result.filename
    assert stored_job.export_media_type == "text/html"
    assert stored_job.export_size_bytes == len(rendered.content)
    assert stored_job.export_document_revision == 1
    stored_path = storage.resolve(result.relative_path)
    assert stored_path.read_bytes() == rendered.content
    assert documents.get_document_with_revision(project_id) == (source_document, 1)


def test_export_workflow_rejects_wrong_job_kind(
    projects: ProjectRepository,
    documents: DocumentRepository,
    catalog: FormattingCatalog,
    storage: ExportStorage,
    jobs: JobRepository,
    project_id: str,
) -> None:
    jobs.enqueue(project_id, JobKind.ASSEMBLE, "docgen-light")
    job = jobs.claim_next()
    assert job is not None
    exporter = _FakeExporter(rendered=RenderedFile(filename="d.html", media_type="text/html", content=b"x"))
    workflow = _workflow(projects, documents, catalog, storage, jobs, exporter)

    with pytest.raises(WorkflowError, match="Некорректный тип задания для экспорта"):
        workflow.run(job, ProgressSpy())

    assert exporter.calls == 0


def test_export_workflow_requires_project(
    projects: ProjectRepository,
    documents: DocumentRepository,
    catalog: FormattingCatalog,
    storage: ExportStorage,
    jobs: JobRepository,
    project_id: str,
    session: Session,
) -> None:
    documents.save_document(project_id, _document())
    job = _claimed_export_job(jobs, project_id)
    project = session.get(Project, project_id)
    assert project is not None
    session.delete(project)
    session.flush()
    exporter = _FakeExporter(rendered=RenderedFile(filename="d.html", media_type="text/html", content=b"x"))
    workflow = _workflow(projects, documents, catalog, storage, jobs, exporter)

    with pytest.raises(WorkflowError, match="Проект не найден"):
        workflow.run(job, ProgressSpy())

    assert exporter.calls == 0


def test_export_workflow_requires_document(
    projects: ProjectRepository,
    documents: DocumentRepository,
    catalog: FormattingCatalog,
    storage: ExportStorage,
    jobs: JobRepository,
    project_id: str,
) -> None:
    """No document was ever saved, so the requested revision can never be
    found. `ExportService.export()` -- not this workflow -- is the sole
    place that detects this (see `test_export_workflow_uses_requested_
    revision_not_current_revision_at_run_time` below for why the workflow
    must not duplicate that check itself), so it surfaces as the same
    ExportError a mid-flight document change would."""
    from docgen.export.protocol import ExportError

    job = _claimed_export_job(jobs, project_id, revision=1)
    exporter = _FakeExporter(rendered=RenderedFile(filename="d.html", media_type="text/html", content=b"x"))
    workflow = _workflow(projects, documents, catalog, storage, jobs, exporter)

    with pytest.raises(ExportError, match="Документ изменён; запустите экспорт повторно"):
        workflow.run(job, ProgressSpy())

    assert exporter.calls == 0


def test_export_workflow_uses_requested_revision_not_current_revision_at_run_time(
    projects: ProjectRepository,
    documents: DocumentRepository,
    catalog: FormattingCatalog,
    storage: ExportStorage,
    jobs: JobRepository,
    project_id: str,
) -> None:
    """Reproduces the finding-3 bug: a document is edited while an export
    job for its earlier revision sits queued. The job must never silently
    export whatever revision happens to be current when it finally runs --
    it must fail loudly, using the exact revision requested at submit time.
    """
    from docgen.export.protocol import ExportError

    documents.save_document(project_id, _document())  # revision 1
    job = _claimed_export_job(jobs, project_id, revision=1)
    assert job.requested_document_revision == 1

    # The document changes while the job is still queued/running.
    documents.save_document(project_id, _document())  # revision 2

    exporter = _FakeExporter(
        rendered=RenderedFile(filename="d.html", media_type="text/html", content=b"x")
    )
    workflow = _workflow(projects, documents, catalog, storage, jobs, exporter)

    with pytest.raises(ExportError, match="Документ изменён; запустите экспорт повторно"):
        workflow.run(job, ProgressSpy())

    # Must never have rendered the (wrong, now-current) revision.
    assert exporter.calls == 0


def test_export_workflow_propagates_export_errors_without_recording_result(
    projects: ProjectRepository,
    documents: DocumentRepository,
    catalog: FormattingCatalog,
    storage: ExportStorage,
    jobs: JobRepository,
    project_id: str,
) -> None:
    from docgen.export.protocol import ExportError

    documents.save_document(project_id, _document())
    job = _claimed_export_job(jobs, project_id)
    exporter = _FakeExporter(error=ExportError("Не удалось сформировать файл"))
    workflow = _workflow(projects, documents, catalog, storage, jobs, exporter)

    with pytest.raises(ExportError):
        workflow.run(job, ProgressSpy())

    stored_job = jobs.get(job.id)
    assert stored_job is not None
    assert stored_job.export_relative_path is None


def test_build_workflows_registers_export_only_when_dependencies_given(
    projects: ProjectRepository,
    documents: DocumentRepository,
    jobs: JobRepository,
    catalog: FormattingCatalog,
    storage: ExportStorage,
    tmp_path: Path,
) -> None:
    from docgen.templates_catalog.loader import TemplateCatalog

    class _FakeNormalization:
        def run(self, project_id: str, before_extract=None) -> None:
            raise AssertionError("not used in this test")

    settings = Settings(_env_file=None, data_dir=tmp_path)
    without_export = build_workflows(
        settings,
        WorkflowDependencies(
            projects=projects,
            normalization=_FakeNormalization(),  # type: ignore[arg-type]
            templates=TemplateCatalog(),
            documents=documents,
        ),
    )
    assert JobKind.EXPORT not in without_export

    service = ExportService(documents, catalog, storage, {OutputFormat.HTML: _FakeExporter()})
    with_export = build_workflows(
        settings,
        WorkflowDependencies(
            projects=projects,
            normalization=_FakeNormalization(),  # type: ignore[arg-type]
            templates=TemplateCatalog(),
            documents=documents,
            jobs=jobs,
            export_service=service,
        ),
    )
    assert isinstance(with_export[JobKind.EXPORT], ExportWorkflow)
