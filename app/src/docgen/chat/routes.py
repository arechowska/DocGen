from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from docgen.ai.client import ModelConfigurationError, ModelError, build_text_model
from docgen.chat.errors import ChatError, ChatErrorCode
from docgen.chat.proposals import FindingFixProposalRepository
from docgen.chat.retrieval import SourceSnapshot, SourceSnapshotCache
from docgen.chat.schemas import ChatEditRequest, ChatEditResult
from docgen.chat.service import ChatService, validate_finding_fix_scope
from docgen.config import Settings
from docgen.documents.edit_service import DocumentEditService, EditConflict
from docgen.documents.operations import EditValidationError, find_node
from docgen.documents.repository import DocumentRepository
from docgen.extraction.confluence import ConfluenceClient
from docgen.extraction.registry import ExtractionError, ExtractorRegistry
from docgen.extraction.schemas import NormalizedBlock
from docgen.projects.repository import ProjectRepository
from docgen.projects.routes import get_session
from docgen.sources.repository import SourceRepository
from docgen.sources.storage import LocalStorage
from docgen.templates_catalog.loader import TemplateCatalog
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

    return _run_chat_edit(
        request,
        session,
        project_id,
        revision,
        lambda chat: chat.edit(
            project_id,
            ChatEditRequest(message=message, expected_revision=revision),
        ),
    )


@router.post("/{project_id}/report/findings/{rule_id}/propose-fix")
def post_propose_finding_fix(
    request: Request,
    project_id: str,
    rule_id: str,
    session: SessionDependency,
    revision: Annotated[int | None, Form()] = None,
) -> Response:
    if revision is None:
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={"message": "Не удалось определить ревизию документа"},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    report = DocumentRepository(session).get_report(project_id)
    finding = next(
        (item for item in report.findings if item.rule_id == rule_id),
        None,
    ) if report is not None else None
    if finding is None or not finding.suggestion:
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={"message": "Для этой проблемы нет предложенного исправления"},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    if finding.code == "template-structure-mismatch" or not finding.node_id:
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={
                "message": "Это расхождение нельзя исправить одной безопасной правкой",
                "action": "Перейди к документу и исправь структуру вручную.",
            },
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    chat = _chat_service(request, session)
    try:
        proposal = chat.propose_finding_fix(project_id, finding, revision)
    except ChatError as error:
        session.rollback()
        return _chat_error_response(request, error)
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

    report_record = DocumentRepository(session).get_latest_report_record(project_id)
    if report_record is None or report_record.document_revision != revision:
        session.rollback()
        return _chat_error_response(
            request, ChatError(ChatErrorCode.REVISION_CONFLICT)
        )
    stored = FindingFixProposalRepository(session).create(
        project_id=project_id,
        report_id=report_record.id,
        document_revision=revision,
        finding_rule_id=rule_id,
        finding_code=finding.code,
        summary=proposal.summary,
        operations=proposal.operations,
    )
    before_document = DocumentRepository(session).get_document(project_id)
    before = find_node(before_document, finding.node_id) if before_document else None
    after = find_node(proposal.document, finding.node_id)
    session.commit()
    return templates.TemplateResponse(
        request=request,
        name="chat/fix_preview.html",
        context={
            "project_id": project_id,
            "proposal": stored,
            "finding": finding,
            "before": _node_preview(before),
            "after": _node_preview(after),
        },
    )


@router.post("/{project_id}/report/fix-proposals/{proposal_id}/apply")
def post_apply_finding_fix_proposal(
    request: Request,
    project_id: str,
    proposal_id: str,
    session: SessionDependency,
) -> Response:
    proposals = FindingFixProposalRepository(session)
    proposal = proposals.get(proposal_id)
    if proposal is None or proposal.project_id != project_id:
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={"message": "Предложение правки не найдено"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if proposal.applied:
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={"message": "Эта правка уже применена"},
            status_code=status.HTTP_409_CONFLICT,
        )
    documents = DocumentRepository(session)
    report_record = documents.get_report_record(proposal.report_id)
    if report_record is None or report_record.project_id != project_id:
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={"message": "Исходный отчёт не найден"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    finding = next(
        (
            item
            for item in report_record.report.findings
            if item.rule_id == proposal.finding_rule_id
            and item.code == proposal.finding_code
        ),
        None,
    )
    if finding is None:
        return templates.TemplateResponse(
            request=request,
            name="chat/error.html",
            context={"message": "Расхождение из предложения не найдено"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    operations = proposals.operations(proposal)
    try:
        validate_finding_fix_scope(finding, operations)
        result = DocumentEditService(documents).apply(
            project_id, proposal.document_revision, operations
        )
    except ChatError as error:
        session.rollback()
        return _chat_error_response(request, error)
    except EditConflict:
        session.rollback()
        return _chat_error_response(
            request, ChatError(ChatErrorCode.REVISION_CONFLICT)
        )
    except EditValidationError as error:
        session.rollback()
        return _chat_error_response(
            request,
            ChatError(ChatErrorCode.INVALID_OPERATION, message=str(error)),
        )
    proposal.applied = True
    session.commit()
    stale_report = report_record.report
    confirmed = [item for item in stale_report.findings if item.confidence >= 0.7]
    low_confidence = [item for item in stale_report.findings if item.confidence < 0.7]
    response = templates.TemplateResponse(
        request=request,
        name="chat/message.html",
        context={
            "summary": "Правка применена. Перепроверь документ по шаблону.",
            "revision": result.revision,
            "project": ProjectRepository(session).get(project_id),
            "project_id": project_id,
            "document": result.document,
            "workspace_html": documents.get_workspace_html(project_id),
            "editor_oob": True,
            "has_report": True,
            "stale_report": report_record,
            "report": stale_report,
            "confirmed": confirmed,
            "low_confidence": low_confidence,
            "rule_instructions": _rule_instructions(request, stale_report.template_id),
            "stale": True,
            "replace_report": True,
            "report_target_source_id": report_record.target_source_id,
        },
    )
    response.headers["HX-Trigger"] = json.dumps(
        {"docgen:document-updated": {"revision": result.revision}},
        ensure_ascii=False,
    )
    return response


def _run_chat_edit(
    request: Request,
    session: Session,
    project_id: str,
    revision: int,
    run: Callable[[ChatService], ChatEditResult],
) -> Response:
    chat = _chat_service(request, session)
    try:
        result = run(chat)
    except ChatError as error:
        session.rollback()
        return _chat_error_response(request, error)
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
    documents = DocumentRepository(session)
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
                documents.get_workspace_html(project_id)
                if result.revision == revision
                else None
            ),
            "editor_oob": True,
            "has_report": documents.get_latest_report_record(project_id) is not None,
        },
    )
    response.headers["HX-Trigger"] = json.dumps(
        {"docgen:document-updated": {"revision": result.revision}},
        ensure_ascii=False,
    )
    return response


def _chat_error_response(request: Request, error: ChatError) -> Response:
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


def _node_preview(node: object) -> str:
    if node is None:
        return ""
    text = getattr(node, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    data = getattr(node, "data", None)
    if isinstance(data, dict) and data:
        return json.dumps(data, ensure_ascii=False, indent=2)
    return "Пустой блок"


def _rule_instructions(request: Request, template_id: str) -> dict[str, str]:
    try:
        semantic_template = TemplateCatalog(
            external_directory=request.app.state.settings.template_dir
        ).get(template_id)
    except ValueError:
        return {}
    return {rule.id: rule.instruction for rule in semantic_template.rules}


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
