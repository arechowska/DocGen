from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from typing import Any

from docgen.ai.client import ModelResponseFormatError, TextModel
from docgen.chat.adapters import FaqAdapter
from docgen.chat.errors import ChatError, ChatErrorCode
from docgen.chat.executors import (
    authored_operations,
    formatting_operations,
    structural_operations,
)
from docgen.chat.intents import IntentKind, route_intent
from docgen.chat.manual_insert import (
    ManualInsertError,
)
from docgen.chat.retrieval import SourceSnapshot, retrieve_relevant_blocks
from docgen.chat.schemas import (
    ChatEditOperation,
    ChatEditPlan,
    ChatEditRequest,
    ChatEditResult,
    FaqEntryDraft,
)
from docgen.documents.edit_service import DocumentEditService, EditConflict
from docgen.documents.operations import (
    DeleteNode,
    DocumentOperation,
    EditValidationError,
    InsertNode,
    MoveNode,
    UpdateData,
    UpdateProvenance,
    UpdateText,
    find_node,
)
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import NormalizedBlock, Provenance

CHAT_SYSTEM_PROMPT = """
Вы редактируете DocGen-документ на русском языке.
Верните только структурированный план правок.
Используйте операции только против существующих node_id, кроме явной вставки нового блока.
Каждую операцию верните в объекте с полями operation и evidence_block_ids.
Каждое фактическое добавление должно иметь в своём объекте evidence_block_ids из источников проекта.
Нефактические правки форматирования, шрифта, цвета, выравнивания и отступов не требуют evidence_block_ids.
Структурные команды (разделить на разделы, сгруппировать, переставить блоки) не добавляют новых фактов и не требуют evidence_block_ids. Для них сохраняйте текст блоков, используйте MoveNode и при необходимости InsertNode с заголовками; не возвращайте пустой operations, если документ можно перестроить.
Для таких команд используйте operation.kind="update_data" по существующему node_id и сохраняйте прежние data-поля узла.
Форматирование задавайте внутри поля operation.data.style только безопасными CSS-ключами: color, background-color, font-family, font-size, font-style, font-weight, line-height, margin-left, margin-right, margin-top, margin-bottom, text-align, text-decoration, text-indent.
Пример operation: {"kind":"update_data","node_id":"...","data":{"style":{"color":"blue","font-weight":"700","margin-left":"24px"}}}. Не используйте отдельное поле "data.style".
Если подтверждения в источниках нет, верните пустой список operations.
Если правок нет, верните JSON-объект {"summary":"Нет правок","operations":[]}, а не отдельный список.
Не удаляйте содержимое сверх прямого запроса пользователя.
""".strip()

CHAT_RETRY_PROMPT = """
Предыдущий ответ не принят: верни только один валидный JSON-объект по схеме ChatEditPlan.
Не пиши пояснений, Markdown, префиксов и текста вне JSON. Используй пустой operations,
если не можешь обосновать правку источниками.
""".strip()

CHAT_GROUNDING_RETRY_PROMPT = """
Предыдущий план не прошёл проверку источников. Исправь план: каждая фактическая операция должна ссылаться в evidence_block_ids на существующие source_blocks, подтверждающие добавляемый или изменяемый текст. Не используй идентификаторы наугад. Если подтверждения нет, исключи такую операцию.
""".strip()

FAQ_SYSTEM_PROMPT = """
Ты создаёшь одну подтверждённую FAQ-запись для DocGen-документа.
Верни только объект FaqEntryDraft: непустые question и answer, placement и
evidence_block_ids. Ответ использует только факты из переданных source_blocks.
Не помещай evidence_block_ids, placement и другие служебные поля в question или answer.
""".strip()

class ChatGroundingError(ChatError):
    def __init__(
        self,
        message: str | None = None,
        *,
        code: ChatErrorCode = ChatErrorCode.GROUNDING_FAILED,
    ) -> None:
        super().__init__(code, message=message)


class ChatValidationError(ChatError):
    def __init__(self, message: str) -> None:
        super().__init__(ChatErrorCode.INVALID_OPERATION, message=message)


class ChatService:
    def __init__(
        self,
        *,
        documents: DocumentRepository,
        model: TextModel,
        source_blocks: Callable[
            [str], SourceSnapshot | list[NormalizedBlock]
        ],
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
            raise ChatError(ChatErrorCode.REVISION_CONFLICT)

        try:
            decision = route_intent(message, document)
        except ManualInsertError as error:
            raise ChatValidationError(str(error)) from error

        if decision.kind is IntentKind.CLARIFICATION:
            raise ChatError(
                ChatErrorCode.CLARIFICATION,
                message=decision.clarification,
                action="Сформулируй правку точнее и повтори запрос.",
            )
        if decision.kind is IntentKind.AUTHORED_EDIT:
            try:
                operations = authored_operations(document, decision)
            except ManualInsertError as error:
                raise ChatValidationError(str(error)) from error
            return self._apply_operations(
                project_id,
                request.expected_revision,
                operations,
                summary="Применена авторская правка",
            )
        if decision.kind is IntentKind.FORMAT:
            return self._apply_operations(
                project_id,
                request.expected_revision,
                formatting_operations(document, message),
                summary="Применено форматирование",
            )
        if decision.kind is IntentKind.STRUCTURE:
            return self._apply_operations(
                project_id,
                request.expected_revision,
                structural_operations(document, decision),
                summary="Документ структурирован",
            )

        snapshot = _as_source_snapshot(self._source_blocks(project_id))
        retrieved = retrieve_relevant_blocks(snapshot, decision.retrieval_query)
        source_blocks = retrieved.blocks

        if decision.kind is IntentKind.TEMPLATE_ACTION:
            try:
                plan = self._faq_plan(document, source_blocks, message)
            except ChatGroundingError:
                plan = self._faq_plan(
                    document,
                    source_blocks,
                    message,
                    retry_grounding=True,
                )
        else:
            plan = self._generate_with_schema_retry(
                system=CHAT_SYSTEM_PROMPT,
                user=_serialized_context(document, source_blocks, message),
                schema=ChatEditPlan,
            )

        if not plan.operations:
            raise ChatGroundingError(code=ChatErrorCode.EVIDENCE_MISSING)
        _validate_plan_for_intent(decision.kind, document, plan)
        try:
            _validate_plan(document, source_blocks, plan)
        except ChatGroundingError:
            if decision.kind is IntentKind.TEMPLATE_ACTION:
                plan = self._faq_plan(
                    document,
                    source_blocks,
                    message,
                    retry_grounding=True,
                )
            else:
                plan = self._generate_with_schema_retry(
                    system=f"{CHAT_SYSTEM_PROMPT}\n\n{CHAT_GROUNDING_RETRY_PROMPT}",
                    user=_serialized_context(document, source_blocks, message),
                    schema=ChatEditPlan,
                )
            if not plan.operations:
                raise ChatGroundingError(code=ChatErrorCode.EVIDENCE_MISSING)
            _validate_plan_for_intent(decision.kind, document, plan)
            _validate_plan(document, source_blocks, plan)

        operations = _operations_for_application(document, plan, source_blocks)
        return self._apply_operations(
            project_id,
            request.expected_revision,
            operations,
            summary=plan.summary,
        )

    def _faq_plan(
        self,
        document: WorkingDocument,
        source_blocks: list[NormalizedBlock],
        message: str,
        *,
        retry_grounding: bool = False,
    ) -> ChatEditPlan:
        system = FAQ_SYSTEM_PROMPT
        if retry_grounding:
            system = f"{system}\n\n{CHAT_GROUNDING_RETRY_PROMPT}"
        draft = self._generate_with_schema_retry(
            system=system,
            user=_serialized_context(document, source_blocks, message),
            schema=FaqEntryDraft,
        )
        _validate_faq_draft_evidence(draft, source_blocks)
        item = FaqAdapter().compile(document, draft)
        return ChatEditPlan(
            summary="Добавлена подтверждённая пара вопрос–ответ",
            operations=[item],
        )

    def _generate_with_schema_retry(self, *, system: str, user: str, schema):
        try:
            return self._model.generate_json(system=system, user=user, schema=schema)
        except ModelResponseFormatError:
            try:
                return self._model.generate_json(
                    system=f"{system}\n\n{CHAT_RETRY_PROMPT}",
                    user=user,
                    schema=schema,
                )
            except ModelResponseFormatError as error:
                raise ChatError(ChatErrorCode.MODEL_INVALID_JSON) from error

    def _apply_operations(
        self,
        project_id: str,
        expected_revision: int,
        operations: list[DocumentOperation],
        *,
        summary: str,
    ) -> ChatEditResult:
        try:
            result = DocumentEditService(self._documents).apply(
                project_id,
                expected_revision,
                operations,
            )
        except EditConflict as error:
            raise ChatError(ChatErrorCode.REVISION_CONFLICT) from error
        except EditValidationError as error:
            raise ChatError(
                ChatErrorCode.INVALID_OPERATION,
                message=str(error),
            ) from error
        return ChatEditResult(
            summary=summary,
            document=result.document,
            revision=result.revision,
        )


def _as_source_snapshot(
    value: SourceSnapshot | list[NormalizedBlock],
) -> SourceSnapshot:
    if isinstance(value, SourceSnapshot):
        return value
    return SourceSnapshot(
        configured_source_count=1 if value else 0,
        blocks=value,
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


def _validate_plan_for_intent(
    intent: IntentKind,
    document: WorkingDocument,
    plan: ChatEditPlan,
) -> None:
    if intent is IntentKind.TEMPLATE_ACTION:
        if len(plan.operations) != 1 or not isinstance(
            plan.operations[0].operation, InsertNode
        ):
            raise ChatValidationError("FAQ-адаптер вернул недопустимую операцию")
        return
    if intent is not IntentKind.GROUNDED_EDIT:
        return
    for item in plan.operations:
        operation = item.operation
        if isinstance(operation, (DeleteNode, MoveNode)):
            raise ChatValidationError(
                "Фактологический запрос не может удалять или перемещать блоки"
            )
        if isinstance(operation, InsertNode) and operation.node.kind is NodeKind.HEADING:
            raise ChatValidationError(
                "Фактологический запрос не может менять структуру документа"
            )
        if not _factual_changed_tokens(document, operation):
            raise ChatValidationError(
                "Модель вернула операцию, не соответствующую фактологическому запросу"
            )


def _operations_for_application(
    document: WorkingDocument,
    plan: ChatEditPlan,
    source_blocks: list[NormalizedBlock],
) -> list[DocumentOperation]:
    evidence_by_id = {block.id: block for block in source_blocks}
    operations: list[DocumentOperation] = []
    provenance_by_node: dict[str, list[Provenance]] = {}
    for item in plan.operations:
        operation = item.operation
        provenance = _provenance_for_evidence(item.evidence_block_ids, evidence_by_id)
        if isinstance(operation, UpdateData):
            node = find_node(document, operation.node_id)
            if node is not None:
                merged_data = _merged_data(node.data, operation.data)
                if "items" in operation.data:
                    if "items_html" not in operation.data:
                        merged_data.pop("items_html", None)
                    if "item_styles" not in operation.data:
                        merged_data.pop("item_styles", None)
                operation = operation.model_copy(
                    update={"data": merged_data}
                )
        elif isinstance(operation, InsertNode) and provenance:
            operation = operation.model_copy(
                update={
                    "node": operation.node.model_copy(
                        update={"provenance": provenance}
                    )
                }
            )
        operations.append(operation)
        if provenance and isinstance(operation, (UpdateText, UpdateData)):
            node = find_node(document, operation.node_id)
            if node is not None:
                merged_provenance = _merge_provenance(
                    provenance_by_node.get(operation.node_id, node.provenance), provenance
                )
                provenance_by_node[operation.node_id] = merged_provenance
                operations.append(
                    UpdateProvenance(
                        node_id=operation.node_id,
                        provenance=merged_provenance,
                    )
                )
    return operations


def _provenance_for_evidence(
    evidence_block_ids: list[str], evidence_by_id: dict[str, NormalizedBlock]
) -> list[Provenance]:
    provenance = [
        item
        for block_id in evidence_block_ids
        for item in evidence_by_id[block_id].provenance
    ]
    return _merge_provenance([], provenance)


def _validate_faq_draft_evidence(
    draft: FaqEntryDraft,
    source_blocks: list[NormalizedBlock],
) -> None:
    evidence_by_id = {block.id: block for block in source_blocks}
    try:
        evidence = [evidence_by_id[block_id] for block_id in draft.evidence_block_ids]
    except KeyError as error:
        raise ChatGroundingError(
            "Ответ модели ссылается на неизвестный фрагмент источника",
            code=ChatErrorCode.EVIDENCE_MISSING,
        ) from error
    if not _tokens_supported(
        _tokens(draft.answer),
        _tokens(" ".join(block.text for block in evidence)),
    ):
        raise ChatGroundingError(code=ChatErrorCode.GROUNDING_FAILED)


def _merge_provenance(
    existing: list[Provenance], additions: list[Provenance]
) -> list[Provenance]:
    seen: set[tuple[str, str, str | None]] = set()
    result: list[Provenance] = []
    for item in [*existing, *additions]:
        key = (item.source_id, item.locator, item.quote)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _merged_data(existing: dict, update: dict) -> dict:
    merged = dict(existing)
    for key, value in update.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merged_data(current, value)
        else:
            merged[key] = value
    return merged


def _validate_operation_evidence(
    document: WorkingDocument,
    evidence_by_id: dict[str, NormalizedBlock],
    item: ChatEditOperation,
) -> None:
    if _is_non_factual_structural_operation(item.operation):
        return
    try:
        cited_blocks = [evidence_by_id[block_id] for block_id in item.evidence_block_ids]
    except KeyError as error:
        raise ChatGroundingError(
            "Ответ модели ссылается на неизвестный фрагмент источника",
            code=ChatErrorCode.EVIDENCE_MISSING,
        ) from error

    changed_tokens = _factual_changed_tokens(document, item.operation)
    if not changed_tokens:
        return
    if not cited_blocks:
        raise ChatGroundingError(code=ChatErrorCode.EVIDENCE_MISSING)
    if not _tokens_supported(
        changed_tokens,
        _tokens(" ".join(block.text for block in cited_blocks)),
        allow_grounded_synthesis=_allows_grounded_synthesis(document, item.operation),
    ):
        raise ChatGroundingError(code=ChatErrorCode.GROUNDING_FAILED)


def _is_non_factual_structural_operation(operation: DocumentOperation) -> bool:
    return isinstance(operation, MoveNode) or (
        isinstance(operation, InsertNode) and operation.node.kind is NodeKind.HEADING
    )


def _allows_grounded_synthesis(
    document: WorkingDocument,
    operation: DocumentOperation,
) -> bool:
    if isinstance(operation, InsertNode):
        return False
    if not isinstance(operation, UpdateData):
        return False
    node = find_node(document, operation.node_id)
    if node is None:
        return False
    return node.kind is NodeKind.LIST or any(
        key in operation.data for key in ("items", "items_html")
    )


_WORD_PATTERN = re.compile(r"[^\W\d_]+", re.UNICODE)
_NUMBER_BODY = r"[+\-−]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?"
_CURRENCY_UNIT = r"(?:[$€₽£¥₸]|rub|rur|usd|eur|kzt|gbp|jpy|руб(?:лей|ля|\.)?|р\.)"
_NUMERIC_LITERAL_PATTERN = re.compile(
    rf"""
    (?<!\d)
    (?P<prefix>[$€₽£¥₸])?\s*
    (?P<first>{_NUMBER_BODY})
    (?:\s*(?P<range>\.\.|[-–—])\s*(?P<second>{_NUMBER_BODY}))?
    \s*(?P<unit>%|{_CURRENCY_UNIT}(?!\w))?
    (?!\d)
    """,
    re.IGNORECASE | re.UNICODE | re.VERBOSE,
)
_NUMERIC_TOKEN_PREFIX = "num:"
_CURRENCY_ALIASES = {
    "$": "usd",
    "€": "eur",
    "₽": "rub",
    "£": "gbp",
    "¥": "jpy",
    "₸": "kzt",
    "rur": "rub",
    "руб": "rub",
    "руб.": "rub",
    "рубля": "rub",
    "рублей": "rub",
    "р.": "rub",
}
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
    numeric: list[str] = []
    word_text = list(normalized)
    for match in _NUMERIC_LITERAL_PATTERN.finditer(normalized):
        numeric.append(_normalized_numeric_literal(match))
        word_text[match.start() : match.end()] = " " * (match.end() - match.start())
    words = [
        token
        for token in _WORD_PATTERN.findall("".join(word_text))
        if len(token) >= 2 and token not in _STOP_WORDS
    ]
    return [*numeric, *words]


def _normalized_numeric_literal(match: re.Match[str]) -> str:
    first = _normalized_number(match.group("first"))
    second = match.group("second")
    value = first if second is None else f"{first}..{_normalized_number(second)}"
    units = [
        _normalized_numeric_unit(unit)
        for unit in (match.group("prefix"), match.group("unit"))
        if unit
    ]
    unique_units = list(dict.fromkeys(units))
    suffix = f":{':'.join(unique_units)}" if unique_units else ""
    return f"{_NUMERIC_TOKEN_PREFIX}{value}{suffix}"


def _normalized_number(value: str) -> str:
    return (
        value.replace("−", "-")
        .replace(" ", "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(",", ".")
    )


def _normalized_numeric_unit(value: str) -> str:
    normalized = value.casefold()
    return _CURRENCY_ALIASES.get(normalized, normalized)


def _tokens_supported(
    changed: list[str],
    evidence: list[str],
    *,
    allow_grounded_synthesis: bool = False,
) -> bool:
    # MVP policy: every complete numeric fact must occur in the cited operation-level
    # evidence with the same multiplicity. Remaining words use conservative lexical
    # support so grounded Russian inflections/paraphrases are not forced to be exact.
    numeric = Counter(token for token in changed if _is_numeric_token(token))
    evidence_numbers = Counter(token for token in evidence if _is_numeric_token(token))
    if numeric - evidence_numbers:
        return False

    if allow_grounded_synthesis:
        return bool(changed)

    words = [token for token in changed if not _is_numeric_token(token)]
    evidence_words = [token for token in evidence if not _is_numeric_token(token)]
    if not words:
        return True
    matched = sum(
        1
        for token in words
        if any(_tokens_match(token, evidence_token) for evidence_token in evidence_words)
    )
    required = 1 if len(words) == 1 else max(2, math.ceil(len(words) * 0.6))
    return matched >= required


def _tokens_match(left: str, right: str) -> bool:
    if left == right:
        return True
    if _is_numeric_token(left) or _is_numeric_token(right):
        return False
    common = 0
    for left_character, right_character in zip(left, right, strict=False):
        if left_character != right_character:
            break
        common += 1
    return common >= 5 and common / max(len(left), len(right)) >= 0.6


def _is_numeric_token(token: str) -> bool:
    return token.startswith(_NUMERIC_TOKEN_PREFIX)


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
