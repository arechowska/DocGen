from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any
from uuid import uuid4

from docgen.chat.errors import ChatError, ChatErrorCode
from docgen.chat.intents import AuthoredReplacement, IntentDecision, StructureAction
from docgen.chat.manual_insert import manual_insert_operations
from docgen.documents.operations import (
    DeleteNode,
    DocumentOperation,
    InsertNode,
    MoveNode,
    UpdateData,
    UpdateFlags,
    UpdateText,
    UpdateTitle,
)
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument


def authored_operations(
    document: WorkingDocument,
    decision: IntentDecision,
) -> list[DocumentOperation]:
    if decision.manual_insert is not None:
        return manual_insert_operations(document, decision.manual_insert)
    if decision.replacement is not None:
        return _replacement_operations(document, decision.replacement)
    raise ChatError(ChatErrorCode.INVALID_OPERATION)


def formatting_operations(
    document: WorkingDocument,
    message: str,
) -> list[DocumentOperation]:
    style = _style_from_message(message)
    target = _selected_visible_node(document, message)
    if not style:
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message="Не удалось определить нужное оформление",
            action="Укажи шрифт, цвет, выравнивание или отступ.",
        )
    if target is None:
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message="Не удалось определить блок для форматирования",
            action="Укажи номер абзаца или блока.",
        )
    data = _merge_data(target.data, {"style": style})
    return [UpdateData(node_id=target.id, data=data)]


def structural_operations(
    document: WorkingDocument,
    decision: IntentDecision,
) -> list[DocumentOperation]:
    if decision.structure_action is StructureAction.SECTIONIZE:
        return _sectionize(document)
    if decision.structure_action is StructureAction.DELETE:
        target = _root_node(document, decision.target_ordinals, 0)
        return [DeleteNode(node_id=target.id)]
    if decision.structure_action is StructureAction.MOVE:
        return _move_operations(document, decision)
    if decision.structure_action is StructureAction.MERGE:
        return _merge_operations(document, decision)
    if decision.structure_action is StructureAction.SPLIT:
        return _split_operations(document, decision)
    raise ChatError(
        ChatErrorCode.CLARIFICATION,
        message="Для структурной правки не хватает точной позиции блока",
        action=(
            "Укажи номера блоков и позицию, например: «перемести третий блок перед первым»."
        ),
    )


def _move_operations(
    document: WorkingDocument,
    decision: IntentDecision,
) -> list[DocumentOperation]:
    moving = _root_node(document, decision.target_ordinals, 0)
    destination = _root_node(document, decision.target_ordinals, 1)
    if decision.relation not in {"before", "after"}:
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message="Не указано, куда переместить блок",
            action="Укажи позицию: перед или после другого блока.",
        )
    root = list(document.nodes)
    moving_index = root.index(moving)
    destination_index = root.index(destination)
    if moving_index < destination_index:
        destination_index -= 1
    index = destination_index + (1 if decision.relation == "after" else 0)
    return [MoveNode(node_id=moving.id, index=index)]


def _merge_operations(
    document: WorkingDocument,
    decision: IntentDecision,
) -> list[DocumentOperation]:
    left = _root_node(document, decision.target_ordinals, 0)
    right = _root_node(document, decision.target_ordinals, 1)
    if left.kind is not right.kind:
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message="Выбранные блоки имеют разные типы",
            action="Выбери два текстовых блока или два списка.",
        )
    flags = list(dict.fromkeys([*left.flags, "structural-edit"]))
    if left.kind in {NodeKind.PARAGRAPH, NodeKind.HEADING}:
        if not (left.text and right.text):
            raise ChatError(ChatErrorCode.CLARIFICATION)
        return [
            UpdateText(node_id=left.id, text=f"{left.text}\n{right.text}"),
            UpdateFlags(node_id=left.id, flags=flags),
            DeleteNode(node_id=right.id),
        ]
    if left.kind is NodeKind.LIST:
        left_items = left.data.get("items")
        right_items = right.data.get("items")
        if not isinstance(left_items, list) or not isinstance(right_items, list):
            raise ChatError(ChatErrorCode.CLARIFICATION)
        data = dict(left.data)
        data["items"] = [*left_items, *right_items]
        data.pop("items_html", None)
        data.pop("item_styles", None)
        return [
            UpdateData(node_id=left.id, data=data),
            UpdateFlags(node_id=left.id, flags=flags),
            DeleteNode(node_id=right.id),
        ]
    raise ChatError(
        ChatErrorCode.CLARIFICATION,
        message="Этот тип блоков нельзя безопасно объединить",
        action="Выбери два текстовых блока или два списка.",
    )


def _split_operations(
    document: WorkingDocument,
    decision: IntentDecision,
) -> list[DocumentOperation]:
    target = _root_node(document, decision.target_ordinals, 0)
    if target.kind is NodeKind.LIST:
        items = target.data.get("items")
        if not isinstance(items, list) or len(items) < 2:
            raise ChatError(ChatErrorCode.CLARIFICATION)
        root_index = document.nodes.index(target)
        left_data = {**target.data, "items": [items[0]]}
        right_data = {**target.data, "items": items[1:]}
        for data in (left_data, right_data):
            data.pop("items_html", None)
            data.pop("item_styles", None)
        return [
            UpdateData(node_id=target.id, data=left_data),
            InsertNode(
                index=root_index + 1,
                node=target.model_copy(
                    update={
                        "id": f"split-{uuid4()}",
                        "data": right_data,
                        "flags": list(dict.fromkeys([*target.flags, "structural-edit"])),
                    }
                ),
            ),
        ]
    if target.kind is not NodeKind.PARAGRAPH or not target.text:
        raise ChatError(ChatErrorCode.CLARIFICATION)
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", target.text) if part.strip()]
    if len(parts) < 2:
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message="В блоке не найдено безопасной границы разделения",
            action="Укажи текст, с которого должен начинаться новый блок.",
        )
    root_index = document.nodes.index(target)
    flags = list(dict.fromkeys([*target.flags, "structural-edit"]))
    operations: list[DocumentOperation] = [
        UpdateText(node_id=target.id, text=parts[0]),
        UpdateFlags(node_id=target.id, flags=flags),
    ]
    operations.extend(
        InsertNode(
            index=root_index + offset,
            node=DocumentNode(
                id=f"split-{uuid4()}",
                kind=NodeKind.PARAGRAPH,
                text=part,
                provenance=target.provenance,
                flags=flags,
            ),
        )
        for offset, part in enumerate(parts[1:], start=1)
    )
    return operations


def _root_node(
    document: WorkingDocument,
    ordinals: tuple[int, ...],
    ordinal_index: int,
) -> DocumentNode:
    if len(ordinals) <= ordinal_index:
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message="Не указан номер структурного блока",
            action="Укажи номер блока в текущем документе.",
        )
    ordinal = ordinals[ordinal_index]
    content = [node for node in document.nodes if node.kind is not NodeKind.GAP]
    if ordinal < 1 or ordinal > len(content):
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message=f"Блок {ordinal} не найден",
            action="Проверь номер блока и повтори команду.",
        )
    return content[ordinal - 1]


def _replacement_operations(
    document: WorkingDocument,
    replacement: AuthoredReplacement,
) -> list[DocumentOperation]:
    target = replacement.target
    if target is None:
        target = _resolve_declared_value_target(document, replacement.replacement)
    if not target:
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message="Не удалось однозначно найти прежнее значение",
            action="Укажи замену явно: «замени старый текст на новый».",
        )

    operations: list[DocumentOperation] = []
    updated_title = _replace_text(document.title, target, replacement.replacement)
    if updated_title != document.title:
        operations.append(UpdateTitle(title=updated_title))
    for node in _walk(document.nodes):
        changed = False
        if node.text is not None:
            updated_text = _replace_text(node.text, target, replacement.replacement)
            if updated_text != node.text:
                operations.append(UpdateText(node_id=node.id, text=updated_text))
                changed = True
        updated_data = _replace_data(node.data, target, replacement.replacement)
        if updated_data != node.data:
            operations.append(UpdateData(node_id=node.id, data=updated_data))
            changed = True
        if changed and "manual-edit" not in node.flags:
            operations.append(
                UpdateFlags(node_id=node.id, flags=[*node.flags, "manual-edit"])
            )
    if not operations:
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message=f"Текст «{target}» не найден в документе",
            action="Укажи точный старый текст или целевой блок.",
        )
    return operations


def _resolve_declared_value_target(
    document: WorkingDocument,
    replacement: str,
) -> str | None:
    replacement_words = _LATIN_TOKEN.findall(replacement)
    candidates = {
        token
        for node in _walk(document.nodes)
        for token in _LATIN_TOKEN.findall(
            " ".join([node.text or "", _data_text(node.data)])
        )
    }
    if not replacement_words or not candidates:
        return None
    replacement_word = replacement_words[-1]
    ranked = sorted(
        (
            SequenceMatcher(None, candidate.casefold(), replacement_word.casefold()).ratio(),
            candidate,
        )
        for candidate in candidates
        if candidate.casefold() != replacement_word.casefold()
    )
    if not ranked or ranked[-1][0] < 0.7:
        return None
    best_score, best = ranked[-1]
    if len(ranked) > 1 and ranked[-2][0] == best_score and ranked[-2][1] != best:
        return None
    return best


def _replace_text(value: str, target: str, replacement: str) -> str:
    return re.sub(re.escape(target), replacement, value, flags=re.IGNORECASE)


def _replace_data(
    value: Any,
    target: str,
    replacement: str,
    *,
    textual: bool = False,
) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_data(
                item,
                target,
                replacement,
                textual=textual or str(key).casefold() in _TEXTUAL_DATA_KEYS,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_data(item, target, replacement, textual=textual)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _replace_data(item, target, replacement, textual=textual)
            for item in value
        )
    if isinstance(value, str) and textual:
        return _replace_text(value, target, replacement)
    return value


def _sectionize(document: WorkingDocument) -> list[DocumentOperation]:
    content = [node for node in document.nodes if node.kind is not NodeKind.GAP]
    if not content:
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message="В документе нет блоков для разделения",
            action="Добавь текст и повтори команду.",
        )
    if all(node.kind is NodeKind.HEADING and node.children for node in content):
        raise ChatError(
            ChatErrorCode.CLARIFICATION,
            message="Документ уже разделён на семантические разделы",
            action="Уточни, какие разделы нужно объединить или переименовать.",
        )

    operations: list[DocumentOperation] = []
    root_index = 0
    section_number = 1
    for node in content:
        if node.kind is NodeKind.HEADING:
            root_index += 1
            continue
        heading_id = f"section-{uuid4()}"
        operations.append(
            InsertNode(
                index=root_index,
                node=DocumentNode(
                    id=heading_id,
                    kind=NodeKind.HEADING,
                    text=f"Раздел {section_number}",
                    flags=["structural-edit"],
                ),
            )
        )
        operations.append(MoveNode(node_id=node.id, parent_id=heading_id, index=0))
        root_index += 1
        section_number += 1
    return operations


def _selected_visible_node(
    document: WorkingDocument,
    message: str,
) -> DocumentNode | None:
    visible = [
        node
        for node in _walk(document.nodes)
        if node.kind in {NodeKind.HEADING, NodeKind.PARAGRAPH, NodeKind.LIST, NodeKind.TABLE}
    ]
    ordinal = _ordinal_from_message(message) or 1
    return visible[ordinal - 1] if 0 < ordinal <= len(visible) else None


def _ordinal_from_message(message: str) -> int | None:
    numeric = re.search(r"\b(\d+)(?:-(?:й|ый|го))?\s+(?:абзац|блок)", message.casefold())
    if numeric is not None:
        return int(numeric.group(1))
    normalized = message.casefold().replace("ё", "е")
    for word, ordinal in _ORDINALS.items():
        if word in normalized:
            return ordinal
    return None


def _style_from_message(message: str) -> dict[str, str]:
    normalized = message.casefold().replace("ё", "е")
    style: dict[str, str] = {}
    rules = (
        (("жирн", "полужирн", "bold"), "font-weight", "700"),
        (("курсив", "italic"), "font-style", "italic"),
        (("подчерк", "underline"), "text-decoration", "underline"),
        (("син", "blue"), "color", "blue"),
        (("красн", "red"), "color", "red"),
        (("зелен", "green"), "color", "green"),
        (("выровн",), "text-align", "center"),
        (("отступ", "indent"), "margin-left", "24px"),
    )
    for terms, key, value in rules:
        if any(term in normalized for term in terms):
            style[key] = value
    return style


def _merge_data(existing: dict, update: dict) -> dict:
    merged = dict(existing)
    for key, value in update.items():
        current = merged.get(key)
        merged[key] = (
            _merge_data(current, value)
            if isinstance(current, dict) and isinstance(value, dict)
            else value
        )
    return merged


def _data_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_data_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_data_text(item) for item in value)
    return value if isinstance(value, str) else ""


def _walk(nodes: list[DocumentNode]):
    for node in nodes:
        yield node
        yield from _walk(node.children)


_LATIN_TOKEN = re.compile(r"\b[A-Za-z][A-Za-z0-9._-]*\b")
_TEXTUAL_DATA_KEYS = {
    "alt",
    "caption",
    "items",
    "items_html",
    "rows",
    "text",
    "title",
}
_ORDINALS = {
    "перв": 1,
    "втор": 2,
    "трет": 3,
    "четверт": 4,
    "пят": 5,
    "шест": 6,
    "седьм": 7,
    "восьм": 8,
    "девят": 9,
    "десят": 10,
}
