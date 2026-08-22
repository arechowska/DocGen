from __future__ import annotations

from enum import StrEnum


class ChatErrorCode(StrEnum):
    SOURCES_MISSING = "sources_missing"
    SOURCE_UNAVAILABLE = "source_unavailable"
    RELEVANT_FRAGMENT_MISSING = "relevant_fragment_missing"
    MODEL_INVALID_JSON = "model_invalid_json"
    EVIDENCE_MISSING = "evidence_missing"
    GROUNDING_FAILED = "grounding_failed"
    REVISION_CONFLICT = "revision_conflict"
    CLARIFICATION = "clarification_required"
    INVALID_OPERATION = "invalid_operation"


_ERROR_DETAILS: dict[ChatErrorCode, tuple[str, str, int]] = {
    ChatErrorCode.SOURCES_MISSING: (
        "К проекту не добавлены источники",
        "Добавь источник и повтори запрос.",
        422,
    ),
    ChatErrorCode.SOURCE_UNAVAILABLE: (
        "Источник не удалось прочитать",
        "Проверь доступ к источнику и повтори запрос.",
        503,
    ),
    ChatErrorCode.RELEVANT_FRAGMENT_MISSING: (
        "В источниках не найден фрагмент по теме запроса",
        "Уточни формулировку или добавь подходящий источник.",
        422,
    ),
    ChatErrorCode.MODEL_INVALID_JSON: (
        "Модель вернула ответ в неверном формате",
        "Повтори запрос; если ошибка повторится, проверь настройку модели.",
        502,
    ),
    ChatErrorCode.EVIDENCE_MISSING: (
        "Ответ модели не содержит подтверждения из источника",
        "Уточни запрос или проверь содержание источника.",
        422,
    ),
    ChatErrorCode.GROUNDING_FAILED: (
        "Предложенная правка не подтверждается выбранным фрагментом",
        "Уточни факт или укажи другой источник.",
        422,
    ),
    ChatErrorCode.REVISION_CONFLICT: (
        "Документ уже изменён",
        "Обнови документ и повтори правку.",
        409,
    ),
    ChatErrorCode.CLARIFICATION: (
        "Нужно уточнить команду",
        "Уточни действие и целевой блок документа.",
        422,
    ),
    ChatErrorCode.INVALID_OPERATION: (
        "Предложенную правку нельзя безопасно применить",
        "Уточни целевой блок или повтори запрос.",
        422,
    ),
}


class ChatError(ValueError):
    def __init__(
        self,
        code: ChatErrorCode,
        *,
        message: str | None = None,
        action: str | None = None,
    ) -> None:
        default_message, default_action, status_code = _ERROR_DETAILS[code]
        self.code = code
        self.message = message or default_message
        self.action = action or default_action
        self.status_code = status_code
        super().__init__(self.message)
