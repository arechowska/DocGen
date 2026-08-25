"""HTTP routes for choosing a format/template, running an export job, and
downloading the resulting file.

The actual rendering work (`ExportService.export`) runs asynchronously in
the job worker via `docgen.workflows.export.ExportWorkflow` -- these routes
only validate the request, enqueue an `EXPORT` job, and later let the
project's owner poll its status and download the finished file. Per the
Global Constraints, export never mutates the project or working document,
so none of these routes touch `DocumentRepository` beyond reading the
current revision to detect a stale submission.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from docgen.documents.repository import DocumentRepository
from docgen.export.storage import ExportStorage
from docgen.formatting.catalog import (
    FormattingCatalog,
    FormattingTemplateError,
    default_templates_dir,
)
from docgen.formatting.schemas import OutputFormat
from docgen.jobs.models import Job, JobKind, JobStatus
from docgen.jobs.repository import (
    ActiveProjectJobExists,
    JobRepository,
    JobTargetUnavailable,
)
from docgen.models import Project
from docgen.projects.repository import ProjectRepository
from docgen.projects.routes import get_session
from docgen.templates_catalog.loader import NO_TEMPLATE_ID
from docgen.web import templates

router = APIRouter(prefix="/projects")

SessionDependency = Annotated[Session, Depends(get_session)]

_ACTIVE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})
_FAILED_MESSAGE = "Не удалось выполнить экспорт"
_PROJECT_ACTIVE_MESSAGE = "Проект уже обрабатывается"
_DOCUMENT_MISSING_MESSAGE = "Документ не найден"
_DOCUMENT_REVISION_MESSAGE = (
    "Нажмите «Сохранить в проект» — документ станет доступен для экспорта"
)
_DOCUMENT_STALE_MESSAGE = "Документ изменён; обновите страницу и повторите экспорт"
_TEMPLATE_MISSING_MESSAGE = "Шаблон не найден для выбранного формата"
_EXPORT_PENDING_MESSAGE = "Экспорт ещё выполняется"
_EXPORT_NOT_SUCCEEDED_MESSAGE = "Экспорт завершился без файла"
_EXPORT_FILE_MISSING_MESSAGE = "Файл экспорта недоступен"

# Fixed by format rather than trusted from the stored job row, so a download
# always advertises the media type that actually matches its bytes.
_MEDIA_TYPES: dict[OutputFormat, str] = {
    OutputFormat.DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    OutputFormat.PDF: "application/pdf",
    OutputFormat.HTML: "text/html; charset=utf-8",
    OutputFormat.MARKDOWN: "text/markdown; charset=utf-8",
}


@router.get("/{project_id}/export/templates")
def export_templates(
    request: Request,
    project_id: str,
    session: SessionDependency,
    format: Annotated[OutputFormat, Query()],
    semantic_template_id: Annotated[str | None, Query()] = None,
) -> Response:
    _project_or_404(session, project_id)
    catalog = _catalog(request)
    return templates.TemplateResponse(
        request=request,
        name="export/template_options.html",
        context={
            "templates": catalog.list(format),
            "project_id": project_id,
            "manual_html_build": (
                format is OutputFormat.HTML and semantic_template_id == NO_TEMPLATE_ID
            ),
        },
    )


@router.post("/{project_id}/export", status_code=status.HTTP_202_ACCEPTED)
def start_export(
    request: Request,
    project_id: str,
    session: SessionDependency,
    format: Annotated[OutputFormat, Form()],
    template_id: Annotated[str, Form()],
    revision: Annotated[str | None, Form()] = None,
) -> Response:
    _project_or_404(session, project_id)

    stored = DocumentRepository(session).get_document_with_revision(project_id)
    if stored is None:
        return _error_response(
            request, _DOCUMENT_MISSING_MESSAGE, status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    _, current_revision = stored
    try:
        requested_revision = int(revision) if revision is not None else None
    except ValueError:
        requested_revision = None
    if requested_revision is None:
        return _error_response(
            request, _DOCUMENT_REVISION_MESSAGE, status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    if requested_revision != current_revision:
        return _error_response(request, _DOCUMENT_STALE_MESSAGE, status.HTTP_409_CONFLICT)

    try:
        _catalog(request).get(format, template_id)
    except FormattingTemplateError:
        return _error_response(
            request, _TEMPLATE_MISSING_MESSAGE, status.HTTP_422_UNPROCESSABLE_CONTENT
        )

    try:
        job = JobRepository(session).enqueue_if_project_idle(
            project_id,
            JobKind.EXPORT,
            template_id,
            export_format=format,
            requested_document_revision=requested_revision,
        )
    except ActiveProjectJobExists:
        return _error_response(request, _PROJECT_ACTIVE_MESSAGE, status.HTTP_409_CONFLICT)
    except JobTargetUnavailable as error:
        return _error_response(
            request, str(error), status.HTTP_422_UNPROCESSABLE_CONTENT
        )

    return _status_response(session, request, job, status_code=status.HTTP_202_ACCEPTED)


@router.get("/{project_id}/exports/{job_id}/status")
def export_status(
    request: Request,
    project_id: str,
    job_id: str,
    session: SessionDependency,
) -> Response:
    _project_or_404(session, project_id)
    job = _owned_export_job_or_404(session, project_id, job_id)
    return _status_response(session, request, job)


@router.get("/{project_id}/exports/{job_id}/download")
def download_export(
    project_id: str,
    job_id: str,
    session: SessionDependency,
    request: Request,
) -> Response:
    _project_or_404(session, project_id)
    job = _owned_export_job_or_404(session, project_id, job_id)

    if job.status in _ACTIVE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=_EXPORT_PENDING_MESSAGE
        )
    if job.status is not JobStatus.SUCCEEDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=_EXPORT_NOT_SUCCEEDED_MESSAGE
        )
    if (
        job.export_relative_path is None
        or job.export_filename is None
        or job.export_format is None
    ):
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=_EXPORT_FILE_MISSING_MESSAGE
        )

    storage = ExportStorage(request.app.state.settings.data_dir)
    try:
        path = storage.resolve(job.export_relative_path)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=_EXPORT_FILE_MISSING_MESSAGE
        ) from error
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=_EXPORT_FILE_MISSING_MESSAGE
        )

    return FileResponse(
        path,
        media_type=_MEDIA_TYPES[job.export_format],
        filename=job.export_filename,
    )


@router.get("/{project_id}/exports/{job_id}/open")
def open_html_export(
    project_id: str,
    job_id: str,
    session: SessionDependency,
    request: Request,
) -> Response:
    """Open the exact completed HTML export in a browser tab."""
    job = _owned_export_job_or_404(session, project_id, job_id)
    if job.status is not JobStatus.SUCCEEDED or job.export_format is not OutputFormat.HTML:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не найден")
    if job.export_relative_path is None or job.export_filename is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=_EXPORT_FILE_MISSING_MESSAGE
        )

    storage = ExportStorage(request.app.state.settings.data_dir)
    try:
        path = storage.resolve(job.export_relative_path)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=_EXPORT_FILE_MISSING_MESSAGE
        ) from error
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=_EXPORT_FILE_MISSING_MESSAGE
        )
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES[OutputFormat.HTML],
        filename=job.export_filename,
        content_disposition_type="inline",
    )


def _project_or_404(session: Session, project_id: str) -> Project:
    project = ProjectRepository(session).get(project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Проект не найден",
        )
    return project


def _owned_export_job_or_404(session: Session, project_id: str, job_id: str) -> Job:
    job = JobRepository(session).get(job_id)
    if job is None or job.project_id != project_id or job.kind is not JobKind.EXPORT:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Задание не найдено",
        )
    return job


def _catalog(request: Request) -> FormattingCatalog:
    settings = request.app.state.settings
    directory = settings.formatting_template_dir or default_templates_dir()
    return FormattingCatalog(directory)


def _error_response(request: Request, message: str, status_code: int) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="export/error.html",
        context={"message": message},
        status_code=status_code,
    )


def _is_no_template_html_export(session: Session, job: Job) -> bool:
    if (
        job.export_format is not OutputFormat.HTML
        or job.requested_document_revision is None
    ):
        return False
    document = DocumentRepository(session).get_document_at_revision(
        job.project_id,
        job.requested_document_revision,
    )
    return document is not None and document.template_id == NO_TEMPLATE_ID


def _status_response(
    session: Session,
    request: Request,
    job: Job,
    *,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="export/download_button.html",
        context={
            "job": job,
            "is_active": job.status in _ACTIVE_STATUSES,
            "safe_error": _safe_export_error(job),
            "show_html_download": _is_no_template_html_export(session, job),
        },
        status_code=status_code,
    )


def _safe_export_error(job: Job) -> str | None:
    if job.status is not JobStatus.FAILED:
        return None
    if job.error_message and job.status_message == job.error_message:
        return job.error_message
    return _FAILED_MESSAGE


__all__ = ["router"]
