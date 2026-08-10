from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from docgen.documents.edit_service import DocumentEditService, EditConflict
from docgen.documents.operations import (
    DeleteNode,
    EditValidationError,
    InsertNode,
    MoveNode,
    UpdateData,
    UpdateText,
    find_node,
)
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.editor.validation import ImagePayload, ListPayload, TablePayload
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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


@router.post("/{project_id}/editor/nodes")
def insert_node(
    request: Request,
    project_id: str,
    kind: Annotated[str, Form()],
    revision: Annotated[int, Form()],
    session: SessionDependency,
    after_node_id: Annotated[str | None, Form()] = None,
) -> Response:
    _project_or_404(session, project_id)
    stored = DocumentRepository(session).get_document_with_revision(project_id)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Документ не найден")
    document, _ = stored
    parent_id, index = _insertion_target(document, after_node_id)
    node = _new_node(kind)
    return _apply_and_render_document(
        request,
        session,
        project_id,
        revision,
        [InsertNode(parent_id=parent_id, index=index, node=node)],
    )


@router.delete("/{project_id}/editor/nodes/{node_id}")
def delete_node(
    request: Request,
    project_id: str,
    node_id: str,
    revision: Annotated[int, Form()],
    session: SessionDependency,
) -> Response:
    _project_or_404(session, project_id)
    return _apply_and_render_document(
        request,
        session,
        project_id,
        revision,
        [DeleteNode(node_id=node_id)],
    )


@router.post("/{project_id}/editor/nodes/{node_id}/move")
def move_node(
    request: Request,
    project_id: str,
    node_id: str,
    direction: Annotated[str, Form()],
    revision: Annotated[int, Form()],
    session: SessionDependency,
) -> Response:
    _project_or_404(session, project_id)
    stored = DocumentRepository(session).get_document_with_revision(project_id)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Документ не найден")
    document, _ = stored
    parent_id, index = _move_target(document, node_id, direction)
    return _apply_and_render_document(
        request,
        session,
        project_id,
        revision,
        [MoveNode(node_id=node_id, parent_id=parent_id, index=index)],
    )


@router.patch("/{project_id}/editor/nodes/{node_id}/list")
async def update_list_node(
    request: Request,
    project_id: str,
    node_id: str,
    session: SessionDependency,
) -> Response:
    _project_or_404(session, project_id)
    _ensure_node_kind(session, project_id, node_id, NodeKind.LIST)
    payload = await _list_payload(request)
    return _apply_and_render_node(
        request,
        session,
        project_id,
        node_id,
        payload.revision,
        [UpdateData(node_id=node_id, data={"items": payload.items})],
    )


@router.patch("/{project_id}/editor/nodes/{node_id}/table")
async def update_table_node(
    request: Request,
    project_id: str,
    node_id: str,
    session: SessionDependency,
) -> Response:
    _project_or_404(session, project_id)
    _ensure_node_kind(session, project_id, node_id, NodeKind.TABLE)
    payload = await _table_payload(request)
    return _apply_and_render_node(
        request,
        session,
        project_id,
        node_id,
        payload.revision,
        [UpdateData(node_id=node_id, data={"rows": payload.rows})],
    )


@router.patch("/{project_id}/editor/nodes/{node_id}/image")
async def update_image_node(
    request: Request,
    project_id: str,
    node_id: str,
    session: SessionDependency,
) -> Response:
    _project_or_404(session, project_id)
    _ensure_node_kind(session, project_id, node_id, NodeKind.IMAGE)
    payload = await _image_payload(request)
    return _apply_and_render_node(
        request,
        session,
        project_id,
        node_id,
        payload.revision,
        [
            UpdateData(
                node_id=node_id,
                data={
                    "alignment": payload.alignment,
                    "width": payload.width,
                    "alt": payload.alt,
                },
            )
        ],
    )


def _project_or_404(session: Session, project_id: str) -> None:
    if ProjectRepository(session).get(project_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Проект не найден",
        )


def _apply_and_render_node(
    request: Request,
    session: Session,
    project_id: str,
    node_id: str,
    revision: int,
    operations: list,
) -> Response:
    repository = DocumentRepository(session)
    service = DocumentEditService(repository)
    try:
        result = service.apply(project_id, revision, operations)
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
            context={"message": str(error), "value": None},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    session.commit()
    node = find_node(result.document, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Блок не найден")
    return templates.TemplateResponse(
        request=request,
        name="editor/node.html",
        context={
            "project_id": project_id,
            "node": node,
            "revision": result.revision,
        },
    )


def _apply_and_render_document(
    request: Request,
    session: Session,
    project_id: str,
    revision: int,
    operations: list,
) -> Response:
    repository = DocumentRepository(session)
    service = DocumentEditService(repository)
    try:
        result = service.apply(project_id, revision, operations)
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
            context={"message": str(error), "value": None},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    session.commit()
    return templates.TemplateResponse(
        request=request,
        name="editor/document.html",
        context={
            "project_id": project_id,
            "document": result.document,
            "revision": result.revision,
        },
    )


def _ensure_node_kind(
    session: Session,
    project_id: str,
    node_id: str,
    expected_kind: NodeKind,
) -> None:
    document = DocumentRepository(session).get_document(project_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Документ не найден")
    node = find_node(document, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Блок не найден")
    if node.kind is not expected_kind:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Тип блока не соответствует операции",
        )


async def _list_payload(request: Request) -> ListPayload:
    if _is_json(request):
        return _validated_payload(ListPayload, await request.json())
    form = await request.form()
    raw_items = form.getlist("items")
    items_text = str(form.get("items_text", ""))
    items = [str(item) for item in raw_items] if raw_items else items_text.splitlines()
    return _validated_payload(
        ListPayload,
        {"revision": form.get("revision"), "items": items},
    )


async def _table_payload(request: Request) -> TablePayload:
    if _is_json(request):
        return _validated_payload(TablePayload, await request.json())
    form = await request.form()
    rows_text = str(form.get("rows_text", ""))
    rows = [
        [cell.strip() for cell in row.split("\t")]
        for row in rows_text.splitlines()
        if row.strip()
    ]
    return _validated_payload(
        TablePayload,
        {"revision": form.get("revision"), "rows": rows},
    )


async def _image_payload(request: Request) -> ImagePayload:
    if _is_json(request):
        return _validated_payload(ImagePayload, await request.json())
    form = await request.form()
    return _validated_payload(
        ImagePayload,
        {
            "revision": form.get("revision"),
            "alignment": form.get("alignment"),
            "width": form.get("width"),
            "alt": form.get("alt"),
        },
    )


def _validated_payload(model, payload):
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


def _is_json(request: Request) -> bool:
    return "application/json" in request.headers.get("content-type", "")


def _new_node(kind: str) -> DocumentNode:
    match kind:
        case "heading":
            return DocumentNode(
                id=str(uuid4()),
                kind=NodeKind.HEADING,
                text="Новый раздел",
            )
        case "paragraph":
            return DocumentNode(
                id=str(uuid4()),
                kind=NodeKind.PARAGRAPH,
                text="Новый абзац",
            )
        case "list":
            return DocumentNode(
                id=str(uuid4()),
                kind=NodeKind.LIST,
                data={"items": [""]},
            )
        case "table":
            return DocumentNode(
                id=str(uuid4()),
                kind=NodeKind.TABLE,
                data={"rows": [["", ""], ["", ""]]},
            )
        case _:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Тип блока не поддерживается",
            )


def _insertion_target(
    document: WorkingDocument, after_node_id: str | None
) -> tuple[str | None, int]:
    if after_node_id is None:
        return None, len(document.nodes)
    location = _locate_node(document.nodes, after_node_id)
    if location is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Блок не найден")
    parent_id, index, _ = location
    return parent_id, index + 1


def _move_target(document: WorkingDocument, node_id: str, direction: str) -> tuple[str | None, int]:
    if direction not in {"up", "down"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Направление перемещения не поддерживается",
        )
    location = _locate_node(document.nodes, node_id)
    if location is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Блок не найден")
    parent_id, index, siblings = location
    if direction == "up":
        return parent_id, max(index - 1, 0)
    return parent_id, min(index + 1, len(siblings) - 1)


def _locate_node(
    nodes: list[DocumentNode],
    node_id: str,
    *,
    parent_id: str | None = None,
) -> tuple[str | None, int, list[DocumentNode]] | None:
    for index, node in enumerate(nodes):
        if node.id == node_id:
            return parent_id, index, nodes
        child_location = _locate_node(node.children, node_id, parent_id=node.id)
        if child_location is not None:
            return child_location
    return None


__all__ = ["router"]
