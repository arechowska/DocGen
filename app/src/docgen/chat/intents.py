from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from docgen.chat.manual_insert import ManualInsertIntent, parse_manual_insert
from docgen.documents.schemas import WorkingDocument


class IntentKind(StrEnum):
    AUTHORED_EDIT = "authored_edit"
    GROUNDED_EDIT = "grounded_edit"
    TEMPLATE_ACTION = "template_action"
    STRUCTURE = "structure"
    FORMAT = "format"
    CLARIFICATION = "clarification"


class StructureAction(StrEnum):
    SECTIONIZE = "sectionize"
    DELETE = "delete"
    MOVE = "move"
    MERGE = "merge"
    SPLIT = "split"


@dataclass(frozen=True)
class AuthoredReplacement:
    replacement: str
    target: str | None = None
    subject: str | None = None


@dataclass(frozen=True)
class IntentDecision:
    kind: IntentKind
    manual_insert: ManualInsertIntent | None = None
    replacement: AuthoredReplacement | None = None
    structure_action: StructureAction | None = None
    target_ordinals: tuple[int, ...] = ()
    relation: str | None = None
    retrieval_query: str = ""
    template_action: str | None = None
    clarification: str = ""


_EXPLICIT_REPLACEMENT = re.compile(
    r"^\s*(?:замени|заменить|исправь|исправить)\s+"
    r"(?P<target>.+?)\s+(?:на|как)\s+(?P<replacement>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_DECLARED_VALUE = re.compile(
    r"^\s*(?P<subject>.+?)\s+(?:должен|должна|должно|должны)\s+"
    r"быть\s+(?P<replacement>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_QUESTION_ACTION = re.compile(
    r"\b(?:добав(?:ь|ить)|созда(?:й|ть)|состав(?:ь|ить)|сформируй)\b.*"
    r"\bвопрос(?:\b|ы|ов)",
    re.IGNORECASE | re.DOTALL,
)
_FORMAT_TERMS = (
    "жирн",
    "bold",
    "полужирн",
    "курсив",
    "подчерк",
    "шрифт",
    "цвет",
    "сини",
    "blue",
    "красн",
    "зелен",
    "выровн",
    "отступ",
    "формат",
    "indent",
)
_STRUCTURE_RULES = (
    (StructureAction.DELETE, ("удал",)),
    (StructureAction.MOVE, ("перемест", "перестав")),
    (StructureAction.MERGE, ("объедин", "соедин")),
    (StructureAction.SECTIONIZE, ("раздел", "секци")),
    (StructureAction.SPLIT, ("разбей", "раздел")),
)
_GROUNDING_MARKERS = (
    "по источник",
    "из источник",
    "согласно источник",
    "подтвержд",
    "факт",
)
_GROUNDING_VERBS = ("уточн", "допол", "исправ", "обнов", "добав")
_QUERY_NOISE = {
    "добавь",
    "добавить",
    "документ",
    "из",
    "источника",
    "источникам",
    "источнике",
    "источнику",
    "по",
    "согласно",
    "строго",
    "уточни",
    "уточнить",
}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def route_intent(message: str, document: WorkingDocument) -> IntentDecision:
    normalized = " ".join(message.strip().split())
    lowered = normalized.casefold().replace("ё", "е")

    manual = parse_manual_insert(normalized)
    if manual is not None and _is_positioned_or_complete_pair(normalized, manual):
        return IntentDecision(IntentKind.AUTHORED_EDIT, manual_insert=manual)

    replacement = _authored_replacement(normalized)
    if replacement is not None:
        return IntentDecision(IntentKind.AUTHORED_EDIT, replacement=replacement)

    if any(term in lowered for term in _FORMAT_TERMS):
        return IntentDecision(IntentKind.FORMAT)

    structure = _structure_action(lowered)
    if structure is not None:
        return IntentDecision(
            IntentKind.STRUCTURE,
            structure_action=structure,
            target_ordinals=_ordinals(normalized),
            relation=(
                "before"
                if "перед" in lowered
                else "after"
                if "после" in lowered
                else None
            ),
        )

    if _QUESTION_ACTION.search(normalized):
        query = _retrieval_query(normalized)
        if document.template_id == "faq":
            return IntentDecision(
                IntentKind.TEMPLATE_ACTION,
                retrieval_query=query,
                template_action="faq.add_entry",
            )
        return IntentDecision(
            IntentKind.CLARIFICATION,
            clarification=(
                "Уточни, куда добавить вопрос и в каком виде оформить ответ; "
                "пара вопрос–ответ автоматически поддерживается для шаблона FAQ."
            ),
        )

    if manual is not None and _is_explicit_authored_insert(normalized, manual):
        return IntentDecision(IntentKind.AUTHORED_EDIT, manual_insert=manual)

    if any(marker in lowered for marker in _GROUNDING_MARKERS) or any(
        lowered.startswith(verb) for verb in _GROUNDING_VERBS
    ):
        return IntentDecision(
            IntentKind.GROUNDED_EDIT,
            retrieval_query=_retrieval_query(normalized),
        )

    return IntentDecision(
        IntentKind.CLARIFICATION,
        clarification=(
            "Уточни, что изменить: текст или факт, целевой блок, структуру либо оформление."
        ),
    )


def _is_explicit_authored_insert(
    message: str, intent: ManualInsertIntent
) -> bool:
    lowered = message.casefold()
    return (
        intent.explicit_position
        or ":" in message
        or ("вопрос" in lowered and "ответ" in lowered)
        or len(intent.text.split()) >= 2
    )


def _is_positioned_or_complete_pair(
    message: str,
    intent: ManualInsertIntent,
) -> bool:
    lowered = message.casefold()
    return intent.explicit_position or (
        "вопрос" in lowered and "ответ" in lowered
    )


def _authored_replacement(message: str) -> AuthoredReplacement | None:
    explicit = _EXPLICIT_REPLACEMENT.match(message)
    if explicit is not None:
        return AuthoredReplacement(
            target=_clean_value(explicit.group("target")),
            replacement=_clean_value(explicit.group("replacement")),
        )
    declared = _DECLARED_VALUE.match(message)
    if declared is not None:
        return AuthoredReplacement(
            subject=_clean_value(declared.group("subject")),
            replacement=_clean_value(declared.group("replacement")),
        )
    return None


def _structure_action(message: str) -> StructureAction | None:
    for action, stems in _STRUCTURE_RULES:
        if not any(stem in message for stem in stems):
            continue
        if action is StructureAction.SECTIONIZE and not any(
            term in message for term in ("раздел", "секци")
        ):
            continue
        if action is StructureAction.SECTIONIZE and "блок" in message:
            continue
        return action
    return None


def _retrieval_query(message: str) -> str:
    tokens = [
        token
        for token in _WORD.findall(message.casefold().replace("ё", "е"))
        if token not in _QUERY_NOISE and len(token) > 2
    ]
    return " ".join(tokens)


def _clean_value(value: str) -> str:
    return value.strip(" \t\r\n:;,.—-«»\"")


def _ordinals(message: str) -> tuple[int, ...]:
    matches: list[tuple[int, int]] = []
    normalized = message.casefold().replace("ё", "е")
    for match in re.finditer(r"\b(?P<number>\d+)(?:-(?:й|ый|го))?\b", normalized):
        matches.append((match.start(), int(match.group("number"))))
    for stem, number in _ORDINAL_STEMS.items():
        for match in re.finditer(rf"\b{stem}\w*", normalized):
            matches.append((match.start(), number))
    return tuple(number for _position, number in sorted(matches))


_ORDINAL_STEMS = {
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
