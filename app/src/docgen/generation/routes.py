from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import SecretStr
from sqlalchemy.orm import Session

from docgen.ai.client import ModelConfigurationError, build_text_model
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import CheckReport, WorkingDocument
from docgen.export.html import local_storage_image_loader
from docgen.export.protocol import ExportError
from docgen.export.service import default_exporters
from docgen.export.storage import ExportStorage
from docgen.extraction.confluence import ConfluenceClient
from docgen.extraction.registry import ExtractionError, ExtractorRegistry
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
from docgen.models import Project, Source, SourceKind
from docgen.projects.repository import ProjectRepository
from docgen.projects.routes import get_session, selected_template_id
from docgen.sources.repository import SourceRepository
from docgen.sources.storage import LocalStorage
from docgen.templates_catalog.loader import (
    NO_TEMPLATE_ID,
    TemplateCatalog,
    TemplateConfigurationError,
)
from docgen.web import templates
from docgen.workflows.check import structure_gap_operations
from docgen.workflows.conversion import conversion_document
from docgen.workflows.normalize import NormalizationWorkflow, PageLimitExceeded

from .targets import is_supported_check_target

router = APIRouter(prefix="/projects")

SessionDependency = Annotated[Session, Depends(get_session)]

_ACTIVE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})
_FAILED_MESSAGE = "Не удалось обработать источники"
_PROJECT_ACTIVE_MESSAGE = "Проект уже обрабатывается"
_CONVERSION_MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".html": "text/html; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".pdf": "application/pdf",
}


@router.post("/{project_id}/jobs/assemble", status_code=status.HTTP_202_ACCEPTED)
def start_assemble(
    request: Request,
    project_id: str,
    template_id: Annotated[str, Form()],
    session: SessionDependency,
) -> Response:
    if template_id == NO_TEMPLATE_ID:
        project = _project_or_404(session, project_id)
        return _setup_error(
            request,
            session,
            project,
            "Без шаблона используйте лёгкую конвертацию",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return _start_job(request, session, project_id, JobKind.ASSEMBLE, template_id)


@router.post("/{project_id}/convert")
def convert_source(
    request: Request,
    project_id: str,
    output_format: Annotated[OutputFormat, Form()],
    formatting_template_id: Annotated[str, Form()],
    session: SessionDependency,
) -> Response:
    """Convert one source to a file without creating an editor document."""
    project = _project_or_404(session, project_id)
    sources = SourceRepository(session).list_for_project(project_id)
    if len(sources) != 1:
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(
                request=request,
                name="projects/conversion_result_panel.html",
                context={
                    "project_id": project_id,
                    "document": None,
                    "has_document": False,
                    "conversion_error": (
                        "Без шаблона можно собрать только один источник. "
                        f"Сейчас в проекте: {len(sources)}."
                    ),
                },
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        if "text/html" in request.headers.get("Accept", ""):
            return templates.TemplateResponse(
                request=request,
                name="generation/conversion_error.html",
                context={
                    "project_id": project_id,
                    "source_count": len(sources),
                },
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Для конвертации без шаблона нужен ровно один источник",
        )
    dependency_error = _dependency_error(request, sources, require_text_model=False)
    if dependency_error is not None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=dependency_error,
        )

    settings = request.app.state.settings
    formatting_directory = settings.formatting_template_dir or default_templates_dir()
    try:
        formatting_template = FormattingCatalog(formatting_directory).get(
            output_format, formatting_template_id
        )
        source_storage = LocalStorage(settings.data_dir)
        normalized = NormalizationWorkflow(
            SourceRepository(session),
            source_storage,
            ExtractorRegistry.default(settings),
            ConfluenceClient.from_settings(settings),
        ).run(project_id)
        source = sources[0]
        blocks = [
            block
            for block in normalized.blocks
            if any(item.source_id == source.id for item in block.provenance)
        ]
        if not blocks:
            raise ExtractionError("В источнике нет извлекаемого содержимого")
        document = conversion_document(blocks, source.display_name or project.name)
        exporter = default_exporters(
            image_loader=local_storage_image_loader(source_storage),
            templates_dir=formatting_directory,
        )[output_format]
        rendered = exporter.render(document, formatting_template)
        export_storage = ExportStorage(settings.data_dir)
        if output_format is OutputFormat.HTML:
            stored = export_storage.save_conversion(
                project_id,
                output_format,
                formatting_template_id,
                rendered,
            )
        else:
            stored = export_storage.save(
                project_id,
                output_format,
                formatting_template_id,
                rendered,
            )
    except (
        ExportError,
        ExtractionError,
        FormattingTemplateError,
        PageLimitExceeded,
        ValueError,
    ) as error:
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(
                request=request,
                name="projects/conversion_result_panel.html",
                context={
                    "project_id": project_id,
                    "document": None,
                    "has_document": False,
                    "conversion_error": str(error),
                },
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request=request,
            name="projects/conversion_result_panel.html",
            context={
                "project": project,
                "project_id": project_id,
                "document": None,
                "has_document": False,
                "sources": sources,
                "saved_conversion_export": stored,
                "conversion_format": output_format,
            },
        )

    disposition = "inline" if output_format is OutputFormat.HTML else "attachment"
    encoded_filename = quote(stored.filename)
    return Response(
        content=rendered.content,
        media_type=rendered.media_type,
        headers={
            "Content-Disposition": (
                f"{disposition}; filename*=UTF-8''{encoded_filename}"
            )
        },
    )


@router.get("/{project_id}/conversions/{filename}/open")
def open_saved_html_conversion(
    request: Request,
    project_id: str,
    filename: str,
    session: SessionDependency,
) -> Response:
    _project_or_404(session, project_id)
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or not filename.lower().endswith(".html")
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не найден")

    storage = ExportStorage(request.app.state.settings.data_dir)
    relative_path = f"projects/{project_id}/exports/{filename}"
    try:
        path = storage.resolve(relative_path)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Файл не найден",
        ) from error
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не найден")
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        filename=filename,
        content_disposition_type="inline",
    )


@router.get("/{project_id}/conversions/{filename}/download")
def download_saved_conversion(
    request: Request,
    project_id: str,
    filename: str,
    session: SessionDependency,
) -> Response:
    _project_or_404(session, project_id)
    suffix = Path(filename).suffix.lower()
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or suffix not in _CONVERSION_MEDIA_TYPES
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не найден")
    storage = ExportStorage(request.app.state.settings.data_dir)
    try:
        path = storage.resolve(f"projects/{project_id}/exports/{filename}")
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Файл не найден",
        ) from error
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не найден")
    return FileResponse(
        path,
        media_type=_CONVERSION_MEDIA_TYPES[suffix],
        filename=filename,
    )


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
    documents = DocumentRepository(session)
    current = documents.get_document_with_revision(project_id)
    record = documents.get_latest_report_record(project_id)
    if record is None:
        return templates.TemplateResponse(
            request=request,
            name="generation/report_missing.html",
            context={"project_id": project_id},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return _report_response(
        request,
        project_id,
        record.report,
        standalone=True,
        stale=current is None or current[1] != record.document_revision,
        revision=(
            current[1]
            if current is not None and current[1] == record.document_revision
            else None
        ),
        document=(
            current[0]
            if current is not None and current[1] == record.document_revision
            else None
        ),
        report_target_source_id=record.target_source_id,
    )


@router.get("/{project_id}/report/card")
def report_card(request: Request, project_id: str, session: SessionDependency) -> Response:
    """Re-inject the last check report into the chat as the same actionable
    card shown right after a check completes -- chat history is not
    persisted, so this is how the report is reached again after navigating
    away and back."""
    _project_or_404(session, project_id)
    documents = DocumentRepository(session)
    current = documents.get_document_with_revision(project_id)
    record = documents.get_latest_report_record(project_id)
    if record is None:
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={
                "message": (
                    "Отчёт недоступен: документ изменился после последней проверки"
                ),
                "action": "Запусти проверку по шаблону ещё раз.",
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    report = record.report
    confirmed = [finding for finding in report.findings if finding.confidence >= 0.7]
    low_confidence = [finding for finding in report.findings if finding.confidence < 0.7]
    return templates.TemplateResponse(
        request=request,
        name="chat/check_result.html",
        context={
            "project_id": project_id,
            "confirmed": confirmed,
            "low_confidence": low_confidence,
            "rule_instructions": _rule_instructions(request, report.template_id),
            "report": report,
            "stale": current is None or current[1] != record.document_revision,
            "report_target_source_id": record.target_source_id,
        },
    )


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
    without_template = template_id == NO_TEMPLATE_ID
    if without_template and kind is JobKind.CHECK:
        return _setup_error(
            request,
            session,
            project,
            "Для проверки выберите смысловой шаблон",
            422,
            catalog=catalog,
        )
    if without_template and len(sources) != 1:
        return _setup_error(
            request,
            session,
            project,
            "Для сборки без шаблона выберите ровно один источник",
            422,
            catalog=catalog,
        )

    template = None
    if not without_template:
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
    dependency_error = _dependency_error(
        request, sources, require_text_model=not without_template
    )
    if dependency_error is not None:
        return _setup_error(
            request, session, project, dependency_error, 503, catalog=catalog
        )

    try:
        job = JobRepository(session).enqueue_if_project_idle(
            project_id,
            kind,
            NO_TEMPLATE_ID if without_template else template.id,
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


def _dependency_error(
    request: Request, sources: list[Source], *, require_text_model: bool = True
) -> str | None:
    if require_text_model:
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
        latest_report = documents.get_latest_report_record(project.id)
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
                "selected_template_id": selected_template_id(document, latest_report),
                "revision": revision,
                "workspace_html": documents.get_workspace_html(project.id),
                "has_document": document is not None,
                "has_report": latest_report is not None,
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
            "has_report": documents.get_latest_report_record(project.id) is not None,
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
        on_standalone_job_page = _polls_from_standalone_job_page(request, job.id)
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
                if on_standalone_job_page:
                    return _htmx_redirect(f"/projects/{job.project_id}/document")
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
                    return RedirectResponse(
                        url=f"/projects/{job.project_id}#docgen2Editor",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
                if on_standalone_job_page:
                    return _htmx_redirect(f"/projects/{job.project_id}/report")
                document = documents.get_document_at_revision(
                    job.project_id,
                    job.result_report_revision,
                )
                if document is None:
                    return _status_response(
                        request,
                        job,
                        notice="Результат задания заменён более новым",
                    )
                return _check_complete_response(
                    request,
                    session,
                    job.project_id,
                    document,
                    job.result_report_revision,
                    report,
                    warnings=job.warning_messages,
                    report_target_source_id=job.target_source_id,
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


def _polls_from_standalone_job_page(request: Request, job_id: str) -> bool:
    """Detect the bare `/jobs/{id}` page polling for its own completion.

    That page never embeds the project's source/work/action panels, so the
    OOB-swap fragments `_assemble_complete_response`/`_check_complete_response`
    build for the in-project polling loop have nowhere to land there -- the
    visible `#generation-status` swap would end up empty. htmx sends the
    browser's current URL on every request, letting us tell the two polling
    contexts apart and redirect this one to a page that renders standalone.
    """
    current_url = request.headers.get("HX-Current-URL", "")
    return f"/jobs/{job_id}" in current_url


def _htmx_redirect(url: str) -> Response:
    response = Response(status_code=status.HTTP_200_OK)
    response.headers["HX-Redirect"] = url
    return response


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
    context = _workspace_completion_context(
        request,
        session,
        project_id,
        document,
        revision,
    )
    context["warnings"] = warnings
    response = templates.TemplateResponse(
        request=request,
        name="generation/assemble_complete.html",
        context=context,
    )
    response.headers["HX-Trigger"] = json.dumps(
        {"docgen:document-ready": {}}, ensure_ascii=False
    )
    return response


def _report_response(
    request: Request,
    project_id: str,
    report: CheckReport,
    *,
    standalone: bool,
    warnings: tuple[str, ...] = (),
    stale: bool = False,
    revision: int | None = None,
    document: WorkingDocument | None = None,
    report_target_source_id: str | None = None,
) -> Response:
    confirmed = [finding for finding in report.findings if finding.confidence >= 0.7]
    low_confidence = [finding for finding in report.findings if finding.confidence < 0.7]
    actionable_rule_ids = {
        finding.rule_id
        for finding in report.findings
        if finding.suggestion and finding.node_id and finding.rule_id
    }
    if document is not None:
        catalog = TemplateCatalog(
            external_directory=request.app.state.settings.template_dir
        )
        try:
            semantic_template = catalog.get(report.template_id)
        except TemplateConfigurationError:
            semantic_template = None
        if (
            semantic_template is not None
            and semantic_template.structure_check is not None
            and structure_gap_operations(document, semantic_template)
        ):
            actionable_rule_ids.add(semantic_template.structure_check.rule_id)
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
            "stale": stale,
            "revision": revision,
            "actionable_rule_ids": actionable_rule_ids,
            "report_target_source_id": report_target_source_id,
        },
    )


def _check_complete_response(
    request: Request,
    session: Session,
    project_id: str,
    document: WorkingDocument,
    revision: int,
    report: CheckReport,
    *,
    warnings: tuple[str, ...] = (),
    report_target_source_id: str | None = None,
) -> Response:
    confirmed = [finding for finding in report.findings if finding.confidence >= 0.7]
    low_confidence = [finding for finding in report.findings if finding.confidence < 0.7]
    context = _workspace_completion_context(
        request,
        session,
        project_id,
        document,
        revision,
    )
    context.update(
        {
            "report": report,
            "confirmed": confirmed,
            "low_confidence": low_confidence,
            "rule_instructions": _rule_instructions(request, report.template_id),
            "warnings": warnings,
            "stale": False,
            "report_target_source_id": report_target_source_id,
        }
    )
    return templates.TemplateResponse(
        request=request,
        name="generation/check_complete.html",
        context=context,
    )


def _workspace_completion_context(
    request: Request,
    session: Session,
    project_id: str,
    document: WorkingDocument,
    revision: int,
) -> dict[str, object]:
    project = _project_or_404(session, project_id)
    sources = SourceRepository(session).list_for_project(project_id)
    documents = DocumentRepository(session)
    latest_report = documents.get_latest_report_record(project_id)
    template_catalog = TemplateCatalog(
        external_directory=request.app.state.settings.template_dir
    )
    return {
        "project": project,
        "project_id": project_id,
        "sources": sources,
        "check_targets": [
            source for source in sources if is_supported_check_target(source)
        ],
        "templates": template_catalog.list(),
        "document": document,
        "selected_template_id": selected_template_id(document, latest_report),
        "revision": revision,
        "workspace_html": documents.get_workspace_html(project_id),
        "has_document": True,
        "has_report": latest_report is not None,
        "source_error": None,
        "generation_error": None,
    }


def _rule_instructions(request: Request, template_id: str) -> dict[str, str]:
    catalog = TemplateCatalog(external_directory=request.app.state.settings.template_dir)
    try:
        template = catalog.get(template_id)
    except TemplateConfigurationError:
        return {}
    return {rule.id: rule.instruction for rule in template.rules}


__all__ = ["router"]
