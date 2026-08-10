from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from docgen.documents.edit_service import DocumentEditService, EditConflict
from docgen.documents.operations import EditValidationError, UpdateText, find_node
from docgen.documents.repository import DocumentRepository
from docgen.projects.repository import ProjectRepository
from docgen.projects.routes import get_session
from docgen.web import templates

router = APIRouter(prefix="/projects")

SessionDependency = Annotated[Session, Depends(get_session)]


@router.get("/{project_id}/editor")
def editor_view(request: Request, project_id: str, session: SessionDependency) -> Response:
    _project_or_404(session, project_id)
    stored = DocumentRepository(session).get_document_with_revision(project_id)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Документ не найден",
        )
    document, revision = stored
    return templates.TemplateResponse(
        request=request,
        name="editor/document.html",
        context={
            "project_id": project_id,
            "document": document,
            "revision": revision,
        },
    )


@router.patch("/{project_id}/editor/nodes/{node_id}/text")
def update_node_text(
    request: Request,
    project_id: str,
    node_id: str,
    text: Annotated[str, Form()],
    revision: Annotated[int, Form()],
    session: SessionDependency,
) -> Response:
    _project_or_404(session, project_id)
    repository = DocumentRepository(session)
    service = DocumentEditService(repository)
    try:
        result = service.apply(
            project_id,
            revision,
            [UpdateText(node_id=node_id, text=text)],
        )
    except EditConflict:
        session.rollback()
        return templates.TemplateResponse(
            request=request,
            name="editor/conflict.html",
            context={"project_id": project_id},
            status_code=status.HTTP_409_CONFLICT,
        )
    except EditValidationError as error:
        session.rollback()
        return templates.TemplateResponse(
            request=request,
            name="editor/error.html",
            context={"message": str(error), "value": text},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    session.commit()
    node = find_node(result.document, node_id)
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Блок не найден",
        )
    return templates.TemplateResponse(
        request=request,
        name="editor/node.html",
        context={
            "project_id": project_id,
            "node": node,
            "revision": result.revision,
        },
    )


def _project_or_404(session: Session, project_id: str) -> None:
    if ProjectRepository(session).get(project_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Проект не найден",
        )


__all__ = ["router"]
