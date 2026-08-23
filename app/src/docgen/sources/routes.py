from typing import Annotated, BinaryIO

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from docgen.documents.repository import DocumentRepository
from docgen.generation.targets import supported_check_targets
from docgen.jobs.repository import ActiveProjectJobExists
from docgen.projects.repository import ProjectRepository
from docgen.projects.routes import (
    SessionDependency,
    project_detail_response,
    selected_template_id,
)
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.web import templates

from .service import SourceService
from .storage import LocalStorage

router = APIRouter(prefix="/projects/{project_id}/sources")

_UPLOAD_TOO_LARGE = "Файл слишком большой"
_INVALID_UPLOAD = "Некорректная загрузка файла"


class _FileSizeLimitedMultiPartParser(MultiPartParser):
    def __init__(self, *args: object, max_file_bytes: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._max_file_bytes = max_file_bytes
        self._current_file_bytes = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            next_size = self._current_file_bytes + (end - start)
            if next_size > self._max_file_bytes:
                raise MultiPartException(_UPLOAD_TOO_LARGE)
            self._current_file_bytes = next_size
        super().on_part_data(data, start, end)


def get_source_service(request: Request, session: SessionDependency) -> SourceService:
    settings = request.app.state.settings
    return SourceService(
        session,
        LocalStorage(settings.data_dir),
        settings.confluence_hosts,
        max_upload_bytes=settings.max_upload_bytes,
        max_project_storage_bytes=settings.max_project_storage_bytes,
    )


SourceServiceDependency = Annotated[SourceService, Depends(get_source_service)]


def _error_response(
    request: Request,
    project_id: str,
    error: Exception,
    response_status: int,
    session: Session | None = None,
):
    if _wants_full_page(request) and session is not None:
        return project_detail_response(
            request,
            project_id,
            session,
            source_error=str(error),
            status_code=response_status,
        )
    response = templates.TemplateResponse(
        request=request,
        name="sources/error.html",
        context={"error": str(error)},
        status_code=response_status,
    )
    if request.headers.get("HX-Request") == "true":
        response.headers["HX-Retarget"] = "#sources-error"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


def _source_list_response(
    request: Request,
    project_id: str,
    service: SourceService,
    session: Session | None = None,
):
    if _wants_full_page(request):
        return RedirectResponse(
            url=f"/projects/{project_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if session is not None:
        project = ProjectRepository(session).get(project_id)
        if project is None:
            raise LookupError("Проект не найден")
        documents = DocumentRepository(session)
        document = documents.get_document(project_id)
        latest_report = documents.get_latest_report_record(project_id)
        sources = service.list(project_id)
        return templates.TemplateResponse(
            request=request,
            name="sources/project_update.html",
            context={
                "project": project,
                "project_id": project_id,
                "sources": sources,
                "check_targets": supported_check_targets(sources),
                "templates": TemplateCatalog().list(),
                "document": document,
                "selected_template_id": selected_template_id(document, latest_report),
                "has_document": document is not None,
                "generation_error": None,
                "source_error": None,
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="sources/list.html",
        context={"project_id": project_id, "sources": service.list(project_id)},
    )


def _wants_full_page(request: Request) -> bool:
    return (
        request.headers.get("HX-Request") != "true"
        and "text/html" in request.headers.get("Accept", "")
    )


def _add_file_response(
    request: Request,
    project_id: str,
    filename: str,
    media_type: str,
    stream: BinaryIO,
    service: SourceService,
    session: Session | None = None,
):
    service.add_file(project_id, filename, media_type, stream)
    return _source_list_response(request, project_id, service, session)


async def _parse_single_upload(request: Request, max_file_bytes: int) -> UploadFile:
    parser = _FileSizeLimitedMultiPartParser(
        request.headers,
        request.stream(),
        max_files=1,
        max_fields=0,
        max_file_bytes=max_file_bytes,
    )
    try:
        form = await parser.parse()
    except MultiPartException as error:
        if str(error) == _UPLOAD_TOO_LARGE:
            raise ValueError(_UPLOAD_TOO_LARGE) from None
        raise ValueError(_INVALID_UPLOAD) from None

    uploads = form.getlist("file")
    if len(uploads) != 1 or not isinstance(uploads[0], UploadFile):
        await form.close()
        raise ValueError(_INVALID_UPLOAD)
    return uploads[0]


async def _process_upload(
    request: Request,
    project_id: str,
    file: UploadFile,
    service: SourceService,
    session: Session | None = None,
):
    try:
        return await run_in_threadpool(
            _add_file_response,
            request,
            project_id,
            file.filename or "",
            file.content_type or "application/octet-stream",
            file.file,
            service,
            session,
        )
    except ValueError as error:
        return _error_response(
            request,
            project_id,
            error,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            session,
        )
    except LookupError as error:
        return _error_response(
            request, project_id, error, status.HTTP_404_NOT_FOUND, session
        )
    finally:
        await file.close()


@router.post("/files")
async def add_file(
    request: Request,
    project_id: str,
    service: SourceServiceDependency,
    session: SessionDependency,
):
    try:
        file = await _parse_single_upload(
            request,
            request.app.state.settings.max_upload_bytes,
        )
    except ValueError as error:
        return _error_response(
            request,
            project_id,
            error,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            session,
        )
    return await _process_upload(request, project_id, file, service, session)


@router.post("/confluence")
def add_confluence(
    request: Request,
    project_id: str,
    url: Annotated[str, Form()],
    service: SourceServiceDependency,
    session: SessionDependency,
):
    try:
        service.add_confluence(project_id, url)
        return _source_list_response(request, project_id, service, session)
    except ValueError as error:
        return _error_response(
            request,
            project_id,
            error,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            session,
        )
    except LookupError as error:
        return _error_response(
            request, project_id, error, status.HTTP_404_NOT_FOUND, session
        )


@router.delete("/{source_id}")
def delete_source(
    request: Request,
    project_id: str,
    source_id: str,
    service: SourceServiceDependency,
    session: SessionDependency,
):
    try:
        service.delete(project_id, source_id)
        return _source_list_response(request, project_id, service, session)
    except LookupError as error:
        return _error_response(
            request, project_id, error, status.HTTP_404_NOT_FOUND, session
        )
    except ActiveProjectJobExists as error:
        return _error_response(
            request, project_id, error, status.HTTP_409_CONFLICT, session
        )


@router.post("/{source_id}/delete")
def delete_source_fallback(
    request: Request,
    project_id: str,
    source_id: str,
    service: SourceServiceDependency,
    session: SessionDependency,
):
    return delete_source(request, project_id, source_id, service, session)
