import pytest

from docgen.chat.manual_insert import (
    InsertAnchor,
    ManualInsertError,
    ManualInsertIntent,
    parse_manual_insert,
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "допиши в начало документа: Введение",
            ManualInsertIntent("Введение", InsertAnchor.DOCUMENT_START, None, True),
        ),
        (
            "добавь в конец документа: Итог",
            ManualInsertIntent("Итог", InsertAnchor.DOCUMENT_END, None, True),
        ),
        (
            "вставь в начало второго абзаца: Перед ним",
            ManualInsertIntent("Перед ним", InsertAnchor.BEFORE_VISUAL, 2, True),
        ),
        (
            "допиши перед третьим абзацем: Перед третьим",
            ManualInsertIntent("Перед третьим", InsertAnchor.BEFORE_VISUAL, 3, True),
        ),
        (
            "допиши в конец 21-го абзаца: После него",
            ManualInsertIntent("После него", InsertAnchor.AFTER_VISUAL, 21, True),
        ),
        (
            "допиши после 2 абзаца: После второго",
            ManualInsertIntent("После второго", InsertAnchor.AFTER_VISUAL, 2, True),
        ),
        (
            "добавь вопрос Где вход ответ Справа",
            ManualInsertIntent(
                "Вопрос: Где вход\nОтвет: Справа",
                InsertAnchor.DOCUMENT_END,
                None,
                False,
            ),
        ),
    ],
)
def test_parse_manual_insert(message: str, expected: ManualInsertIntent) -> None:
    assert parse_manual_insert(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "уточни второй абзац",
        "сделай заголовок жирным",
    ],
)
def test_parse_manual_insert_ignores_non_manual_commands(message: str) -> None:
    assert parse_manual_insert(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "допиши в начало документа:",
        "добавь после второго абзаца:",
        "вставь перед нулевым абзацем: Текст",
        "добавь после 0 абзаца: Текст",
    ],
)
def test_parse_manual_insert_rejects_missing_text_or_invalid_ordinal(message: str) -> None:
    with pytest.raises(ManualInsertError):
        parse_manual_insert(message)


@pytest.mark.parametrize(
    ("number", "genitive", "instrumental"),
    [
        (1, "первого", "первым"),
        (2, "второго", "вторым"),
        (3, "третьего", "третьим"),
        (4, "четвертого", "четвертым"),
        (5, "пятого", "пятым"),
        (6, "шестого", "шестым"),
        (7, "седьмого", "седьмым"),
        (8, "восьмого", "восьмым"),
        (9, "девятого", "девятым"),
        (10, "десятого", "десятым"),
        (11, "одиннадцатого", "одиннадцатым"),
        (12, "двенадцатого", "двенадцатым"),
        (13, "тринадцатого", "тринадцатым"),
        (14, "четырнадцатого", "четырнадцатым"),
        (15, "пятнадцатого", "пятнадцатым"),
        (16, "шестнадцатого", "шестнадцатым"),
        (17, "семнадцатого", "семнадцатым"),
        (18, "восемнадцатого", "восемнадцатым"),
        (19, "девятнадцатого", "девятнадцатым"),
        (20, "двадцатого", "двадцатым"),
    ],
)
def test_parse_russian_ordinal_forms(
    number: int,
    genitive: str,
    instrumental: str,
) -> None:
    after = parse_manual_insert(f"допиши после {genitive} абзаца: Текст")
    before = parse_manual_insert(f"допиши перед {instrumental} абзацем: Текст")

    assert after is not None and after.ordinal == number
    assert before is not None and before.ordinal == number


@pytest.mark.parametrize(
    ("position", "expected_anchor", "expected_ordinal"),
    [
        ("7", InsertAnchor.AFTER_VISUAL, 7),
        ("8-й", InsertAnchor.AFTER_VISUAL, 8),
        ("9-ый", InsertAnchor.AFTER_VISUAL, 9),
        ("10-го", InsertAnchor.AFTER_VISUAL, 10),
        ("11", InsertAnchor.BEFORE_VISUAL, 11),
    ],
)
def test_parse_numeric_ordinal_suffixes(
    position: str,
    expected_anchor: InsertAnchor,
    expected_ordinal: int,
) -> None:
    command = (
        f"добавить после {position} абзаца; Текст"
        if expected_anchor is InsertAnchor.AFTER_VISUAL
        else f"вставить в начало {position} абзаца: Текст"
    )

    assert parse_manual_insert(command) == ManualInsertIntent(
        "Текст", expected_anchor, expected_ordinal, True
    )


@pytest.mark.parametrize(
    ("message", "expected_message"),
    [
        (
            "дописать в конец 2 абзаца — Текст",
            ManualInsertIntent("Текст", InsertAnchor.AFTER_VISUAL, 2, True),
        ),
        (
            "допиши в начало 3 абзаца: Текст",
            ManualInsertIntent("Текст", InsertAnchor.BEFORE_VISUAL, 3, True),
        ),
    ],
)
def test_parse_manual_insert_strips_only_position_separator(
    message: str,
    expected_message: ManualInsertIntent,
) -> None:
    assert parse_manual_insert(message) == expected_message


def test_parse_manual_insert_reports_exact_validation_messages() -> None:
    with pytest.raises(ManualInsertError, match="Укажите текст для добавления"):
        parse_manual_insert("допиши в начало документа:")

    with pytest.raises(ManualInsertError, match="Номер абзаца должен быть больше нуля"):
        parse_manual_insert("добавь после 0 абзаца: Текст")

    with pytest.raises(ManualInsertError, match="Номер абзаца должен быть больше нуля"):
        parse_manual_insert("вставь перед нулевым абзацем: Текст")
