from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.templating import Jinja2Templates

from docgen.projects.routes import SessionDependency

from .service import SourceService
from .storage import LocalStorage

router = APIRouter(prefix="/projects/{project_id}/sources")
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


def get_source_service(request: Request, session: SessionDependency) -> SourceService:
    return SourceService(
        session,
        LocalStorage(request.app.state.settings.data_dir),
        request.app.state.settings.confluence_hosts,
    )


SourceServiceDependency = Annotated[SourceService, Depends(get_source_service)]


def _error_response(request: Request, error: Exception, response_status: int):
    return templates.TemplateResponse(
        request=request,
        name="sources/error.html",
        context={"error": str(error)},
        status_code=response_status,
    )


def _source_list_response(request: Request, project_id: str, service: SourceService):
    return templates.TemplateResponse(
        request=request,
        name="sources/list.html",
        context={"project_id": project_id, "sources": service.list(project_id)},
    )


@router.post("/files")
async def add_file(
    request: Request,
    project_id: str,
    file: Annotated[UploadFile, File()],
    service: SourceServiceDependency,
):
    try:
        service.add_file(
            project_id,
            file.filename or "",
            file.content_type or "application/octet-stream",
            file.file,
        )
        return _source_list_response(request, project_id, service)
    except ValueError as error:
        return _error_response(request, error, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except LookupError as error:
        return _error_response(request, error, status.HTTP_404_NOT_FOUND)
    finally:
        await file.close()


@router.post("/confluence")
def add_confluence(
    request: Request,
    project_id: str,
    url: Annotated[str, Form()],
    service: SourceServiceDependency,
):
    try:
        service.add_confluence(project_id, url)
        return _source_list_response(request, project_id, service)
    except ValueError as error:
        return _error_response(request, error, status.HTTP_422_UNPROCESSABLE_CONTENT)
    except LookupError as error:
        return _error_response(request, error, status.HTTP_404_NOT_FOUND)


@router.delete("/{source_id}")
def delete_source(
    request: Request,
    project_id: str,
    source_id: str,
    service: SourceServiceDependency,
):
    try:
        service.delete(project_id, source_id)
        return _source_list_response(request, project_id, service)
    except LookupError as error:
        return _error_response(request, error, status.HTTP_404_NOT_FOUND)
