from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from docgen.ai.client import ModelConfigurationError, ModelError, build_text_model
from docgen.chat.errors import ChatError
from docgen.chat.retrieval import SourceSnapshot, SourceSnapshotCache
from docgen.chat.schemas import ChatEditRequest
from docgen.chat.service import ChatService
from docgen.config import Settings
from docgen.documents.repository import DocumentRepository
from docgen.extraction.confluence import ConfluenceClient
from docgen.extraction.registry import ExtractionError, ExtractorRegistry
from docgen.extraction.schemas import NormalizedBlock
from docgen.projects.repository import ProjectRepository
from docgen.projects.routes import get_session
from docgen.sources.repository import SourceRepository
from docgen.sources.storage import LocalStorage
from docgen.web import templates
from docgen.workflows.normalize import NormalizationWorkflow, PageLimitExceeded

router = APIRouter(prefix="/projects")

SessionDependency = Annotated[Session, Depends(get_session)]


@router.post("/{project_id}/chat")
def post_chat(
    request: Request,
    project_id: str,
    session: SessionDependency,
    message: Annotated[str | None, Form()] = None,
    revision: Annotated[int | None, Form()] = None,
) -> Response:
    if message is None or not message.strip() or revision is None:
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={"message": "Введите сообщение и повторите попытку"},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    chat = _chat_service(request, session)
    try:
        result = chat.edit(
            project_id,
            ChatEditRequest(message=message, expected_revision=revision),
        )
    except ChatError as error:
        session.rollback()
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={
                "message": error.message,
                "action": error.action,
                "error_code": error.code.value,
            },
            status_code=error.status_code,
        )
    except ModelConfigurationError:
        session.rollback()
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={"message": "Локальная модель недоступна"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except ModelError as error:
        session.rollback()
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={"message": str(error)},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    session.commit()
    response = templates.TemplateResponse(
        request=request,
        name="chat/message.html",
        context={
            "summary": result.summary,
            "revision": result.revision,
            "project": ProjectRepository(session).get(project_id),
            "project_id": project_id,
            "document": result.document,
            "workspace_html": (
                DocumentRepository(session).get_workspace_html(project_id)
                if result.revision == revision
                else None
            ),
            "editor_oob": True,
        },
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
        source_blocks=lambda project_id: _source_snapshot_from_project(
            session,
            request.app.state.settings,
            project_id,
            cache=_snapshot_cache(request),
        ),
    )


def _snapshot_cache(request: Request) -> SourceSnapshotCache:
    cache = getattr(request.app.state, "chat_source_snapshot_cache", None)
    if cache is None:
        cache = SourceSnapshotCache()
        request.app.state.chat_source_snapshot_cache = cache
    return cache


def _source_snapshot_from_project(
    session: Session,
    settings: Settings,
    project_id: str,
    *,
    cache: SourceSnapshotCache | None = None,
) -> SourceSnapshot:
    sources = SourceRepository(session)
    configured = sources.list_for_project(project_id)
    identity = "|".join(
        f"{source.id}:{source.status}:{source.created_at.isoformat()}"
        for source in configured
    )
    if cache is not None:
        cached = cache.get(project_id, identity)
        if cached is not None:
            return cached
    if not configured:
        return SourceSnapshot(configured_source_count=0, identity=identity)

    normalization = NormalizationWorkflow(
        sources,
        LocalStorage(settings.data_dir),
        ExtractorRegistry.default(settings),
        ConfluenceClient.from_settings(settings),
    )
    try:
        normalized = normalization.run(project_id)
        snapshot = SourceSnapshot(
            configured_source_count=len(configured),
            blocks=normalized.blocks,
            warnings=tuple(normalized.warnings),
            identity=identity,
        )
    except (ExtractionError, PageLimitExceeded, ValueError) as error:
        snapshot = SourceSnapshot(
            configured_source_count=len(configured),
            warnings=(str(error),),
            identity=identity,
        )
    if cache is not None:
        cache.put(project_id, snapshot)
    return snapshot


def _source_blocks_from_project(
    session: Session,
    settings: Settings,
    project_id: str,
) -> list[NormalizedBlock]:
    """Compatibility helper for callers that only inspect normalized blocks."""
    return list(_source_snapshot_from_project(session, settings, project_id).blocks)


__all__ = ["router"]
