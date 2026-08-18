import pytest

from docgen.chat.manual_insert import (
    InsertAnchor,
    ManualInsertError,
    ManualInsertIntent,
    manual_insert_operations,
    parse_manual_insert,
)
from docgen.documents.operations import apply_operations
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument


def test_insert_before_second_visual_paragraph_counts_heading() -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(id="heading", kind=NodeKind.HEADING, text="Заголовок"),
            DocumentNode(id="body", kind=NodeKind.PARAGRAPH, text="Текст"),
        ],
    )
    intent = ManualInsertIntent("Новый", InsertAnchor.BEFORE_VISUAL, 2, True)

    result = apply_operations(document, manual_insert_operations(document, intent))

    assert [node.text for node in result.nodes] == ["Заголовок", "Новый", "Текст"]
    assert result.nodes[1].flags == ["manual-edit"]


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        (InsertAnchor.DOCUMENT_START, ["Новый", "Первый", "Второй"]),
        (InsertAnchor.DOCUMENT_END, ["Первый", "Второй", "Новый"]),
    ],
)
def test_insert_at_document_boundary(anchor: InsertAnchor, expected: list[str]) -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(id="one", kind=NodeKind.PARAGRAPH, text="Первый"),
            DocumentNode(id="two", kind=NodeKind.PARAGRAPH, text="Второй"),
        ],
    )
    intent = ManualInsertIntent("Новый", anchor, None, True)

    result = apply_operations(document, manual_insert_operations(document, intent))

    assert [node.text for node in result.nodes] == expected


def test_visual_numbering_skips_non_text_nodes_and_inserts_with_nested_target() -> None:
    parent = DocumentNode(
        id="parent",
        kind=NodeKind.TABLE,
        data={"rows": [["Ячейка"]]},
        children=[
            DocumentNode(id="nested", kind=NodeKind.PARAGRAPH, text="Вложенный"),
        ],
    )
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(id="gap", kind=NodeKind.GAP),
            parent,
            DocumentNode(id="image", kind=NodeKind.IMAGE, data={"src": "x"}),
            DocumentNode(id="last", kind=NodeKind.PARAGRAPH, text="Последний"),
        ],
    )
    intent = ManualInsertIntent("Новый", InsertAnchor.AFTER_VISUAL, 1, True)

    result = apply_operations(document, manual_insert_operations(document, intent))

    assert [child.text for child in result.nodes[1].children] == ["Вложенный", "Новый"]
    assert result.nodes[3].text == "Последний"


def _list_node() -> DocumentNode:
    return DocumentNode(
        id="steps",
        kind=NodeKind.LIST,
        data={
            "ordered": True,
            "style": {"color": "blue"},
            "items": ["Первый", "Второй", "Третий"],
            "items_html": ["<b>Первый</b>", "Второй", "Третий"],
            "item_styles": ["font-weight:700", "", "font-style:italic"],
        },
    )


@pytest.mark.parametrize(
    ("anchor", "ordinal", "left_items", "right_items"),
    [
        (InsertAnchor.BEFORE_VISUAL, 2, ["Первый"], ["Второй", "Третий"]),
        (InsertAnchor.AFTER_VISUAL, 2, ["Первый", "Второй"], ["Третий"]),
    ],
)
def test_insert_around_internal_list_item_preserves_aligned_metadata(
    anchor: InsertAnchor,
    ordinal: int,
    left_items: list[str],
    right_items: list[str],
) -> None:
    list_node = _list_node()
    document = WorkingDocument(title="Документ", template_id="use-case", nodes=[list_node])
    intent = ManualInsertIntent("Новый", anchor, ordinal, True)

    result = apply_operations(document, manual_insert_operations(document, intent))

    assert [node.kind for node in result.nodes] == [
        NodeKind.LIST,
        NodeKind.PARAGRAPH,
        NodeKind.LIST,
    ]
    left, paragraph, right = result.nodes
    split = len(left_items)
    assert left.data["items"] == left_items
    assert right.data["items"] == right_items
    assert left.data["items_html"] == list_node.data["items_html"][:split]
    assert right.data["items_html"] == list_node.data["items_html"][split:]
    assert left.data["item_styles"] == list_node.data["item_styles"][:split]
    assert right.data["item_styles"] == list_node.data["item_styles"][split:]
    assert left.data["ordered"] is True and right.data["ordered"] is True
    assert left.data["style"] == right.data["style"] == {"color": "blue"}
    assert paragraph.text == "Новый"


@pytest.mark.parametrize(
    ("anchor", "ordinal", "expected_kinds"),
    [
        (InsertAnchor.BEFORE_VISUAL, 1, [NodeKind.PARAGRAPH, NodeKind.LIST]),
        (InsertAnchor.AFTER_VISUAL, 3, [NodeKind.LIST, NodeKind.PARAGRAPH]),
    ],
)
def test_insert_at_list_boundary_does_not_split_list(
    anchor: InsertAnchor,
    ordinal: int,
    expected_kinds: list[NodeKind],
) -> None:
    list_node = _list_node()
    document = WorkingDocument(title="Документ", template_id="use-case", nodes=[list_node])
    intent = ManualInsertIntent("Новый", anchor, ordinal, True)

    result = apply_operations(document, manual_insert_operations(document, intent))

    assert [node.kind for node in result.nodes] == expected_kinds
    preserved = next(node for node in result.nodes if node.kind is NodeKind.LIST)
    assert preserved.data == list_node.data


def test_mixed_visual_order_counts_each_list_item() -> None:
    document = WorkingDocument(
        title="Документ",
        template_id="use-case",
        nodes=[
            DocumentNode(id="heading", kind=NodeKind.HEADING, text="Заголовок"),
            DocumentNode(
                id="list",
                kind=NodeKind.LIST,
                data={"items": ["Первый", "Второй"]},
            ),
            DocumentNode(id="last", kind=NodeKind.PARAGRAPH, text="Последний"),
        ],
    )
    intent = ManualInsertIntent("Новый", InsertAnchor.BEFORE_VISUAL, 3, True)

    result = apply_operations(document, manual_insert_operations(document, intent))

    assert result.nodes[1].data["items"] == ["Первый"]
    assert result.nodes[2].text == "Новый"
    assert result.nodes[3].data["items"] == ["Второй"]


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
