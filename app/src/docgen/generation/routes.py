from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from pydantic import SecretStr
from sqlalchemy.orm import Session

from docgen.ai.client import ModelConfigurationError, build_text_model
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import CheckReport, WorkingDocument
from docgen.extraction.confluence import ConfluenceClient
from docgen.extraction.registry import ExtractionError
from docgen.jobs.models import Job, JobKind, JobStatus
from docgen.jobs.repository import (
    ActiveProjectJobExists,
    JobRepository,
    JobTargetUnavailable,
)
from docgen.models import Project, Source, SourceKind
from docgen.projects.repository import ProjectRepository
from docgen.projects.routes import get_session
from docgen.sources.repository import SourceRepository
from docgen.templates_catalog.loader import TemplateCatalog, TemplateConfigurationError
from docgen.web import templates

from .targets import is_supported_check_target

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
    target_source_id: Annotated[str | None, Form()] = None,
) -> Response:
    return _start_job(
        request,
        session,
        project_id,
        JobKind.CHECK,
        template_id,
        target_source_id=target_source_id or None,
    )


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
    if _wants_full_page(request):
        return RedirectResponse(
            url=f"/projects/{project_id}/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
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
    *,
    target_source_id: str | None = None,
) -> Response:
    project = _project_or_404(session, project_id)
    sources = SourceRepository(session).list_for_project(project_id)
    if not sources:
        return _setup_error(request, session, project, "Добавьте хотя бы один источник", 422)

    catalog = TemplateCatalog(external_directory=request.app.state.settings.template_dir)
    try:
        template = catalog.get(template_id)
    except TemplateConfigurationError:
        return _setup_error(
            request, session, project, "Шаблон не найден", 422, catalog=catalog
        )

    if kind is JobKind.CHECK:
        document = DocumentRepository(session).get_document(project_id)
        if target_source_id is not None:
            target_source = SourceRepository(session).get(target_source_id)
            if (
                target_source is None
                or target_source.project_id != project_id
                or not is_supported_check_target(target_source)
            ):
                return _setup_error(
                    request,
                    session,
                    project,
                    "Документ для проверки не найден",
                    422,
                    catalog=catalog,
                )
        elif document is None:
            check_targets = [
                source for source in sources if is_supported_check_target(source)
            ]
            if len(check_targets) == 1:
                target_source_id = check_targets[0].id
            else:
                return _setup_error(
                    request,
                    session,
                    project,
                    "Выберите документ для проверки",
                    422,
                    catalog=catalog,
                )
        if target_source_id is None and document.template_id != template.id:
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
        job = JobRepository(session).enqueue_if_project_idle(
            project_id,
            kind,
            template.id,
            target_source_id=target_source_id,
        )
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
    except JobTargetUnavailable as error:
        return _setup_error(request, session, project, str(error), 422, catalog=catalog)
    if _wants_full_page(request):
        return RedirectResponse(
            url=f"/projects/{project_id}/jobs/{job.id}",
            status_code=status.HTTP_303_SEE_OTHER,
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
    except ModelConfigurationError:
        return "Локальные модели не настроены"

    if any(source.kind is SourceKind.CONFLUENCE for source in sources):
        settings = request.app.state.settings
        try:
            ConfluenceClient.from_settings(settings)
        except ExtractionError as error:
            return str(error)
        token = settings.confluence_token
        token_value = token.get_secret_value() if isinstance(token, SecretStr) else token
        password = settings.confluence_pass
        password_value = password.get_secret_value() if isinstance(password, SecretStr) else password
        has_api_base = settings.confluence_api_base or settings.confluence_base_url
        has_basic_auth = settings.confluence_user and password_value
        if not has_api_base or not ((token_value and token_value.strip()) or has_basic_auth):
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
    template_catalog = catalog or TemplateCatalog(
        external_directory=request.app.state.settings.template_dir
    )
    documents = DocumentRepository(session)
    if _wants_full_page(request):
        sources = SourceRepository(session).list_for_project(project.id)
        stored_document = documents.get_document_with_revision(project.id)
        document = stored_document[0] if stored_document is not None else None
        revision = stored_document[1] if stored_document is not None else None
        return templates.TemplateResponse(
            request=request,
            name="projects/detail.html",
            context={
                "project": project,
                "project_id": project.id,
                "sources": sources,
                "check_targets": [
                    source for source in sources if is_supported_check_target(source)
                ],
                "templates": template_catalog.list(),
                "generation_error": message,
                "setup_fragment": False,
                "document": document,
                "revision": revision,
                "workspace_html": documents.get_workspace_html(project.id),
                "has_document": document is not None,
                "has_report": documents.get_report(project.id) is not None,
                "source_error": None,
            },
            status_code=status_code,
        )
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
            "check_targets": [
                source
                for source in SourceRepository(session).list_for_project(project.id)
                if is_supported_check_target(source)
            ],
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
        name=(
            "generation/job.html"
            if _wants_full_page(request)
            else "generation/status.html"
        ),
        context={
            "job": job,
            "is_active": job.status in _ACTIVE_STATUSES,
            "notice": notice,
            "safe_error": _safe_job_error(job),
        },
        status_code=status_code,
    )


def _safe_job_error(job: Job) -> str | None:
    if job.status is not JobStatus.FAILED:
        return None
    if job.error_message and job.status_message == job.error_message:
        return job.error_message
    return _FAILED_MESSAGE


def _job_response(request: Request, session: Session, job: Job) -> Response:
    if job.status is JobStatus.SUCCEEDED:
        documents = DocumentRepository(session)
        if job.kind is JobKind.ASSEMBLE:
            document = (
                documents.get_document_at_revision(
                    job.project_id, job.result_document_revision
                )
                if job.result_document_revision is not None
                else None
            )
            if document is not None:
                if _wants_full_page(request):
                    return _document_response(
                        request,
                        job.project_id,
                        document,
                        standalone=True,
                        warnings=job.warning_messages,
                    )
                return _assemble_complete_response(
                    request,
                    session,
                    job.project_id,
                    document,
                    job.result_document_revision or 1,
                    warnings=job.warning_messages,
                )
        else:
            report = (
                documents.get_report_at_revision(
                    job.project_id,
                    job.result_report_revision,
                    report_generation=job.result_report_generation,
                )
                if job.result_report_revision is not None
                and job.result_report_generation is not None
                else None
            )
            if report is not None:
                if _wants_full_page(request):
                    return _report_response(
                        request,
                        job.project_id,
                        report,
                        standalone=True,
                        warnings=job.warning_messages,
                    )
                return _check_complete_response(
                    request,
                    job.project_id,
                    report,
                    warnings=job.warning_messages,
                )
        return _status_response(
            request,
            job,
            notice="Результат задания заменён более новым",
        )
    return _status_response(request, job)


def _wants_full_page(request: Request) -> bool:
    return (
        request.headers.get("HX-Request") != "true"
        and "text/html" in request.headers.get("Accept", "")
    )


def _document_response(
    request: Request,
    project_id: str,
    document: WorkingDocument,
    *,
    standalone: bool,
    warnings: tuple[str, ...] = (),
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="generation/result.html",
        context={
            "project_id": project_id,
            "document": document,
            "standalone": standalone,
            "warnings": warnings,
        },
    )


def _editor_response(
    request: Request,
    project_id: str,
    document: WorkingDocument,
    revision: int,
    *,
    warnings: tuple[str, ...] = (),
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="editor/surface.html",
        context={
            "project_id": project_id,
            "document": document,
            "revision": revision,
            "warnings": warnings,
        },
    )


def _assemble_complete_response(
    request: Request,
    session: Session,
    project_id: str,
    document: WorkingDocument,
    revision: int,
    *,
    warnings: tuple[str, ...] = (),
) -> Response:
    project = _project_or_404(session, project_id)
    sources = SourceRepository(session).list_for_project(project_id)
    documents = DocumentRepository(session)
    template_catalog = TemplateCatalog(
        external_directory=request.app.state.settings.template_dir
    )
    return templates.TemplateResponse(
        request=request,
        name="generation/assemble_complete.html",
        context={
            "project": project,
            "project_id": project_id,
            "sources": sources,
            "check_targets": [
                source for source in sources if is_supported_check_target(source)
            ],
            "templates": template_catalog.list(),
            "document": document,
            "revision": revision,
            "has_document": True,
            "has_report": documents.get_report(project_id) is not None,
            "source_error": None,
            "generation_error": None,
            "warnings": warnings,
        },
    )


def _report_response(
    request: Request,
    project_id: str,
    report: CheckReport,
    *,
    standalone: bool,
    warnings: tuple[str, ...] = (),
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
            "rule_instructions": _rule_instructions(request, report.template_id),
            "standalone": standalone,
            "warnings": warnings,
        },
    )


def _check_complete_response(
    request: Request,
    project_id: str,
    report: CheckReport,
    *,
    warnings: tuple[str, ...] = (),
) -> Response:
    confirmed = [finding for finding in report.findings if finding.confidence >= 0.7]
    low_confidence = [finding for finding in report.findings if finding.confidence < 0.7]
    return templates.TemplateResponse(
        request=request,
        name="generation/check_complete.html",
        context={
            "project_id": project_id,
            "report": report,
            "confirmed": confirmed,
            "low_confidence": low_confidence,
            "rule_instructions": _rule_instructions(request, report.template_id),
            "warnings": warnings,
        },
    )


def _rule_instructions(request: Request, template_id: str) -> dict[str, str]:
    catalog = TemplateCatalog(external_directory=request.app.state.settings.template_dir)
    try:
        template = catalog.get(template_id)
    except TemplateConfigurationError:
        return {}
    return {rule.id: rule.instruction for rule in template.rules}


__all__ = ["router"]
