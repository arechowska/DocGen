import re
from dataclasses import dataclass
from enum import Enum


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
