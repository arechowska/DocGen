from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from docgen.documents.repository import DocumentRepository
from docgen.generation.targets import supported_check_targets
from docgen.jobs.repository import ActiveProjectJobExists
from docgen.sources.service import SourceService
from docgen.sources.storage import LocalStorage
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.web import templates

from .repository import ProjectRepository
from .service import ProjectService

router = APIRouter(prefix="/projects")


def get_session(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_project_repository(session: Annotated[Session, Depends(get_session)]) -> ProjectRepository:
    return ProjectRepository(session)


def get_project_service(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> ProjectService:
    storage = LocalStorage(request.app.state.settings.data_dir)
    return ProjectService(session, storage)


SessionDependency = Annotated[Session, Depends(get_session)]
ProjectRepositoryDependency = Annotated[ProjectRepository, Depends(get_project_repository)]
ProjectServiceDependency = Annotated[ProjectService, Depends(get_project_service)]


def _commit(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def _project_or_404(repository: ProjectRepository, project_id: str):
    project = repository.get(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Проект не найден")
    return project


@router.get("")
def project_list(request: Request, repository: ProjectRepositoryDependency):
    return templates.TemplateResponse(
        request=request,
        name="projects/index.html",
        context={"projects": repository.list()},
    )


@router.post("")
def create_project(
    name: Annotated[str, Form()], session: SessionDependency, repository: ProjectRepositoryDependency
):
    try:
        project = repository.create(name)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error

    _commit(session)
    return RedirectResponse(url=f"/projects/{project.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{project_id}")
def project_detail(
    request: Request,
    project_id: str,
    repository: ProjectRepositoryDependency,
    session: SessionDependency,
):
    del repository
    return project_detail_response(request, project_id, session)


def project_detail_response(
    request: Request,
    project_id: str,
    session: Session,
    *,
    source_error: str | None = None,
    status_code: int = status.HTTP_200_OK,
):
    repository = ProjectRepository(session)
    project = _project_or_404(repository, project_id)
    source_service = SourceService(
        session,
        LocalStorage(request.app.state.settings.data_dir),
        request.app.state.settings.confluence_hosts,
    )
    documents = DocumentRepository(session)
    stored_document = documents.get_document_with_revision(project_id)
    document = stored_document[0] if stored_document is not None else None
    revision = stored_document[1] if stored_document is not None else None
    sources = source_service.list(project_id)
    return templates.TemplateResponse(
        request=request,
        name="projects/detail.html",
        context={
            "project": project,
            "project_id": project_id,
            "sources": sources,
            "check_targets": supported_check_targets(sources),
            "templates": TemplateCatalog().list(),
            "generation_error": None,
            "setup_fragment": False,
            "document": document,
            "revision": revision,
            "has_document": document is not None,
            "has_report": documents.get_report(project_id) is not None,
            "source_error": source_error,
        },
        status_code=status_code,
    )


@router.patch("/{project_id}")
def rename_project(
    request: Request,
    project_id: str,
    name: Annotated[str, Form()],
    session: SessionDependency,
    repository: ProjectRepositoryDependency,
):
    project = _project_or_404(repository, project_id)
    try:
        project = repository.rename(project_id, name)
    except ValueError as error:
        return templates.TemplateResponse(
            request=request,
            name="projects/name_form.html",
            context={"project": project, "name": name, "error": str(error)},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    _commit(session)
    return templates.TemplateResponse(
        request=request,
        name="projects/name_form.html",
        context={"project": project, "name": project.name, "error": None},
    )


@router.post("/{project_id}/rename")
def rename_project_fallback(
    project_id: str,
    name: Annotated[str, Form()],
    session: SessionDependency,
    repository: ProjectRepositoryDependency,
):
    _project_or_404(repository, project_id)
    try:
        repository.rename(project_id, name)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    _commit(session)
    return RedirectResponse(
        url=f"/projects/{project_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.delete("/{project_id}")
def delete_project(request: Request, project_id: str, service: ProjectServiceDependency):
    try:
        service.delete(project_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ActiveProjectJobExists as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=status.HTTP_200_OK, headers={"HX-Redirect": "/projects"})
    return RedirectResponse(url="/projects", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{project_id}/delete")
def delete_project_fallback(
    request: Request,
    project_id: str,
    service: ProjectServiceDependency,
):
    return delete_project(request, project_id, service)
