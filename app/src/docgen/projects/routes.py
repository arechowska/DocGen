from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from docgen.sources.service import SourceService
from docgen.sources.storage import LocalStorage
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
    project = _project_or_404(repository, project_id)
    source_service = SourceService(
        session,
        LocalStorage(request.app.state.settings.data_dir),
        request.app.state.settings.confluence_hosts,
    )
    return templates.TemplateResponse(
        request=request,
        name="projects/detail.html",
        context={
            "project": project,
            "project_id": project_id,
            "sources": source_service.list(project_id),
        },
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


@router.delete("/{project_id}")
def delete_project(request: Request, project_id: str, service: ProjectServiceDependency):
    try:
        service.delete(project_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=status.HTTP_200_OK, headers={"HX-Redirect": "/projects"})
    return RedirectResponse(url="/projects", status_code=status.HTTP_303_SEE_OTHER)
