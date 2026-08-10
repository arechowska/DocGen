from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from docgen.ai.client import ModelConfigurationError, ModelError, build_text_model
from docgen.chat.schemas import ChatEditRequest
from docgen.chat.service import ChatGroundingError, ChatService, ChatValidationError
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind
from docgen.extraction.schemas import BlockKind, NormalizedBlock
from docgen.projects.routes import get_session
from docgen.web import templates

router = APIRouter(prefix="/projects")

SessionDependency = Annotated[Session, Depends(get_session)]


@router.post("/{project_id}/chat")
def post_chat(
    request: Request,
    project_id: str,
    message: Annotated[str, Form()],
    revision: Annotated[int, Form()],
    session: SessionDependency,
) -> Response:
    chat = _chat_service(request, session)
    try:
        result = chat.edit(
            project_id,
            ChatEditRequest(message=message, expected_revision=revision),
        )
    except (ChatGroundingError, ChatValidationError) as error:
        session.rollback()
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={"message": str(error)},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    except (ModelConfigurationError, ModelError):
        session.rollback()
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={"message": "Локальная модель недоступна"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    session.commit()
    response = templates.TemplateResponse(
        request=request,
        name="chat/message.html",
        context={"summary": result.summary, "revision": result.revision},
    )
    response.headers["HX-Trigger"] = json.dumps(
        {"docgen:document-updated": {"revision": result.revision}},
        ensure_ascii=False,
    )
    return response


def _chat_service(request: Request, session: Session):
    factory = getattr(request.app.state, "chat_service_factory", None)
    if factory is not None:
        return factory(request, session)
    return ChatService(
        documents=DocumentRepository(session),
        model=build_text_model(request.app.state.settings),
        source_blocks=lambda project_id: _source_blocks_from_document(session, project_id),
    )


def _source_blocks_from_document(session: Session, project_id: str) -> list[NormalizedBlock]:
    document = DocumentRepository(session).get_document(project_id)
    if document is None:
        return []
    return [
        NormalizedBlock(
            id=f"document:{node.id}",
            kind=_block_kind(node.kind),
            text=node.text or json.dumps(node.data, ensure_ascii=False),
            confidence=1,
        )
        for node in _walk_nodes(document.nodes)
        if node.text or node.data
    ]


def _block_kind(kind: NodeKind) -> BlockKind:
    if kind is NodeKind.HEADING:
        return BlockKind.HEADING
    if kind is NodeKind.LIST:
        return BlockKind.LIST
    if kind is NodeKind.TABLE:
        return BlockKind.TABLE
    if kind is NodeKind.IMAGE:
        return BlockKind.IMAGE
    return BlockKind.TEXT


def _walk_nodes(nodes: list[DocumentNode]):
    for node in nodes:
        yield node
        yield from _walk_nodes(node.children)


__all__ = ["router"]
