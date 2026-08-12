from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from typing import Any

from docgen.ai.client import TextModel
from docgen.chat.schemas import ChatEditOperation, ChatEditPlan, ChatEditRequest, ChatEditResult
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
from docgen.documents.schemas import DocumentNode, WorkingDocument
from docgen.extraction.schemas import NormalizedBlock

CHAT_SYSTEM_PROMPT = """
Вы редактируете DocGen-документ на русском языке.
Верните только структурированный план правок.
Используйте операции только против существующих node_id, кроме явной вставки нового блока.
Каждую операцию верните в объекте с полями operation и evidence_block_ids.
Каждое фактическое добавление должно иметь в своём объекте evidence_block_ids из источников проекта.
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
            [item.operation for item in plan.operations],
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
    evidence_by_id = {block.id: block for block in source_blocks}
    inserted_ids: set[str] = set()
    for item in plan.operations:
        _validate_operation(document, item.operation, inserted_ids)
        _validate_operation_evidence(document, evidence_by_id, item)


def _validate_operation_evidence(
    document: WorkingDocument,
    evidence_by_id: dict[str, NormalizedBlock],
    item: ChatEditOperation,
) -> None:
    try:
        cited_blocks = [evidence_by_id[block_id] for block_id in item.evidence_block_ids]
    except KeyError as error:
        raise ChatGroundingError(
            "Для этой правки нет подтверждения в источниках"
        ) from error

    changed_tokens = _factual_changed_tokens(document, item.operation)
    if not changed_tokens:
        return
    if not cited_blocks or not _tokens_supported(
        changed_tokens,
        _tokens(" ".join(block.text for block in cited_blocks)),
    ):
        raise ChatGroundingError("Для этой правки нет подтверждения в источниках")


_TOKEN_PATTERN = re.compile(r"[0-9]+|[^\W_]+", re.UNICODE)
_STOP_WORDS = {
    "без",
    "был",
    "была",
    "были",
    "быть",
    "вас",
    "все",
    "вы",
    "до",
    "для",
    "его",
    "если",
    "есть",
    "еще",
    "из",
    "или",
    "как",
    "который",
    "над",
    "нет",
    "но",
    "он",
    "она",
    "оно",
    "они",
    "при",
    "по",
    "под",
    "от",
    "мы",
    "со",
    "так",
    "то",
    "что",
    "это",
    "этот",
}
_NON_FACTUAL_DATA_KEYS = {
    "alignment",
    "class",
    "color",
    "height",
    "src",
    "style",
    "table_style",
    "uri",
    "url",
    "width",
}


def _factual_changed_tokens(
    document: WorkingDocument,
    operation: DocumentOperation,
) -> list[str]:
    before = ""
    after = ""
    if isinstance(operation, UpdateText):
        node = find_node(document, operation.node_id)
        before = node.text or "" if node is not None else ""
        after = operation.text
    elif isinstance(operation, UpdateData):
        node = find_node(document, operation.node_id)
        before = _factual_data_text(node.data if node is not None else {})
        after = _factual_data_text(operation.data)
    elif isinstance(operation, InsertNode):
        after = _node_factual_text(operation.node)
    else:
        return []

    before_counts = Counter(_tokens(before))
    changed: list[str] = []
    for token in _tokens(after):
        if before_counts[token]:
            before_counts[token] -= 1
        else:
            changed.append(token)
    return changed


def _node_factual_text(node: DocumentNode) -> str:
    parts = [node.text or "", _factual_data_text(node.data)]
    parts.extend(_node_factual_text(child) for child in node.children)
    return " ".join(parts)


def _factual_data_text(value: Any, *, key: str | None = None) -> str:
    if key is not None and key.casefold() in _NON_FACTUAL_DATA_KEYS:
        return ""
    if isinstance(value, dict):
        return " ".join(
            _factual_data_text(item, key=str(item_key))
            for item_key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return " ".join(_factual_data_text(item, key=key) for item in value)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return [
        token
        for token in _TOKEN_PATTERN.findall(normalized)
        if token.isdigit() or (len(token) >= 2 and token not in _STOP_WORDS)
    ]


def _tokens_supported(changed: list[str], evidence: list[str]) -> bool:
    numeric = [token for token in changed if token.isdigit()]
    evidence_numbers = {token for token in evidence if token.isdigit()}
    if any(token not in evidence_numbers for token in numeric):
        return False

    words = [token for token in changed if not token.isdigit()]
    if not words:
        return True
    matched = sum(
        1
        for token in words
        if any(_tokens_match(token, evidence_token) for evidence_token in evidence)
    )
    required = 1 if len(words) == 1 else max(2, math.ceil(len(words) * 0.6))
    return matched >= required


def _tokens_match(left: str, right: str) -> bool:
    if left == right:
        return True
    if left.isdigit() or right.isdigit():
        return False
    common = 0
    for left_character, right_character in zip(left, right, strict=False):
        if left_character != right_character:
            break
        common += 1
    return common >= 5 and common / max(len(left), len(right)) >= 0.6


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
