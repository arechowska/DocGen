from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from docgen.ai.client import ModelResponseFormatError, TextModel
from docgen.chat.adapters import FaqAdapter
from docgen.chat.errors import ChatError, ChatErrorCode
from docgen.chat.executors import (
    authored_operations,
    formatting_operations,
    structural_operations,
)
from docgen.chat.intents import (
    IntentDecision,
    IntentKind,
    StructureAction,
    _retrieval_query,
    route_intent,
)
from docgen.chat.manual_insert import (
    ManualInsertError,
)
from docgen.chat.retrieval import RetrievalResult, SourceSnapshot, retrieve_relevant_blocks
from docgen.chat.schemas import (
    ChatEditOperation,
    ChatEditPlan,
    ChatEditRequest,
    ChatEditResult,
    FaqEntryDraft,
    FindingFixProposal,
    GlossaryDraft,
    GlossaryEntryDraft,
    SemanticIntentDraft,
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
    apply_operations,
    find_node,
)
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import CheckFinding, DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import NormalizedBlock, Provenance
from docgen.templates_catalog.schemas import SemanticTemplate

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
Предыдущий ответ не принят: верни только один валидный JSON-объект точно по
переданной JSON-схеме {schema_name}.
Не пиши пояснений, Markdown, префиксов и текста вне JSON. Используй пустой operations,
только если это поле есть в переданной схеме.
""".strip()

CHAT_GROUNDING_RETRY_PROMPT = """
Предыдущий план не прошёл проверку источников. Исправь план: каждая фактическая операция должна ссылаться в evidence_block_ids на существующие source_blocks, подтверждающие добавляемый или изменяемый текст. Не используй идентификаторы наугад. Если подтверждения нет, исключи такую операцию.
""".strip()

FILL_EMPTY_SYSTEM_PROMPT = """
Заполняйте только пустые ячейки существующих таблиц. Не изменяйте заголовки,
число строк и столбцов, непустые значения или другие блоки документа. Для каждой
таблицы возвращайте полный массив rows с неизменёнными существующими значениями.
Если источник не подтверждает значение поля, оставьте эту ячейку пустой.
""".strip()

FAQ_SYSTEM_PROMPT = """
Ты создаёшь одну подтверждённую FAQ-запись для DocGen-документа.
Верни только объект FaqEntryDraft: непустые question и answer, placement и
evidence_block_ids. Ответ использует только факты из переданных source_blocks.
Не помещай evidence_block_ids, placement и другие служебные поля в question или answer.
Поле placement.parent_id всегда null: FAQ-запись добавляется только на верхний
уровень документа, а не внутрь другого блока.
Поле placement.index — целое число, равное количеству блоков верхнего уровня в
переданном document.nodes (то есть запись добавляется в конец документа).
""".strip()

GLOSSARY_SYSTEM_PROMPT = """
Выделите термины и краткие определения только из переданного текущего документа.
Верните GlossaryDraft с непустым entries. Каждый entry содержит term, definition и
evidence_node_ids — идентификаторы узлов документа, где термин действительно
использован. Если не уверены в идентификаторах, верните пустой evidence_node_ids: сервис найдёт и
проверит узлы сам. Не включайте общеупотребительные слова, заголовки формы, служебные поля,
идентификаторы узлов и термины, уже присутствующие в разделе «Термины и определения».
Не придумывайте значения, числовые факты и расшифровки сокращений, которых нет в
указанных узлах. Никакого Markdown и текста вне JSON.
""".strip()

SEMANTIC_INTENT_SYSTEM_PROMPT = """
Определи намерение пользователя по смыслу фразы, а не по ключевым словам.
Верни SemanticIntentDraft с одним intent:
- grounded_edit: изменить или добавить факты по внешнему источнику;
- fill_empty_fields: заполнить только пустые ячейки таблиц по внешнему источнику;
- document_glossary: извлечь термины и определения из текущего документа;
- structure: переместить, удалить, объединить, разделить или сгруппировать имеющиеся блоки;
- clarification: цель или объект правки неоднозначны.
Для правки по источнику заполни retrieval_query темой искомых фактов.
Если назван конкретный источник, точно скопируй его имя в source_name.
Для structure заполни structure_action, target_ordinals и relation.
Если данных не хватает для безопасного выполнения, выбери clarification и задай один вопрос.
Не предлагай пользователю самому редактировать документ.
Ответ — только JSON-объект с полями intent, retrieval_query, source_name,
clarification, structure_action, target_ordinals и relation. Неприменимые поля можно опустить.
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

        if decision.kind is IntentKind.SEMANTIC:
            semantic = self._semantic_intent(document, message)
            if semantic.intent == "clarification":
                raise ChatError(
                    ChatErrorCode.CLARIFICATION,
                    message=semantic.clarification
                    or "Уточни, что именно изменить",
                    action="Назови целевой блок, факт или источник.",
                )
            if semantic.intent == "document_glossary":
                return self._document_glossary(
                    project_id, request.expected_revision, document, message
                )
            if semantic.intent == "structure":
                if semantic.structure_action is None:
                    raise ChatError(
                        ChatErrorCode.CLARIFICATION,
                        message="Не удалось определить структурную правку",
                        action="Уточни действие и номера блоков.",
                    )
                decision = IntentDecision(
                    kind=IntentKind.STRUCTURE,
                    structure_action=StructureAction(semantic.structure_action),
                    target_ordinals=semantic.target_ordinals,
                    relation=semantic.relation,
                )
            else:
                return self._model_grounded_edit(
                    project_id,
                    request.expected_revision,
                    document,
                    message,
                    semantic.retrieval_query or _retrieval_query(message),
                    IntentKind.GROUNDED_EDIT,
                    fill_empty=semantic.intent == "fill_empty_fields",
                    source_name=semantic.source_name,
                )

        if decision.kind is IntentKind.CLARIFICATION:
            raise ChatError(
                ChatErrorCode.CLARIFICATION,
                message=decision.clarification,
                action="Сформулируй правку точнее и повтори запрос.",
            )
        if decision.kind is IntentKind.DOCUMENT_GLOSSARY:
            return self._document_glossary(
                project_id,
                request.expected_revision,
                document,
                message,
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

        return self._model_grounded_edit(
            project_id,
            request.expected_revision,
            document,
            message,
            decision.retrieval_query,
            decision.kind,
        )

    def _semantic_intent(
        self,
        document: WorkingDocument,
        message: str,
    ) -> SemanticIntentDraft:
        outline = [
            {
                "ordinal": index,
                "id": node.id,
                "kind": node.kind.value,
                "section_id": node.section_id,
                "text": (node.text or "")[:160],
            }
            for index, node in enumerate(document.nodes, start=1)
        ]
        return self._generate_with_schema_retry(
            system=SEMANTIC_INTENT_SYSTEM_PROMPT,
            user=json.dumps(
                {
                    "message": message,
                    "document": {
                        "template_id": document.template_id,
                        "build_template_id": document.build_template_id,
                        "nodes": outline,
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            schema=SemanticIntentDraft,
        )

    def _document_glossary(
        self,
        project_id: str,
        expected_revision: int,
        document: WorkingDocument,
        message: str,
    ) -> ChatEditResult:
        target = _terms_section(document)
        if target is None:
            raise ChatError(
                ChatErrorCode.CLARIFICATION,
                message="Раздел «Термины и определения» не найден",
                action="Добавь форму выбранного шаблона и повтори запрос.",
            )
        evidence_nodes = _glossary_evidence_nodes(document, target)
        if not evidence_nodes:
            raise ChatError(
                ChatErrorCode.CLARIFICATION,
                message="В документе нет содержания для выделения терминов",
                action="Добавь содержательные разделы и повтори запрос.",
            )
        user = json.dumps(
            {
                "task": message,
                "target_heading_id": target.id,
                "existing_items": _existing_glossary_items(document, target),
                "document_nodes": [
                    {
                        "id": node.id,
                        "kind": node.kind.value,
                        "text": node.text,
                        "data": node.data,
                    }
                    for node in evidence_nodes
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        draft = self._generate_with_schema_retry(
            system=GLOSSARY_SYSTEM_PROMPT,
            user=user,
            schema=GlossaryDraft,
        )
        entries = _validated_glossary_entries(
            draft,
            evidence_nodes,
            _existing_glossary_items(document, target),
        )
        operations = _glossary_operations(document, target, entries, evidence_nodes)
        return self._apply_operations(
            project_id,
            expected_revision,
            operations,
            summary=f"Добавлено терминов: {len(entries)}",
        )

    def apply_finding_fix(
        self,
        project_id: str,
        finding: CheckFinding,
        expected_revision: int,
        *,
        template: SemanticTemplate | None = None,
    ) -> ChatEditResult:
        """Apply a check report finding's suggested fix.

        A structure-check finding (a missing corporate-form heading or
        table) is resolved deterministically -- it names a structural fact
        about the document, not a claim needing source evidence, and the
        grounded-edit validator rejects heading/table inserts outright.
        Every other finding goes through the same grounded-edit pipeline as
        a regular chat message, so a suggestion can never be applied
        without the same evidence a human-typed request would need.
        """
        if not finding.suggestion:
            raise ChatValidationError("Для этой проблемы нет предложенного исправления")
        stored = self._documents.get_document_with_revision(project_id)
        if stored is None:
            raise ChatValidationError("Документ не найден")
        document, current_revision = stored
        if current_revision != expected_revision:
            raise ChatError(ChatErrorCode.REVISION_CONFLICT)

        if (
            template is not None
            and template.structure_check is not None
            and finding.rule_id == template.structure_check.rule_id
        ):
            raise ChatValidationError(
                "Полную форму нельзя безопасно добавить как точечную правку"
            )

        message = " ".join(
            part for part in (finding.message, finding.suggestion) if part
        )
        retrieval_text = " ".join(
            part
            for part in (finding.message, finding.evidence, finding.suggestion)
            if part
        )
        return self._model_grounded_edit(
            project_id,
            expected_revision,
            document,
            message,
            _retrieval_query(retrieval_text),
            IntentKind.GROUNDED_EDIT,
        )

    def propose_finding_fix(
        self,
        project_id: str,
        finding: CheckFinding,
        expected_revision: int,
        *,
        template: SemanticTemplate | None = None,
    ) -> FindingFixProposal:
        """Build a source-grounded, target-scoped proposal without saving it."""
        if not finding.suggestion or not finding.rule_id:
            raise ChatValidationError(
                "Для этого расхождения нельзя подготовить безопасную точечную правку"
            )
        stored = self._documents.get_document_with_revision(project_id)
        if stored is None:
            raise ChatValidationError("Документ не найден")
        document, current_revision = stored
        if current_revision != expected_revision:
            raise ChatError(ChatErrorCode.REVISION_CONFLICT)
        if finding.code == "template-structure-mismatch":
            if template is None:
                raise ChatValidationError("Шаблон проверки не найден")
            from docgen.workflows.check import structure_gap_operations

            operations = structure_gap_operations(document, template)
            _validate_structural_fix_scope(document, operations)
            try:
                candidate = apply_operations(document, operations)
            except EditValidationError as error:
                raise ChatValidationError(str(error)) from error
            return FindingFixProposal(
                summary=(
                    "Восстановлены отсутствующие элементы формы и порядок полей "
                    "без изменения заполненных значений"
                ),
                operations=operations,
                document=candidate,
            )
        if not finding.node_id:
            raise ChatValidationError("У расхождения нет точного целевого блока")
        if find_node(document, finding.node_id) is None:
            raise ChatValidationError("Узел расхождения не найден в документе")

        message = " ".join(
            part for part in (finding.message, finding.suggestion) if part
        )
        retrieval_text = " ".join(
            part
            for part in (finding.message, finding.evidence, finding.suggestion)
            if part
        )
        plan, source_blocks = self._grounded_plan(
            project_id,
            document,
            message,
            _retrieval_query(retrieval_text),
            IntentKind.GROUNDED_EDIT,
        )
        operations = _operations_for_application(document, plan, source_blocks)
        _validate_finding_fix_scope(finding, operations)
        try:
            candidate = apply_operations(document, operations)
        except EditValidationError as error:
            raise ChatValidationError(str(error)) from error
        return FindingFixProposal(
            summary=plan.summary,
            operations=operations,
            document=candidate,
        )

    def _model_grounded_edit(
        self,
        project_id: str,
        expected_revision: int,
        document: WorkingDocument,
        message: str,
        retrieval_query: str,
        intent_kind: IntentKind,
        *,
        fill_empty: bool = False,
        source_name: str | None = None,
    ) -> ChatEditResult:
        plan, source_blocks = self._grounded_plan(
            project_id,
            document,
            message,
            retrieval_query,
            intent_kind,
            fill_empty=fill_empty,
            source_name=source_name,
        )
        operations = _operations_for_application(document, plan, source_blocks)
        return self._apply_operations(
            project_id,
            expected_revision,
            operations,
            summary=plan.summary,
        )

    def _grounded_plan(
        self,
        project_id: str,
        document: WorkingDocument,
        message: str,
        retrieval_query: str,
        intent_kind: IntentKind,
        *,
        fill_empty: bool = False,
        source_name: str | None = None,
    ) -> tuple[ChatEditPlan, list[NormalizedBlock]]:
        snapshot = _as_source_snapshot(self._source_blocks(project_id))
        snapshot, explicitly_scoped = _scope_snapshot_to_named_source(
            snapshot, source_name
        )
        if fill_empty:
            empty_query = _empty_table_field_query(document)
            if empty_query:
                retrieval_query = empty_query
        try:
            retrieved = retrieve_relevant_blocks(
                snapshot,
                retrieval_query,
                limit=24 if fill_empty else 12,
            )
        except ChatError as error:
            if (
                not fill_empty
                or not explicitly_scoped
                or error.code is not ChatErrorCode.RELEVANT_FRAGMENT_MISSING
            ):
                raise
            # The source is explicit, but its prose may not repeat the form
            # labels. Keep the fallback bounded; grounding still rejects any
            # value that is absent from these source blocks.
            retrieved = RetrievalResult(
                blocks=list(snapshot.blocks)[:24],
                total_blocks=len(snapshot.blocks),
            )
        source_blocks = retrieved.blocks

        if intent_kind is IntentKind.TEMPLATE_ACTION:
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
                system=(
                    f"{CHAT_SYSTEM_PROMPT}\n\n{FILL_EMPTY_SYSTEM_PROMPT}"
                    if fill_empty
                    else CHAT_SYSTEM_PROMPT
                ),
                user=_serialized_context(document, source_blocks, message),
                schema=ChatEditPlan,
            )

        if not plan.operations:
            raise ChatGroundingError(code=ChatErrorCode.EVIDENCE_MISSING)
        _validate_plan_for_intent(intent_kind, document, plan)
        try:
            _validate_plan(document, source_blocks, plan)
            if fill_empty:
                _validate_fill_empty_plan(document, plan)
        except ChatGroundingError:
            if intent_kind is IntentKind.TEMPLATE_ACTION:
                plan = self._faq_plan(
                    document,
                    source_blocks,
                    message,
                    retry_grounding=True,
                )
            else:
                plan = self._generate_with_schema_retry(
                    system=(
                        f"{CHAT_SYSTEM_PROMPT}\n\n{FILL_EMPTY_SYSTEM_PROMPT}\n\n"
                        f"{CHAT_GROUNDING_RETRY_PROMPT}"
                        if fill_empty
                        else f"{CHAT_SYSTEM_PROMPT}\n\n{CHAT_GROUNDING_RETRY_PROMPT}"
                    ),
                    user=_serialized_context(document, source_blocks, message),
                    schema=ChatEditPlan,
                )
            if not plan.operations:
                raise ChatGroundingError(code=ChatErrorCode.EVIDENCE_MISSING)
            _validate_plan_for_intent(intent_kind, document, plan)
            _validate_plan(document, source_blocks, plan)
            if fill_empty:
                _validate_fill_empty_plan(document, plan)
        return plan, source_blocks

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
                schema_json = json.dumps(
                    schema.model_json_schema(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                return self._model.generate_json(
                    system=(
                        f"{system}\n\n"
                        f"{CHAT_RETRY_PROMPT.format(schema_name=schema.__name__)}\n"
                        f"JSON Schema: {schema_json}"
                    ),
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


def _terms_section(document: WorkingDocument) -> DocumentNode | None:
    return next(
        (
            node
            for node in _document_nodes(document.nodes)
            if node.kind is NodeKind.HEADING
            and (
                node.section_id == "terms"
                or _normalized_section_title(node.text) == "термины и определения"
            )
        ),
        None,
    )


def _normalized_section_title(value: str | None) -> str:
    if not value:
        return ""
    normalized = " ".join(value.casefold().replace("ё", "е").split())
    return re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", normalized)


def _glossary_evidence_nodes(
    document: WorkingDocument,
    target: DocumentNode,
) -> list[DocumentNode]:
    excluded_ids = {node.id for node in _document_nodes([target])}
    return [
        node
        for node in _document_nodes(document.nodes)
        if node.id not in excluded_ids
        and node.kind not in {NodeKind.GAP, NodeKind.IMAGE}
        and _node_factual_text(node).strip()
    ]


def _existing_glossary_list(
    document: WorkingDocument,
    target: DocumentNode,
) -> DocumentNode | None:
    child = next(
        (node for node in target.children if node.kind is NodeKind.LIST),
        None,
    )
    if child is not None:
        return child
    if target not in document.nodes:
        return None
    index = document.nodes.index(target)
    if index + 1 >= len(document.nodes):
        return None
    candidate = document.nodes[index + 1]
    return candidate if candidate.kind is NodeKind.LIST else None


def _existing_glossary_items(
    document: WorkingDocument,
    target: DocumentNode,
) -> list[str]:
    glossary = _existing_glossary_list(document, target)
    if glossary is None:
        return []
    items = glossary.data.get("items", [])
    return [str(item) for item in items] if isinstance(items, list) else []


def _validated_glossary_entries(
    draft: GlossaryDraft,
    evidence_nodes: list[DocumentNode],
    existing_items: list[str],
) -> list[GlossaryEntryDraft]:
    existing_terms = {
        _normalized_glossary_term(item.split("—", 1)[0])
        for item in existing_items
    }
    seen = set(existing_terms)
    result: list[GlossaryEntryDraft] = []
    for entry in draft.entries:
        normalized_term = _normalized_glossary_term(entry.term)
        if not normalized_term or normalized_term in seen:
            continue
        # Never trust model-selected node IDs for document-derived content.
        # Resolve evidence deterministically across the current document so
        # one bad citation cannot reject other valid glossary entries.
        cited = [
            node
            for node in evidence_nodes
            if _tokens_supported(
                _tokens(entry.term),
                _tokens(_node_factual_text(node)),
            )
        ]
        evidence_text = " ".join(_node_factual_text(node) for node in cited)
        if not _tokens_supported(_tokens(entry.term), _tokens(evidence_text)):
            continue
        if not _tokens_supported(
            _tokens(entry.definition),
            _tokens(evidence_text),
            allow_grounded_synthesis=True,
        ):
            continue
        entry = entry.model_copy(
            update={"evidence_node_ids": [node.id for node in cited]}
        )
        seen.add(normalized_term)
        result.append(entry)
    if not result:
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message="Новых подтверждённых терминов в документе не найдено",
            action="Уточни нужные термины или дополни содержательные разделы.",
        )
    return result


def _normalized_glossary_term(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split()).strip(" —–-")


def _glossary_operations(
    document: WorkingDocument,
    target: DocumentNode,
    entries: list[GlossaryEntryDraft],
    evidence_nodes: list[DocumentNode],
) -> list[DocumentOperation]:
    evidence_by_id = {node.id: node for node in evidence_nodes}
    items = [f"{entry.term} — {entry.definition}" for entry in entries]
    provenance = _merge_provenance(
        [],
        [
            item
            for entry in entries
            for node_id in entry.evidence_node_ids
            for item in evidence_by_id[node_id].provenance
        ],
    )
    existing = _existing_glossary_list(document, target)
    if existing is not None:
        data = dict(existing.data)
        current_items = data.get("items", [])
        if not isinstance(current_items, list):
            current_items = []
        data["items"] = [*current_items, *items]
        data.pop("items_html", None)
        data.pop("item_styles", None)
        operations: list[DocumentOperation] = [
            UpdateData(node_id=existing.id, data=data)
        ]
        if provenance:
            operations.append(
                UpdateProvenance(
                    node_id=existing.id,
                    provenance=_merge_provenance(existing.provenance, provenance),
                )
            )
        return operations

    node = DocumentNode(
        id=f"glossary-{uuid4()}",
        kind=NodeKind.LIST,
        data={"items": items},
        provenance=provenance,
        flags=["document-derived", "glossary"],
    )
    if target in document.nodes:
        target_index = document.nodes.index(target)
        operations = []
        if target_index + 1 < len(document.nodes):
            placeholder = document.nodes[target_index + 1]
            if placeholder.kind is NodeKind.GAP or (
                placeholder.kind is NodeKind.PARAGRAPH
                and not (placeholder.text or "").strip()
            ):
                operations.append(DeleteNode(node_id=placeholder.id))
        operations.append(InsertNode(index=target_index + 1, node=node))
        return operations

    gap_children = [child for child in target.children if child.kind is NodeKind.GAP]
    operations = [DeleteNode(node_id=child.id) for child in gap_children]
    operations.append(
        InsertNode(
            parent_id=target.id,
            index=len(target.children) - len(gap_children),
            node=node,
        )
    )
    return operations


def _scope_snapshot_to_named_source(
    snapshot: SourceSnapshot,
    source_name: str | None,
) -> tuple[SourceSnapshot, bool]:
    if source_name is None:
        return snapshot, False
    normalized_name = unicodedata.normalize("NFKC", source_name).casefold().strip()
    matches = [
        source
        for source in snapshot.sources
        if source.display_name.strip()
        and unicodedata.normalize("NFKC", source.display_name).casefold().strip()
        == normalized_name
    ]
    if not matches:
        if snapshot.sources:
            raise ChatError(
                ChatErrorCode.CLARIFICATION,
                message=f"Источник «{source_name}» не найден в проекте",
                action="Укажи точное название источника из левой панели.",
            )
        return snapshot, False
    longest = max(len(source.display_name) for source in matches)
    selected = [source for source in matches if len(source.display_name) == longest]
    if len(selected) != 1:
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message="В проекте несколько источников с таким названием",
            action="Укажи источник точнее.",
        )
    source = selected[0]
    blocks = [
        block
        for block in snapshot.blocks
        if any(item.source_id == source.id for item in block.provenance)
    ]
    return (
        SourceSnapshot(
            configured_source_count=1,
            blocks=blocks,
            warnings=snapshot.warnings,
            identity=snapshot.identity,
            sources=(source,),
        ),
        True,
    )


def _empty_table_field_query(document: WorkingDocument) -> str:
    labels: list[str] = []
    for node in _document_nodes(document.nodes):
        if node.kind is not NodeKind.TABLE:
            continue
        rows = node.data.get("rows", [])
        headers = node.data.get("headers", [])
        if isinstance(rows, list):
            for row in rows:
                if (
                    isinstance(row, list)
                    and len(row) >= 2
                    and not _is_blank_cell(row[0])
                    and all(_is_blank_cell(value) for value in row[1:])
                ):
                    labels.append(str(row[0]))
        if isinstance(headers, list) and isinstance(rows, list):
            for index, header in enumerate(headers):
                values = [
                    row[index]
                    for row in rows
                    if isinstance(row, list) and index < len(row)
                ]
                if not _is_blank_cell(header) and values and all(
                    _is_blank_cell(value) for value in values
                ):
                    labels.append(str(header))
    return " ".join(dict.fromkeys(labels))


def _document_nodes(nodes: list[DocumentNode]) -> list[DocumentNode]:
    result: list[DocumentNode] = []
    for node in nodes:
        result.append(node)
        result.extend(_document_nodes(node.children))
    return result


def _validate_fill_empty_plan(
    document: WorkingDocument,
    plan: ChatEditPlan,
) -> None:
    for item in plan.operations:
        operation = item.operation
        if not isinstance(operation, UpdateData):
            raise ChatValidationError(
                "Заполнение пустых полей может изменять только существующие таблицы"
            )
        node = find_node(document, operation.node_id)
        if node is None or node.kind is not NodeKind.TABLE:
            raise ChatValidationError("Целевая таблица для заполнения не найдена")
        after = _merged_data(node.data, operation.data)
        _validate_only_blank_table_cells_changed(node.data, after)


def _validate_only_blank_table_cells_changed(before: dict, after: dict) -> None:
    if {key: value for key, value in before.items() if key != "rows"} != {
        key: value for key, value in after.items() if key != "rows"
    }:
        raise ChatValidationError(
            "Заполнение пустых полей не может менять структуру таблицы"
        )
    before_rows = before.get("rows", [])
    after_rows = after.get("rows", [])
    if (
        not isinstance(before_rows, list)
        or not isinstance(after_rows, list)
        or len(before_rows) != len(after_rows)
    ):
        raise ChatValidationError(
            "Заполнение пустых полей не может менять строки таблицы"
        )
    changed = False
    for before_row, after_row in zip(before_rows, after_rows, strict=True):
        if (
            not isinstance(before_row, list)
            or not isinstance(after_row, list)
            or len(before_row) != len(after_row)
        ):
            raise ChatValidationError(
                "Заполнение пустых полей не может менять столбцы таблицы"
            )
        for before_value, after_value in zip(before_row, after_row, strict=True):
            if not _is_blank_cell(before_value) and before_value != after_value:
                raise ChatValidationError(
                    "Заполнение пустых полей пытается изменить заполненную ячейку"
                )
            if before_value != after_value:
                changed = True
    if not changed:
        raise ChatValidationError("В предложении нет заполнения пустых полей")


def _is_blank_cell(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


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


def validate_finding_fix_scope(
    finding: CheckFinding,
    operations: list[DocumentOperation],
    *,
    document: WorkingDocument | None = None,
    template: SemanticTemplate | None = None,
) -> None:
    if finding.code == "template-structure-mismatch":
        if document is None:
            raise ChatValidationError("Документ для проверки правки не найден")
        if template is None:
            raise ChatValidationError("Шаблон проверки не найден")
        from docgen.workflows.check import structure_gap_operations

        if operations != structure_gap_operations(document, template):
            raise ChatValidationError(
                "Предложение больше не совпадает с отсутствующими полями шаблона"
            )
        _validate_structural_fix_scope(document, operations)
        return
    _validate_finding_fix_scope(finding, operations)


def _validate_structural_fix_scope(
    document: WorkingDocument,
    operations: list[DocumentOperation],
) -> None:
    if not operations:
        raise ChatValidationError(
            "Не удалось однозначно определить существующую таблицу для дополнения"
        )
    for operation in operations:
        if isinstance(operation, InsertNode):
            flags = set(operation.node.flags)
            valid_heading = (
                operation.node.kind is NodeKind.HEADING
                and bool((operation.node.text or "").strip())
                and flags == {"structural-edit", "empty-template-section"}
            )
            valid_table = (
                operation.node.kind is NodeKind.TABLE
                and not operation.node.text
                and "structural-edit" in flags
                and "empty-template-table" in flags
                and len(
                    [flag for flag in flags if flag.startswith("template-table:")]
                )
                == 1
            )
            if operation.parent_id is not None or not (valid_heading or valid_table):
                raise ChatValidationError(
                    "Структурная правка пытается добавить неразрешённый блок"
                )
            continue
        if not isinstance(operation, UpdateData):
            raise ChatValidationError(
                "Структурная правка может дополнить таблицу или добавить пустой раздел"
            )
        node = find_node(document, operation.node_id)
        if node is None or node.kind is not NodeKind.TABLE:
            raise ChatValidationError("Целевая таблица не найдена")
        _validate_table_additions(node.data, operation.data)


def _validate_table_additions(before: dict, after: dict) -> None:
    if {key: value for key, value in before.items() if key not in {"rows", "headers"}} != {
        key: value for key, value in after.items() if key not in {"rows", "headers"}
    }:
        raise ChatValidationError("Правка пытается изменить оформление таблицы")
    before_values = Counter(_table_values(before))
    after_values = Counter(_table_values(after))
    if any(after_values[value] < count for value, count in before_values.items()):
        raise ChatValidationError("Правка пытается изменить заполненные ячейки")
    if before == after:
        raise ChatValidationError("Правка не добавляет отсутствующие поля")


def _table_values(data: dict) -> list[str]:
    values: list[str] = []
    headers = data.get("headers", [])
    rows = data.get("rows", [])
    if not isinstance(headers, list) or not isinstance(rows, list):
        raise ChatValidationError("Некорректная структура таблицы")
    values.extend(str(value) for value in headers)
    for row in rows:
        if not isinstance(row, list):
            raise ChatValidationError("Некорректная строка таблицы")
        values.extend(str(value) for value in row)
    return values


def _validate_finding_fix_scope(
    finding: CheckFinding,
    operations: list[DocumentOperation],
) -> None:
    if not finding.node_id:
        raise ChatValidationError("У расхождения нет точного целевого блока")
    content_changes = 0
    for operation in operations:
        if not isinstance(operation, (UpdateText, UpdateData, UpdateProvenance)):
            raise ChatValidationError(
                "Предложение пытается изменить структуру документа"
            )
        if operation.node_id != finding.node_id:
            raise ChatValidationError(
                "Предложение не относится к узлу выбранного расхождения"
            )
        if isinstance(operation, (UpdateText, UpdateData)):
            content_changes += 1
    if content_changes != 1:
        raise ChatValidationError(
            "Предложение должно содержать одну точечную правку выбранного узла"
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
