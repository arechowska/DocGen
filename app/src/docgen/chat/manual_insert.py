import re
from dataclasses import dataclass
from enum import Enum
from uuid import uuid4

from docgen.documents.operations import DeleteNode, DocumentOperation, InsertNode
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument


class InsertAnchor(str, Enum):
    DOCUMENT_START = "document_start"
    DOCUMENT_END = "document_end"
    BEFORE_VISUAL = "before_visual"
    AFTER_VISUAL = "after_visual"


@dataclass(frozen=True)
class ManualInsertIntent:
    text: str
    anchor: InsertAnchor
    ordinal: int | None
    explicit_position: bool


class ManualInsertError(ValueError):
    pass


class ManualInsertTargetError(ManualInsertError):
    pass


@dataclass(frozen=True)
class _VisualTarget:
    parent_id: str | None
    node_index: int
    node: DocumentNode
    list_item_index: int | None = None


_MANUAL_COMMAND_PATTERN = re.compile(
    r"^\s*(?:добавь|добавить|вставь|вставить|допиши|дописать)\b(?P<body>.*?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_QUESTION_ANSWER_PATTERN = re.compile(
    r"\bвопрос\b\s*:?\s*(?P<question>.+?)\s+\bответ\b\s*:?\s*(?P<answer>.+)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_DOCUMENT_START_PATTERN = re.compile(
    r"^\s*в\s+начало\s+документа\s*[:;,.—-]*\s*(?P<text>.*?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_DOCUMENT_END_PATTERN = re.compile(
    r"^\s*в\s+конец\s+документа\s*[:;,.—-]*\s*(?P<text>.*?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_POSITION_PATTERN = re.compile(
    r"^\s*(?P<relation>перед|после|в\s+начало|в\s+конец)\s+"
    r"(?P<ordinal>\S+)\s+абзац(?:а|ем)?\s*[:;,.—-]*\s*(?P<text>.*?)\s*$",
    re.IGNORECASE | re.DOTALL,
)

_ORDINALS = {
    "нулевого": 0,
    "нулевым": 0,
    "первого": 1,
    "первым": 1,
    "второго": 2,
    "вторым": 2,
    "третьего": 3,
    "третьим": 3,
    "четвертого": 4,
    "четвертым": 4,
    "пятого": 5,
    "пятым": 5,
    "шестого": 6,
    "шестым": 6,
    "седьмого": 7,
    "седьмым": 7,
    "восьмого": 8,
    "восьмым": 8,
    "девятого": 9,
    "девятым": 9,
    "десятого": 10,
    "десятым": 10,
    "одиннадцатого": 11,
    "одиннадцатым": 11,
    "двенадцатого": 12,
    "двенадцатым": 12,
    "тринадцатого": 13,
    "тринадцатым": 13,
    "четырнадцатого": 14,
    "четырнадцатым": 14,
    "пятнадцатого": 15,
    "пятнадцатым": 15,
    "шестнадцатого": 16,
    "шестнадцатым": 16,
    "семнадцатого": 17,
    "семнадцатым": 17,
    "восемнадцатого": 18,
    "восемнадцатым": 18,
    "девятнадцатого": 19,
    "девятнадцатым": 19,
    "двадцатого": 20,
    "двадцатым": 20,
}
_NUMERIC_ORDINAL_PATTERN = re.compile(r"^(?P<number>\d+)(?:-(?:й|ый|го))?$", re.IGNORECASE)


def parse_manual_insert(message: str) -> ManualInsertIntent | None:
    command = _MANUAL_COMMAND_PATTERN.match(message)
    if command is None:
        return None

    body = command.group("body")
    start = _DOCUMENT_START_PATTERN.match(body)
    if start is not None:
        return _intent(start.group("text"), InsertAnchor.DOCUMENT_START, None, True)

    end = _DOCUMENT_END_PATTERN.match(body)
    if end is not None:
        return _intent(end.group("text"), InsertAnchor.DOCUMENT_END, None, True)

    position = _POSITION_PATTERN.match(body)
    if position is not None:
        ordinal = _parse_ordinal(position.group("ordinal"))
        relation = position.group("relation").casefold()
        anchor = (
            InsertAnchor.BEFORE_VISUAL
            if relation == "перед" or relation == "в начало"
            else InsertAnchor.AFTER_VISUAL
        )
        return _intent(position.group("text"), anchor, ordinal, True)

    return _intent(body, InsertAnchor.DOCUMENT_END, None, False)


def manual_insert_operations(
    document: WorkingDocument,
    intent: ManualInsertIntent,
) -> list[DocumentOperation]:
    paragraph = _manual_paragraph(intent.text)
    if intent.anchor is InsertAnchor.DOCUMENT_START:
        return [InsertNode(parent_id=None, index=0, node=paragraph)]
    if intent.anchor is InsertAnchor.DOCUMENT_END:
        return [InsertNode(parent_id=None, index=len(document.nodes), node=paragraph)]

    target = _visual_target(document, intent.ordinal)
    if target.list_item_index is not None:
        return _list_insert_operations(target, intent.anchor, paragraph)

    index = (
        target.node_index
        if intent.anchor is InsertAnchor.BEFORE_VISUAL
        else target.node_index + 1
    )
    return [InsertNode(parent_id=target.parent_id, index=index, node=paragraph)]


def _manual_paragraph(text: str) -> DocumentNode:
    return DocumentNode(
        id=f"manual-{uuid4()}",
        kind=NodeKind.PARAGRAPH,
        text=text,
        flags=["manual-edit"],
    )


def _visual_target(document: WorkingDocument, ordinal: int | None) -> _VisualTarget:
    targets = _visual_targets(document.nodes, None)
    if ordinal is None or ordinal > len(targets):
        raise ManualInsertTargetError(f"Абзац {ordinal} не найден")
    return targets[ordinal - 1]


def _visual_targets(
    nodes: list[DocumentNode],
    parent_id: str | None,
) -> list[_VisualTarget]:
    targets: list[_VisualTarget] = []
    for index, node in enumerate(nodes):
        if node.kind in {NodeKind.HEADING, NodeKind.PARAGRAPH} and (node.text or "").strip():
            targets.append(_VisualTarget(parent_id, index, node))
        if node.kind is NodeKind.LIST:
            items = node.data.get("items")
            if isinstance(items, list):
                targets.extend(
                    _VisualTarget(parent_id, index, node, item_index)
                    for item_index, item in enumerate(items)
                    if isinstance(item, str)
                )
        targets.extend(_visual_targets(node.children, node.id))
    return targets


def _list_insert_operations(
    target: _VisualTarget,
    anchor: InsertAnchor,
    paragraph: DocumentNode,
) -> list[DocumentOperation]:
    assert target.list_item_index is not None
    items = target.node.data.get("items")
    assert isinstance(items, list)
    boundary = target.list_item_index
    if anchor is InsertAnchor.AFTER_VISUAL:
        boundary += 1

    if boundary == 0:
        return [InsertNode(parent_id=target.parent_id, index=target.node_index, node=paragraph)]
    if boundary == len(items):
        return [InsertNode(parent_id=target.parent_id, index=target.node_index + 1, node=paragraph)]

    left_list, right_list = _split_list_node(target.node, boundary)
    return [
        InsertNode(parent_id=target.parent_id, index=target.node_index + 1, node=paragraph),
        DeleteNode(node_id=target.node.id),
        InsertNode(parent_id=target.parent_id, index=target.node_index, node=left_list),
        InsertNode(parent_id=target.parent_id, index=target.node_index + 2, node=right_list),
    ]


def _split_list_node(node: DocumentNode, boundary: int) -> tuple[DocumentNode, DocumentNode]:
    left_data = dict(node.data)
    right_data = dict(node.data)
    for key in ("items", "items_html", "item_styles"):
        value = node.data.get(key)
        if isinstance(value, list):
            left_data[key] = value[:boundary]
            right_data[key] = value[boundary:]

    left_list = node.model_copy(update={"data": left_data, "children": []})
    right_list = node.model_copy(
        update={
            "id": f"manual-split-{uuid4()}",
            "data": right_data,
        }
    )
    return left_list, right_list


def _intent(
    text: str,
    anchor: InsertAnchor,
    ordinal: int | None,
    explicit_position: bool,
) -> ManualInsertIntent:
    normalized = _normalize_authored_text(text)
    if not normalized:
        raise ManualInsertError("Укажите текст для добавления")
    return ManualInsertIntent(normalized, anchor, ordinal, explicit_position)


def _parse_ordinal(value: str) -> int:
    numeric = _NUMERIC_ORDINAL_PATTERN.fullmatch(value)
    if numeric is not None:
        ordinal = int(numeric.group("number"))
        if ordinal == 0:
            raise ManualInsertError("Номер абзаца должен быть больше нуля")
        return ordinal

    ordinal = _ORDINALS.get(value.casefold())
    if ordinal is None:
        raise ManualInsertError("Неверный номер абзаца")
    if ordinal == 0:
        raise ManualInsertError("Номер абзаца должен быть больше нуля")
    return ordinal


def _normalize_authored_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip(" \t\r\n:;,.—-")
    question_answer = _QUESTION_ANSWER_PATTERN.search(text)
    if question_answer is None:
        return text
    question = _clean_fragment(question_answer.group("question"))
    answer = _clean_fragment(question_answer.group("answer"))
    return f"Вопрос: {question}\nОтвет: {answer}" if question and answer else text


def _clean_fragment(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t\r\n:;,.—-")
