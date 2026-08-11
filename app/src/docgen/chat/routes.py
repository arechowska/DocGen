from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from docgen.ai.client import ModelConfigurationError, ModelError, build_text_model
from docgen.chat.schemas import ChatEditRequest
from docgen.chat.service import ChatGroundingError, ChatService, ChatValidationError
from docgen.config import Settings
from docgen.documents.repository import DocumentRepository
from docgen.extraction.confluence import ConfluenceClient
from docgen.extraction.registry import ExtractorRegistry
from docgen.extraction.schemas import NormalizedBlock
from docgen.projects.routes import get_session
from docgen.sources.repository import SourceRepository
from docgen.sources.storage import LocalStorage
from docgen.web import templates
from docgen.workflows.normalize import NormalizationWorkflow

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
        source_blocks=lambda project_id: _source_blocks_from_project(
            session, request.app.state.settings, project_id
        ),
    )


def _source_blocks_from_project(
    session: Session, settings: Settings, project_id: str
) -> list[NormalizedBlock]:
    normalization = NormalizationWorkflow(
        SourceRepository(session),
        LocalStorage(settings.data_dir),
        ExtractorRegistry.default(settings),
        ConfluenceClient.from_settings(settings),
    )
    return normalization.run(project_id).blocks


__all__ = ["router"]
