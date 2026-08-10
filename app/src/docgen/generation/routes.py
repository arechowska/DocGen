from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import SecretStr
from sqlalchemy.orm import Session

from docgen.ai.client import ModelConfigurationError, build_text_model, build_vision_model
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import CheckReport, WorkingDocument
from docgen.jobs.models import Job, JobKind, JobStatus
from docgen.jobs.repository import ActiveProjectJobExists, JobRepository
from docgen.models import Project, Source, SourceKind
from docgen.projects.repository import ProjectRepository
from docgen.projects.routes import get_session
from docgen.sources.repository import SourceRepository
from docgen.templates_catalog.loader import TemplateCatalog, TemplateConfigurationError
from docgen.web import templates

router = APIRouter(prefix="/projects")

SessionDependency = Annotated[Session, Depends(get_session)]

_ACTIVE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})
_FAILED_MESSAGE = "Не удалось обработать источники"
_PROJECT_ACTIVE_MESSAGE = "Проект уже обрабатывается"


@router.post("/{project_id}/jobs/assemble", status_code=status.HTTP_202_ACCEPTED)
def start_assemble(
    request: Request,
    project_id: str,
    template_id: Annotated[str, Form()],
    session: SessionDependency,
) -> Response:
    return _start_job(request, session, project_id, JobKind.ASSEMBLE, template_id)


@router.post("/{project_id}/jobs/check", status_code=status.HTTP_202_ACCEPTED)
def start_check(
    request: Request,
    project_id: str,
    template_id: Annotated[str, Form()],
    session: SessionDependency,
) -> Response:
    return _start_job(request, session, project_id, JobKind.CHECK, template_id)


@router.get("/{project_id}/jobs/{job_id}")
def job_status(
    request: Request,
    project_id: str,
    job_id: str,
    session: SessionDependency,
) -> Response:
    _project_or_404(session, project_id)
    job = _owned_job_or_404(session, project_id, job_id)
    return _job_response(request, session, job)


@router.post("/{project_id}/jobs/{job_id}/cancel")
def cancel_job(
    request: Request,
    project_id: str,
    job_id: str,
    session: SessionDependency,
) -> Response:
    _project_or_404(session, project_id)
    job = _owned_job_or_404(session, project_id, job_id)
    JobRepository(session).request_cancel(job.id)
    refreshed_job = _owned_job_or_404(session, project_id, job_id)
    if not refreshed_job.cancel_requested:
        return _job_response(request, session, refreshed_job)
    return _status_response(request, refreshed_job, notice="Отмена запрошена")


@router.get("/{project_id}/document")
def document_view(request: Request, project_id: str, session: SessionDependency) -> Response:
    _project_or_404(session, project_id)
    document = DocumentRepository(session).get_document(project_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Документ не найден",
        )
    return _document_response(request, project_id, document, standalone=True)


@router.get("/{project_id}/report")
def report_view(request: Request, project_id: str, session: SessionDependency) -> Response:
    _project_or_404(session, project_id)
    report = DocumentRepository(session).get_report(project_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Отчёт не найден",
        )
    return _report_response(request, project_id, report, standalone=True)


def _start_job(
    request: Request,
    session: Session,
    project_id: str,
    kind: JobKind,
    template_id: str,
) -> Response:
    project = _project_or_404(session, project_id)
    sources = SourceRepository(session).list_for_project(project_id)
    if not sources:
        return _setup_error(request, session, project, "Добавьте хотя бы один источник", 422)

    catalog = TemplateCatalog()
    try:
        template = catalog.get(template_id)
    except TemplateConfigurationError:
        return _setup_error(
            request, session, project, "Шаблон не найден", 422, catalog=catalog
        )

    if kind is JobKind.CHECK:
        document = DocumentRepository(session).get_document(project_id)
        if document is None:
            return _setup_error(
                request,
                session,
                project,
                "Документ для проверки не найден",
                422,
                catalog=catalog,
            )
        if document.template_id != template.id:
            return _setup_error(
                request,
                session,
                project,
                "Документ создан для другого шаблона",
                422,
                catalog=catalog,
            )

    dependency_error = _dependency_error(request, sources)
    if dependency_error is not None:
        return _setup_error(
            request, session, project, dependency_error, 503, catalog=catalog
        )

    try:
        job = JobRepository(session).enqueue_if_project_idle(project_id, kind, template.id)
    except ActiveProjectJobExists:
        active_job = JobRepository(session).get_active_for_project(project_id)
        if active_job is not None:
            return _status_response(
                request,
                active_job,
                notice=_PROJECT_ACTIVE_MESSAGE,
                status_code=status.HTTP_409_CONFLICT,
            )
        return _setup_error(
            request, session, project, _PROJECT_ACTIVE_MESSAGE, 409, catalog=catalog
        )
    return _status_response(
        request,
        job,
        notice=(
            "Сборка поставлена в очередь"
            if kind is JobKind.ASSEMBLE
            else "Проверка поставлена в очередь"
        ),
        status_code=status.HTTP_202_ACCEPTED,
    )


def _dependency_error(request: Request, sources: list[Source]) -> str | None:
    try:
        build_text_model(request.app.state.settings)
        build_vision_model(request.app.state.settings)
    except ModelConfigurationError:
        return "Локальные модели не настроены"

    if any(source.kind is SourceKind.CONFLUENCE for source in sources):
        settings = request.app.state.settings
        token = settings.confluence_token
        token_value = token.get_secret_value() if isinstance(token, SecretStr) else token
        if settings.confluence_api_base is None or not token_value or not token_value.strip():
            return "Интеграция Confluence не настроена"
    return None


def _project_or_404(session: Session, project_id: str) -> Project:
    project = ProjectRepository(session).get(project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Проект не найден",
        )
    return project


def _owned_job_or_404(session: Session, project_id: str, job_id: str) -> Job:
    job = JobRepository(session).get(job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Задание не найдено",
        )
    return job


def _setup_error(
    request: Request,
    session: Session,
    project: Project,
    message: str,
    status_code: int,
    *,
    catalog: TemplateCatalog | None = None,
) -> Response:
    template_catalog = catalog or TemplateCatalog()
    documents = DocumentRepository(session)
    return templates.TemplateResponse(
        request=request,
        name="generation/setup.html",
        context={
            "project": project,
            "templates": template_catalog.list(),
            "generation_error": message,
            "setup_fragment": True,
            "has_document": documents.get_document(project.id) is not None,
            "has_report": documents.get_report(project.id) is not None,
        },
        status_code=status_code,
    )


def _status_response(
    request: Request,
    job: Job,
    *,
    notice: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="generation/status.html",
        context={
            "job": job,
            "is_active": job.status in _ACTIVE_STATUSES,
            "notice": notice,
            "safe_error": _FAILED_MESSAGE if job.status is JobStatus.FAILED else None,
        },
        status_code=status_code,
    )


def _job_response(request: Request, session: Session, job: Job) -> Response:
    if job.status is JobStatus.SUCCEEDED:
        documents = DocumentRepository(session)
        if job.kind is JobKind.ASSEMBLE:
            document = documents.get_document(job.project_id)
            if document is not None:
                return _document_response(
                    request, job.project_id, document, standalone=False
                )
        else:
            report = documents.get_report(job.project_id)
            if report is not None:
                return _report_response(request, job.project_id, report, standalone=False)
    return _status_response(request, job)


def _document_response(
    request: Request,
    project_id: str,
    document: WorkingDocument,
    *,
    standalone: bool,
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="generation/result.html",
        context={
            "project_id": project_id,
            "document": document,
            "standalone": standalone,
        },
    )


def _report_response(
    request: Request,
    project_id: str,
    report: CheckReport,
    *,
    standalone: bool,
) -> Response:
    confirmed = [finding for finding in report.findings if finding.confidence >= 0.7]
    low_confidence = [finding for finding in report.findings if finding.confidence < 0.7]
    return templates.TemplateResponse(
        request=request,
        name="generation/report.html",
        context={
            "project_id": project_id,
            "report": report,
            "confirmed": confirmed,
            "low_confidence": low_confidence,
            "standalone": standalone,
        },
    )


__all__ = ["router"]
