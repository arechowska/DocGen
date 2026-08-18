# Positioned Manual Chat Insertion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic chat commands that insert an ungrounded, user-authored paragraph at the start or end of a document or before/after a visually numbered text block.

**Architecture:** Move manual-command parsing and visual-position resolution into a focused `docgen.chat.manual_insert` module. Explicitly positioned commands bypass the language model and produce document operations directly; generic manual additions retain the existing model-first fallback. Visual numbering walks headings, paragraphs, and individual list items in render order, splitting list nodes when a paragraph must be inserted between list items.

**Tech Stack:** Python 3, Pydantic document models, existing `DocumentOperation` types, SQLAlchemy-backed service tests, pytest.

## Global Constraints

- Inserted user content is a new `paragraph` node and never changes the target block's text.
- Inserted nodes carry `flags=["manual-edit"]` and bypass source-grounding validation.
- Count heading nodes, paragraph nodes, and each list item as visual paragraphs; exclude tables, images, gaps, and empty non-text blocks.
- Support arbitrary positive numeric ordinals and Russian ordinal words from first through twentieth in genitive and instrumental forms.
- Split `items`, `items_html`, and `item_styles` together; copy all other list metadata to both list segments.
- Return a validation error and preserve the revision when an explicit visual target does not exist or authored text is empty.
- Add no third-party dependency.

---

### Task 1: Parse Manual Insertion Commands

**Files:**
- Create: `app/src/docgen/chat/manual_insert.py`
- Create: `app/tests/chat/test_manual_insert.py`

**Interfaces:**
- Produces: `InsertAnchor(str, Enum)` with `DOCUMENT_START`, `DOCUMENT_END`, `BEFORE_VISUAL`, and `AFTER_VISUAL`.
- Produces: frozen `ManualInsertIntent(text: str, anchor: InsertAnchor, ordinal: int | None, explicit_position: bool)`.
- Produces: `ManualInsertError(ValueError)` for malformed explicit manual commands.
- Produces: `parse_manual_insert(message: str) -> ManualInsertIntent | None`.
- Consumes: no application service state.

- [ ] **Step 1: Write failing parser tests**

Create table-driven tests that establish the complete command grammar:

```python
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
```

Cover every supported word form with this table:

```python
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
```

- [ ] **Step 2: Run parser tests and confirm RED**

Run:

```powershell
cd app
python -m pytest tests/chat/test_manual_insert.py -q
```

Expected: collection fails because `docgen.chat.manual_insert` does not exist.

- [ ] **Step 3: Implement the command parser**

Create `manual_insert.py` with the public types above and private helpers:

```python
_MANUAL_COMMAND_PATTERN = re.compile(
    r"^\s*(?:добавь|добавить|вставь|вставить|допиши|дописать)\b(?P<body>.*?)\s*$",
    re.IGNORECASE | re.DOTALL,
)

_ORDINALS = {
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
```

Implement position patterns in precedence order: document start/end, before/after visual paragraph, then unpositioned text. Normalize `в начало N` to `BEFORE_VISUAL` and `в конец N` to `AFTER_VISUAL`. Accept numeric forms `N`, `N-й`, `N-ый`, and `N-го`, reject values below 1, strip only the separator punctuation between position and authored text, then apply the existing question/answer normalization to the authored portion.

Use this concrete normalization contract:

```python
def _normalize_authored_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip(" \t\r\n:;,.—-")
    question_answer = _QUESTION_ANSWER_PATTERN.search(text)
    if question_answer is None:
        return text
    question = _clean_fragment(question_answer.group("question"))
    answer = _clean_fragment(question_answer.group("answer"))
    return f"Вопрос: {question}\nОтвет: {answer}" if question and answer else text
```

Raise `ManualInsertError("Укажите текст для добавления")` for a recognized command with no authored text and `ManualInsertError("Номер абзаца должен быть больше нуля")` for zero-valued ordinals.

- [ ] **Step 4: Run parser tests and confirm GREEN**

Run:

```powershell
cd app
python -m pytest tests/chat/test_manual_insert.py -q
```

Expected: all parser tests pass.

- [ ] **Step 5: Commit parser module and tests**

```powershell
git add app/src/docgen/chat/manual_insert.py app/tests/chat/test_manual_insert.py
git commit -m "feat: parse positioned manual chat inserts"
```

---

### Task 2: Resolve Visual Positions and Split Lists

**Files:**
- Modify: `app/src/docgen/chat/manual_insert.py`
- Modify: `app/tests/chat/test_manual_insert.py`

**Interfaces:**
- Consumes: `ManualInsertIntent` from Task 1 and `WorkingDocument`.
- Produces: `ManualInsertTargetError(ManualInsertError)`.
- Produces: `manual_insert_operations(document: WorkingDocument, intent: ManualInsertIntent) -> list[DocumentOperation]`.
- Uses: `InsertNode`, `DeleteNode`, `UpdateData`, and `apply_operations` from the existing document operation layer.

- [ ] **Step 1: Write failing tests for document and ordinary-node boundaries**

Build a document containing a heading, paragraph, table, nested paragraph, and gap. Assert against the result of `apply_operations(document, manual_insert_operations(document, intent))`:

```python
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
```

Use these additional assertions for document boundaries and nested/excluded nodes:

```python
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
```

- [ ] **Step 2: Run ordinary-position tests and confirm RED**

Run the new test names directly:

```powershell
cd app
python -m pytest tests/chat/test_manual_insert.py -k "document or visual or nested" -q
```

Expected: failure because `manual_insert_operations` and `ManualInsertTargetError` are not implemented.

- [ ] **Step 3: Implement visual traversal and ordinary insertion**

Add a private frozen locator:

```python
@dataclass(frozen=True)
class _VisualTarget:
    parent_id: str | None
    node_index: int
    node: DocumentNode
    list_item_index: int | None = None
```

Implement depth-first render-order traversal. A non-empty heading or paragraph yields one `_VisualTarget`; a list yields one target per string item in `data["items"]`; all node children are visited after their parent content. Tables, images, and gaps yield no target but their children are still traversed.

For document anchors, create one `InsertNode(parent_id=None, index=0 or len(document.nodes), node=_manual_paragraph(intent.text))`. For ordinary targets, use the locator's `parent_id` and `node_index` or `node_index + 1`. Raise `ManualInsertTargetError(f"Абзац {intent.ordinal} не найден")` when the ordinal exceeds the visual target count.

- [ ] **Step 4: Run ordinary-position tests and confirm GREEN**

```powershell
cd app
python -m pytest tests/chat/test_manual_insert.py -k "document or visual or nested" -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Write failing tests for individual list-item positions**

Use a list with aligned metadata:

```python
list_node = DocumentNode(
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
```

Use one parameterized split test and one boundary test:

```python
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
    document = WorkingDocument(title="Документ", template_id="use-case", nodes=[list_node])
    intent = ManualInsertIntent("Новый", anchor, ordinal, True)

    result = apply_operations(document, manual_insert_operations(document, intent))

    assert [node.kind for node in result.nodes] == expected_kinds
    preserved = next(node for node in result.nodes if node.kind is NodeKind.LIST)
    assert preserved.data == list_node.data
```

Prove list items increment mixed visual order independently:

```python
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
```

- [ ] **Step 6: Run list tests and confirm RED**

```powershell
cd app
python -m pytest tests/chat/test_manual_insert.py -k "list" -q
```

Expected: internal list insertion is misplaced or unsupported.

- [ ] **Step 7: Implement metadata-preserving list splitting**

For boundary `0`, insert before the list node. For boundary `len(items)`, insert after it. For an internal boundary, return operations in this exact order:

```python
[
    InsertNode(parent_id=target.parent_id, index=target.node_index + 1, node=paragraph),
    DeleteNode(node_id=target.node.id),
    InsertNode(parent_id=target.parent_id, index=target.node_index, node=left_list),
    InsertNode(parent_id=target.parent_id, index=target.node_index + 2, node=right_list),
]
```

In `_split_list_node`, slice only list-valued `items`, `items_html`, and `item_styles`; copy all other `data` keys. Preserve the original list ID and `section_id` on the left segment, assign a new `manual-split-<uuid>` ID to the right segment, keep original children only on the right segment, and give the left segment no children. This operation ordering supports splitting a document whose only top-level node is the list because the paragraph is inserted before the original list is deleted.

- [ ] **Step 8: Run the complete resolver test file and confirm GREEN**

```powershell
cd app
python -m pytest tests/chat/test_manual_insert.py -q
```

Expected: all parser, ordinary-position, and list-splitting tests pass.

- [ ] **Step 9: Commit resolver and list splitting**

```powershell
git add app/src/docgen/chat/manual_insert.py app/tests/chat/test_manual_insert.py
git commit -m "feat: resolve visual chat insertion positions"
```

---

### Task 3: Integrate Deterministic Positioning Into ChatService

**Files:**
- Modify: `app/src/docgen/chat/service.py`
- Modify: `app/tests/chat/test_service.py`

**Interfaces:**
- Consumes: `parse_manual_insert` and `manual_insert_operations` from Tasks 1-2.
- Preserves: `ChatService.edit(project_id: str, request: ChatEditRequest) -> ChatEditResult`.
- Maps: `ManualInsertError` to existing `ChatValidationError` so the route returns HTTP 422.

- [ ] **Step 1: Write failing service tests for deterministic execution**

Extend `FakeModel` with `calls = 0`, incremented by `generate_json`. Add service tests asserting:

```python
def test_chat_positioned_manual_insert_bypasses_model(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    result = chat_service.edit(
        "p1",
        ChatEditRequest(
            message="допиши перед вторым абзацем: Новый текст",
            expected_revision=2,
        ),
    )

    assert fake_model.calls == 0
    assert [node.text for node in result.document.nodes] == [
        "Оператор",
        "Новый текст",
        "Лимит",
    ]
    assert result.summary == "Добавлен текст пользователя"
```

Add the missing-target and unpositioned-fallback assertions explicitly:

```python
def test_chat_missing_visual_target_preserves_document(
    chat_service: ChatService,
    fake_model: FakeModel,
    session: Session,
) -> None:
    with pytest.raises(ChatValidationError, match="Абзац 9 не найден"):
        chat_service.edit(
            "p1",
            ChatEditRequest(
                message="допиши после девятого абзаца: Новый текст",
                expected_revision=2,
            ),
        )

    stored = DocumentRepository(session).get_document_with_revision("p1")
    assert stored is not None
    document, revision = stored
    assert revision == 2
    assert [node.text for node in document.nodes] == ["Оператор", "Лимит"]
    assert fake_model.calls == 0


def test_chat_unpositioned_manual_insert_keeps_model_first_fallback(
    chat_service: ChatService,
    fake_model: FakeModel,
) -> None:
    fake_model.result = ChatEditPlan(summary="Нет правок", operations=[])

    result = chat_service.edit(
        "p1",
        ChatEditRequest(message="добавь авторский текст", expected_revision=2),
    )

    assert fake_model.calls == 1
    assert result.document.nodes[-1].text == "авторский текст"
```

Keep the existing `ModelError` fallback test for unpositioned manual insertion unchanged.

- [ ] **Step 2: Run focused service tests and confirm RED**

```powershell
cd app
python -m pytest tests/chat/test_service.py -k "positioned_manual or document_start or manual_paragraph" -q
```

Expected: positioned commands call the model or use the old top-level-only insertion logic.

- [ ] **Step 3: Integrate the manual insertion module**

At the start of `ChatService.edit`, after revision validation:

```python
try:
    manual_intent = parse_manual_insert(message)
    manual_operations = (
        manual_insert_operations(document, manual_intent)
        if manual_intent is not None
        else []
    )
except ManualInsertError as error:
    raise ChatValidationError(str(error)) from error

if manual_intent is not None and manual_intent.explicit_position:
    return self._apply_manual_insert(
        project_id,
        request.expected_revision,
        manual_operations,
    )
```

Extract `_apply_manual_insert` as a private `ChatService` method returning `ChatEditResult` with summary `Добавлен текст пользователя`. Reuse it for the `ModelError` and noop-plan fallbacks. Remove `_MANUAL_ADD_PATTERN`, `_QUESTION_ANSWER_PATTERN`, `_START_POSITION_PATTERN`, `_manual_text_insertion_operations`, `_manual_insert_text_and_index`, and `_clean_manual_text` from `service.py` after all callers use the new module.

- [ ] **Step 4: Run service tests and confirm GREEN**

```powershell
cd app
python -m pytest tests/chat/test_service.py -q
```

Expected: all service tests pass, including existing grounding and formatting behavior. The existing route catches `ChatValidationError`, rolls back the session, and renders its message with HTTP 422, so no route change is required.

- [ ] **Step 5: Run all chat tests and confirm GREEN**

```powershell
cd app
python -m pytest tests/chat -q
```

Expected: zero failures across parser, service, schema, route, and out-of-band refresh tests. A pre-existing pytest cache permission warning may remain, but no application warning or traceback is acceptable.

- [ ] **Step 6: Commit service integration**

```powershell
git add app/src/docgen/chat/service.py app/tests/chat/test_service.py
git commit -m "feat: insert chat text at visual positions"
```

---

### Task 4: Final Regression Verification

**Files:**
- Verify only; no planned production edits.

**Interfaces:**
- Verifies the complete chat editing surface and document operation behavior.

- [ ] **Step 1: Run focused document operation tests**

```powershell
cd app
python -m pytest tests/documents tests/editor/test_routes.py -q
```

Expected: zero failures; list rendering and persisted editor data remain compatible with split list nodes.

- [ ] **Step 2: Run the complete chat suite again from a clean test process**

```powershell
cd app
python -m pytest tests/chat -q
```

Expected: zero failures.

- [ ] **Step 3: Inspect the scoped diff**

```powershell
git diff --check
git status --short
```

Confirm that feature changes are limited to the manual insertion module, chat service, related tests, this plan, and the already committed design specification. Preserve unrelated user changes in the dirty worktree.
