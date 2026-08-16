from __future__ import annotations

import json
from typing import Annotated
from uuid import uuid4

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError
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
from docgen.documents.style import normalized_style, normalized_style_attribute
from docgen.editor.validation import ImagePayload, ListPayload, TablePayload
from docgen.projects.repository import ProjectRepository
from docgen.projects.routes import get_session
from docgen.web import templates

router = APIRouter(prefix="/projects")

SessionDependency = Annotated[Session, Depends(get_session)]


class Docgen2SavePayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    html: str = Field(max_length=500_000)
    revision: int = Field(ge=1)


@router.post("/{project_id}/editor/save")
def save_docgen2_workspace(
    project_id: str,
    payload: Docgen2SavePayload,
    session: SessionDependency,
) -> Response:
    _project_or_404(session, project_id)
    title = payload.title.strip()
    if not title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Название документа обязательно",
        )
    html = _sanitize_workspace_html(payload.html)
    repository = DocumentRepository(session)
    stored = repository.get_document_with_revision(project_id)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Документ не найден",
        )
    current, current_revision = stored
    if current_revision != payload.revision:
        session.rollback()
        return JSONResponse(
            {"detail": "Документ уже изменён"},
            status_code=status.HTTP_409_CONFLICT,
        )
    try:
        document, html = _workspace_document_and_html(current, title, html)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    revision = repository.save_workspace(
        project_id,
        payload.revision,
        document,
        html,
    )
    if revision is None:
        session.rollback()
        return JSONResponse(
            {"detail": "Документ уже изменён"},
            status_code=status.HTTP_409_CONFLICT,
        )
    session.commit()
    return JSONResponse({"revision": revision, "title": title, "html": html})


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
    return _with_document_updated_trigger(
        templates.TemplateResponse(
            request=request,
            name="editor/node.html",
            context={
                "project_id": project_id,
                "node": node,
                "revision": result.revision,
            },
        ),
        result.revision,
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
    table = _ensure_node_kind(session, project_id, node_id, NodeKind.TABLE)
    payload = await _table_payload(request)
    data = dict(table.data)
    data["rows"] = payload.rows
    return _apply_and_render_node(
        request,
        session,
        project_id,
        node_id,
        payload.revision,
        [UpdateData(node_id=node_id, data=data)],
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


def _with_document_updated_trigger(response: Response, revision: int) -> Response:
    """Attach the `docgen:document-updated` HX-Trigger header, exactly like
    chat/routes.py already does after an AI edit.

    docgen2-editor.js listens for this event and re-syncs every
    `input[name="revision"]` on the page -- including the export panel's and
    the chat panel's own hidden revision fields, both of which sit outside
    `#editor-shell` and are otherwise never refreshed by a direct toolbar
    edit. Without this, a toolbar edit (insert/delete/move/text/list/table
    /image) leaves those fields stale, and the next export or chat message
    incorrectly hits their conflict/stale-revision guard.
    """
    response.headers["HX-Trigger"] = json.dumps(
        {"docgen:document-updated": {"revision": revision}},
        ensure_ascii=False,
    )
    return response


def _project_or_404(session: Session, project_id: str):
    project = ProjectRepository(session).get(project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Проект не найден",
        )
    return project


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
    return _with_document_updated_trigger(
        templates.TemplateResponse(
            request=request,
            name="editor/node.html",
            context={
                "project_id": project_id,
                "node": node,
                "revision": result.revision,
            },
        ),
        result.revision,
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
    return _with_document_updated_trigger(
        templates.TemplateResponse(
            request=request,
            name="editor/surface.html",
            context={
                "project_id": project_id,
                "document": result.document,
                "revision": result.revision,
            },
        ),
        result.revision,
    )


def _ensure_node_kind(
    session: Session,
    project_id: str,
    node_id: str,
    expected_kind: NodeKind,
) -> DocumentNode:
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
    return node


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


ALLOWED_WORKSPACE_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}

ALLOWED_WORKSPACE_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"alt", "src", "title"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
WORKSPACE_NODE_ATTRIBUTES = {"data-node-id", "data-kind", "data-section-id"}
WORKSPACE_BLOCK_TAGS = {
    "blockquote",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "ol",
    "p",
    "pre",
    "table",
    "ul",
}


def _sanitize_workspace_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for tag in list(soup.find_all(True)):
        if tag.name in {"script", "style", "template"}:
            tag.decompose()
            continue
        if tag.name not in ALLOWED_WORKSPACE_TAGS:
            tag.unwrap()
            continue
        allowed_attributes = (
            ALLOWED_WORKSPACE_ATTRIBUTES.get(tag.name, set())
            | WORKSPACE_NODE_ATTRIBUTES
            | {"style"}
        )
        for attribute in list(tag.attrs):
            if attribute not in allowed_attributes:
                del tag.attrs[attribute]
                continue
            value = tag.attrs.get(attribute)
            if attribute == "style":
                sanitized_style = normalized_style_attribute(str(value))
                if sanitized_style:
                    tag.attrs[attribute] = sanitized_style
                else:
                    del tag.attrs[attribute]
                continue
            if attribute in {"href", "src"} and not _is_safe_workspace_url(value):
                del tag.attrs[attribute]
    return "".join(str(item) for item in soup.contents).strip()


def _is_safe_workspace_url(value) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized.startswith(
        ("#", "/", "http://", "https://", "mailto:", "data:image/")
    )


def workspace_document(
    current: WorkingDocument,
    title: str,
    html: str,
) -> WorkingDocument:
    document, _ = _workspace_document_and_html(current, title, html)
    return document


def _workspace_document_and_html(
    current: WorkingDocument,
    title: str,
    html: str,
) -> tuple[WorkingDocument, str]:
    soup = BeautifulSoup(html, "html.parser")
    elements = _workspace_elements(soup)
    current_nodes = {node.id: node for node in current.nodes}
    claimed_ids = [
        str(element.get("data-node-id"))
        for element in soup.find_all(attrs={"data-node-id": True})
    ]
    if len(claimed_ids) != len(set(claimed_ids)):
        raise ValueError("Идентификатор блока повторяется")
    if any(node_id not in current_nodes for node_id in claimed_ids):
        raise ValueError("Документ содержит неизвестный блок")
    if any(element.parent is not soup for element in soup.find_all(attrs={"data-node-id": True})):
        raise ValueError("Идентификатор блока должен быть на верхнем уровне")

    nodes: list[DocumentNode] = []
    for element in elements:
        claimed_id = element.get("data-node-id")
        existing = current_nodes.get(str(claimed_id)) if claimed_id else None
        node = _workspace_node(element, existing)
        _set_workspace_node_attributes(element, node)
        nodes.append(node)
    document = current.model_copy(update={"title": title, "nodes": nodes})
    normalized_html = "".join(str(item) for item in soup.contents).strip()
    return document, normalized_html


def _workspace_elements(soup: BeautifulSoup) -> list[Tag]:
    elements: list[Tag] = []
    for item in list(soup.contents):
        if isinstance(item, Comment):
            continue
        if isinstance(item, NavigableString):
            if not str(item).strip():
                continue
            wrapper = soup.new_tag("p")
            item.replace_with(wrapper)
            wrapper.append(item)
            elements.append(wrapper)
            continue
        if not isinstance(item, Tag):
            continue
        if item.name in WORKSPACE_BLOCK_TAGS:
            elements.append(item)
            continue
        if item.get_text(" ", strip=True):
            wrapper = soup.new_tag("p")
            item.replace_with(wrapper)
            wrapper.append(item)
            elements.append(wrapper)
    return elements


def _workspace_node(element: Tag, existing: DocumentNode | None) -> DocumentNode:
    kind = _workspace_node_kind(element)
    data = dict(existing.data) if existing is not None and existing.kind is kind else {}
    text: str | None = None
    if kind in {NodeKind.HEADING, NodeKind.PARAGRAPH}:
        text = element.get_text(" ", strip=True)
        _update_workspace_style_data(data, element)
    elif kind is NodeKind.LIST:
        data["items"] = [item.get_text(" ", strip=True) for item in element.find_all("li")]
        _update_workspace_style_data(data, element)
    elif kind is NodeKind.TABLE:
        headers, rows = _workspace_table_data(element)
        if headers:
            data["headers"] = headers
        else:
            data.pop("headers", None)
        data["rows"] = rows
        _update_workspace_style_data(data, element)
    elif kind is NodeKind.IMAGE:
        for attribute in ("alt", "src", "title"):
            if value := element.get(attribute):
                data[attribute] = str(value)
            else:
                data.pop(attribute, None)
        text = str(element.get("alt") or "") or None

    if existing is None:
        return DocumentNode(
            kind=kind,
            text=text,
            data=data,
            flags=["manual-edit"],
        )
    updates: dict = {"kind": kind, "data": data}
    if kind in {NodeKind.HEADING, NodeKind.PARAGRAPH, NodeKind.IMAGE}:
        updates["text"] = text
    elif existing.kind in {NodeKind.HEADING, NodeKind.PARAGRAPH, NodeKind.IMAGE}:
        updates["text"] = None
    return existing.model_copy(update=updates)


def _workspace_table_data(element: Tag) -> tuple[list[str], list[list[str]]]:
    table_rows = element.find_all("tr")
    header_rows = element.select("thead tr")
    headers: list[str] = []
    if header_rows:
        headers = [
            cell.get_text(" ", strip=True)
            for cell in header_rows[0].find_all(["th", "td"], recursive=False)
        ]
    elif table_rows:
        first_cells = table_rows[0].find_all(["th", "td"], recursive=False)
        if first_cells and all(cell.name == "th" for cell in first_cells):
            headers = [cell.get_text(" ", strip=True) for cell in first_cells]
            header_rows = [table_rows[0]]

    rows = [
        [
            cell.get_text(" ", strip=True)
            for cell in row.find_all(["th", "td"], recursive=False)
        ]
        for row in table_rows
        if row not in header_rows
    ]
    return headers, rows


def _update_workspace_style_data(data: dict, element: Tag) -> None:
    style = normalized_style(
        {"style": dict(_style_declarations(str(element.get("style", ""))))}
    )
    if style:
        data["style"] = style
    else:
        data.pop("style", None)


def _style_declarations(value: str):
    for declaration in value.split(";"):
        if ":" not in declaration:
            continue
        property_name, property_value = declaration.split(":", 1)
        yield property_name.strip(), property_value.strip()


def _workspace_node_kind(element: Tag) -> NodeKind:
    if element.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return NodeKind.HEADING
    if element.name in {"ul", "ol"}:
        return NodeKind.LIST
    if element.name == "table":
        return NodeKind.TABLE
    if element.name == "img":
        return NodeKind.IMAGE
    if element.name == "hr":
        return NodeKind.GAP
    return NodeKind.PARAGRAPH


def _set_workspace_node_attributes(element: Tag, node: DocumentNode) -> None:
    element["data-node-id"] = node.id
    element["data-kind"] = node.kind.value
    if node.section_id is not None:
        element["data-section-id"] = node.section_id
    else:
        element.attrs.pop("data-section-id", None)


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


__all__ = ["router", "workspace_document"]
