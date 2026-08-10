from __future__ import annotations

import json
from collections.abc import Callable

from docgen.ai.client import TextModel
from docgen.chat.schemas import ChatEditPlan, ChatEditRequest, ChatEditResult
from docgen.documents.edit_service import DocumentEditService
from docgen.documents.operations import (
    DeleteNode,
    DocumentOperation,
    InsertNode,
    MoveNode,
    UpdateData,
    UpdateText,
    find_node,
)
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import WorkingDocument
from docgen.extraction.schemas import NormalizedBlock

CHAT_SYSTEM_PROMPT = """
Вы редактируете DocGen-документ на русском языке.
Верните только структурированный план правок.
Используйте операции только против существующих node_id, кроме явной вставки нового блока.
Каждое фактическое добавление должно иметь evidence_block_ids из источников проекта.
Если подтверждения в источниках нет, верните пустой список operations.
Не удаляйте содержимое сверх прямого запроса пользователя.
""".strip()


class ChatGroundingError(ValueError):
    pass


class ChatValidationError(ValueError):
    pass


class ChatService:
    def __init__(
        self,
        *,
        documents: DocumentRepository,
        model: TextModel,
        source_blocks: Callable[[str], list[NormalizedBlock]],
    ) -> None:
        self._documents = documents
        self._model = model
        self._source_blocks = source_blocks

    def edit(self, project_id: str, request: ChatEditRequest) -> ChatEditResult:
        message = _validated_message(request.message)
        stored = self._documents.get_document_with_revision(project_id)
        if stored is None:
            raise ChatValidationError("Документ не найден")
        document, current_revision = stored
        if current_revision != request.expected_revision:
            raise ChatValidationError("Документ уже изменён")

        source_blocks = self._source_blocks(project_id)
        plan = self._model.generate_json(
            system=CHAT_SYSTEM_PROMPT,
            user=_serialized_context(document, source_blocks, message),
            schema=ChatEditPlan,
        )
        _validate_plan(document, source_blocks, plan)
        result = DocumentEditService(self._documents).apply(
            project_id,
            request.expected_revision,
            plan.operations,
        )
        return ChatEditResult(
            summary=plan.summary,
            document=result.document,
            revision=result.revision,
        )


def _validated_message(message: str) -> str:
    normalized = message.strip()
    if not normalized:
        raise ChatValidationError("Сообщение обязательно")
    if len(normalized) > 4000:
        raise ChatValidationError("Сообщение слишком длинное")
    return normalized


def _serialized_context(
    document: WorkingDocument,
    source_blocks: list[NormalizedBlock],
    message: str,
) -> str:
    return json.dumps(
        {
            "document": document.model_dump(mode="json"),
            "source_blocks": [block.model_dump(mode="json") for block in source_blocks],
            "message": message,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _validate_plan(
    document: WorkingDocument,
    source_blocks: list[NormalizedBlock],
    plan: ChatEditPlan,
) -> None:
    known_evidence = {block.id for block in source_blocks}
    if any(block_id not in known_evidence for block_id in plan.evidence_block_ids):
        raise ChatGroundingError("Для этой правки нет подтверждения в источниках")
    if plan.operations and not plan.evidence_block_ids:
        raise ChatGroundingError("Для этой правки нет подтверждения в источниках")

    inserted_ids: set[str] = set()
    for operation in plan.operations:
        _validate_operation(document, operation, inserted_ids)


def _validate_operation(
    document: WorkingDocument,
    operation: DocumentOperation,
    inserted_ids: set[str],
) -> None:
    if isinstance(operation, InsertNode):
        if find_node(document, operation.node.id) is not None or operation.node.id in inserted_ids:
            raise ChatValidationError("План содержит повторяющийся идентификатор блока")
        inserted_ids.add(operation.node.id)
        if operation.parent_id is not None and find_node(document, operation.parent_id) is None:
            raise ChatValidationError("План ссылается на неизвестный блок")
        return

    if isinstance(operation, (UpdateText, UpdateData, DeleteNode, MoveNode)):
        if find_node(document, operation.node_id) is None:
            raise ChatValidationError("План ссылается на неизвестный блок")
        if (
            isinstance(operation, MoveNode)
            and operation.parent_id is not None
            and find_node(document, operation.parent_id) is None
        ):
            raise ChatValidationError("План ссылается на неизвестный блок")
        return

    raise ChatValidationError("План содержит неподдерживаемую операцию")
