# DocGen Stage 3 Editor and Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Позволить пользователю безопасно редактировать структурированный рабочий документ вручную и через русскоязычный чат, автоматически сохраняя текущее состояние.

**Architecture:** Редактор работает на уровне блоков `DocumentNode` и отправляет небольшие HTMX-команды серверному `DocumentEditService`. Каждое изменение проверяется по схеме и сохраняется с монотонным номером ревизии для защиты от перезаписи, но предыдущие состояния не хранятся. Чат преобразует запрос пользователя в типизированный список операций над блоками, проверяет происхождение новых фактов и применяет операции тем же сервисом, что и ручной редактор.

**Tech Stack:** стек этапов 1–2, серверный HTMX-интерфейс, Pydantic-модели операций, локальный OpenAI-совместимый текстовый endpoint, pytest.

## Global Constraints

- История версий и откат не создаются; `revision` служит только для обнаружения устаревшего запроса.
- Ручной редактор позволяет пользователю вводить собственный текст.
- Чат может использовать только текущий документ и источники проекта; неподтверждённые сведения отклоняются.
- Все команды, ошибки и результаты — на русском языке.
- После каждой успешной ручной или чат-правки текущее состояние автоматически сохраняется.
- Редактор поддерживает заголовки, абзацы, списки, таблицы, изображения, схемы и перестановку блоков.
- Один чат-запрос применяется атомарно: либо все операции сохранены, либо документ не меняется.
- Экспорт и визуальные шаблоны остаются за границами этапа 3.

## File Structure

```text
Проекты/DocGen/app/src/docgen/
├── documents/
│   ├── operations.py              # типизированные операции редактора
│   ├── edit_service.py            # атомарное применение и revision check
│   └── repository.py
├── editor/
│   ├── routes.py
│   └── validation.py
├── chat/
│   ├── schemas.py
│   ├── service.py
│   └── routes.py
└── templates/
    ├── editor/{document,node,conflict,error}.html
    └── chat/{panel,message,error}.html

Проекты/DocGen/app/tests/
├── documents/test_edit_service.py
├── editor/test_routes.py
├── chat/test_service.py
├── chat/test_routes.py
└── test_stage3_journey.py
```

---

### Task 1: Add atomic document operations and revision checks

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/documents/operations.py`
- Create: `Проекты/DocGen/app/src/docgen/documents/edit_service.py`
- Modify: `Проекты/DocGen/app/src/docgen/documents/models.py`
- Modify: `Проекты/DocGen/app/src/docgen/documents/repository.py`
- Create: `Проекты/DocGen/app/tests/documents/test_edit_service.py`

**Interfaces:**
- Produces: `UpdateText(node_id: str, text: str)`, `InsertNode(parent_id: str | None, index: int, node: DocumentNode)`, `DeleteNode(node_id: str)`, `MoveNode(node_id: str, parent_id: str | None, index: int)`, `UpdateData(node_id: str, data: dict)`
- Produces: union `DocumentOperation`
- Produces: `EditResult(document: WorkingDocument, revision: int)`
- Produces: `find_node(document: WorkingDocument, node_id: str) -> DocumentNode | None`
- Produces: `DocumentEditService.apply(project_id: str, expected_revision: int, operations: list[DocumentOperation]) -> EditResult`

- [ ] **Step 1: Write failing atomicity and conflict tests**

```python
def test_apply_updates_text_and_increments_revision(edit_service, saved_document):
    result = edit_service.apply("p1", 3, [UpdateText(node_id="n1", text="Новый текст")])
    assert find_node(result.document, "n1").text == "Новый текст"
    assert result.revision == 4


def test_invalid_second_operation_rolls_back_first(edit_service, saved_document):
    with pytest.raises(EditValidationError, match="Блок missing не найден"):
        edit_service.apply("p1", 3, [UpdateText(node_id="n1", text="Изменено"), DeleteNode(node_id="missing")])
    assert document_repository.get_document("p1").document == saved_document


def test_stale_revision_is_rejected(edit_service):
    with pytest.raises(EditConflict, match="Документ уже изменён"):
        edit_service.apply("p1", 2, [UpdateText(node_id="n1", text="Поздняя правка")])
```

- [ ] **Step 2: Run tests and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/documents/test_edit_service.py -v`

Expected: FAIL because operation models do not exist.

- [ ] **Step 3: Add current revision to the artifact**

Add non-null integer `revision` defaulting to 1. `DocumentRepository.get_document` returns `StoredDocument(document, revision)`. `replace_document(project_id, expected_revision, document)` performs one conditional update `WHERE revision = expected_revision`, increments it, and raises `EditConflict` when zero rows change.

- [ ] **Step 4: Implement pure operation application**

Deep-copy the Pydantic document, index all nodes recursively, validate every operation and resulting schema before persistence. Reject deleting the last top-level node, moving a node into its own descendant, blank heading text, negative indices, invalid table dimensions, and duplicate node IDs.

```python
def apply_operations(document: WorkingDocument, operations: list[DocumentOperation]) -> WorkingDocument:
    candidate = document.model_copy(deep=True)
    for operation in operations:
        candidate = apply_one(candidate, operation)
    validate_tree(candidate)
    return candidate
```

- [ ] **Step 5: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/documents -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/src/docgen/documents Проекты/DocGen/app/tests/documents
git commit -m "feat: add atomic document editing"
```

### Task 2: Render the editable block document

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/editor/routes.py`
- Create: `Проекты/DocGen/app/src/docgen/templates/editor/document.html`
- Create: `Проекты/DocGen/app/src/docgen/templates/editor/node.html`
- Create: `Проекты/DocGen/app/src/docgen/templates/editor/error.html`
- Modify: `Проекты/DocGen/app/src/docgen/main.py`
- Create: `Проекты/DocGen/app/tests/editor/test_routes.py`

**Interfaces:**
- Produces: `GET /projects/{project_id}/editor`
- Produces: `PATCH /projects/{project_id}/editor/nodes/{node_id}/text`
- Produces: recursive `editor/node.html` fragment for every `NodeKind`

- [ ] **Step 1: Write failing render and text-edit tests**

```python
def test_editor_renders_all_node_kinds(client, project_with_document):
    response = client.get(f"/projects/{project_with_document.id}/editor")
    assert response.status_code == 200
    for marker in ["data-kind=\"heading\"", "data-kind=\"paragraph\"", "data-kind=\"list\"", "data-kind=\"table\"", "data-kind=\"image\"", "data-kind=\"gap\""]:
        assert marker in response.text


def test_text_autosave_returns_new_revision(client, project_with_document):
    response = client.patch(
        f"/projects/{project_with_document.id}/editor/nodes/n1/text",
        data={"text": "Исправленный текст", "revision": "1"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert 'data-revision="2"' in response.text
```

- [ ] **Step 2: Run tests and verify 404**

Run: `cd Проекты/DocGen/app && python -m pytest tests/editor/test_routes.py -v`

Expected: FAIL with 404.

- [ ] **Step 3: Implement recursive rendering and text edits**

Render each node with `data-node-id`, `data-kind`, current revision and controls appropriate to its kind. Text fields use `hx-patch`, `hx-trigger="change delay:700ms"`, and target only their node fragment. Map validation errors to 422 with the original value and Russian inline error.

- [ ] **Step 4: Add a conflict response**

Create `conflict.html` with `Документ уже изменён. Обновите рабочую область.` and an HTMX button that reloads the complete editor. Return HTTP 409 for `EditConflict`; do not silently overwrite.

- [ ] **Step 5: Register routes, run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/editor/test_routes.py -v && python -m ruff check .`

Expected: PASS; no Ruff errors.

```bash
git add Проекты/DocGen/app/src/docgen/editor Проекты/DocGen/app/src/docgen/templates/editor Проекты/DocGen/app/src/docgen/main.py Проекты/DocGen/app/tests/editor
git commit -m "feat: render editable DocGen documents"
```

### Task 3: Support block creation, deletion and movement

**Files:**
- Modify: `Проекты/DocGen/app/src/docgen/editor/routes.py`
- Modify: `Проекты/DocGen/app/src/docgen/templates/editor/document.html`
- Modify: `Проекты/DocGen/app/src/docgen/templates/editor/node.html`
- Modify: `Проекты/DocGen/app/tests/editor/test_routes.py`

**Interfaces:**
- Produces: `POST /projects/{id}/editor/nodes`
- Produces: `DELETE /projects/{id}/editor/nodes/{node_id}`
- Produces: `POST /projects/{id}/editor/nodes/{node_id}/move`

- [ ] **Step 1: Add failing endpoint tests**

```python
def test_insert_paragraph_after_selected_node(client, project_with_document):
    response = client.post(f"/projects/{project_with_document.id}/editor/nodes", data={"kind": "paragraph", "after_node_id": "n1", "revision": 1})
    assert response.status_code == 200
    assert "Новый абзац" in response.text


def test_move_node_down_persists_order(client, project_with_document):
    response = client.post(f"/projects/{project_with_document.id}/editor/nodes/n1/move", data={"direction": "down", "revision": 1})
    assert response.status_code == 200
    assert document_repository.get_document(project_with_document.id).document.nodes[1].id == "n1"
```

- [ ] **Step 2: Run and verify route failures**

Run: `cd Проекты/DocGen/app && python -m pytest tests/editor/test_routes.py -v`

Expected: new tests FAIL with 404 or 405.

- [ ] **Step 3: Implement explicit block commands**

Allow insertion of heading, paragraph, list and table from the toolbar. New nodes use UUID IDs and Russian defaults (`Новый раздел`, `Новый абзац`, one empty list item, 2×2 empty table). Image insertion is not exposed because images must already come from sources. Movement accepts only `up` and `down`; server calculates the target index. Render the complete document after structural changes.

- [ ] **Step 4: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/editor -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/src/docgen/editor Проекты/DocGen/app/src/docgen/templates/editor Проекты/DocGen/app/tests/editor
git commit -m "feat: manage DocGen document blocks"
```

### Task 4: Edit lists, tables and image placement

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/editor/validation.py`
- Modify: `Проекты/DocGen/app/src/docgen/editor/routes.py`
- Modify: `Проекты/DocGen/app/src/docgen/templates/editor/node.html`
- Create: `Проекты/DocGen/app/tests/editor/test_complex_nodes.py`

**Interfaces:**
- Produces: `PATCH /projects/{id}/editor/nodes/{node_id}/list`
- Produces: `PATCH /projects/{id}/editor/nodes/{node_id}/table`
- Produces: `PATCH /projects/{id}/editor/nodes/{node_id}/image`

- [ ] **Step 1: Write failing complex-node tests**

```python
def test_table_rejects_ragged_rows(client, project_with_table):
    response = client.patch(
        f"/projects/{project_with_table.id}/editor/nodes/table-1/table",
        json={"revision": 1, "rows": [["A", "B"], ["C"]]},
    )
    assert response.status_code == 422
    assert "Все строки таблицы должны иметь одинаковое число ячеек" in response.text


def test_image_alignment_is_limited(client, project_with_image):
    response = client.patch(
        f"/projects/{project_with_image.id}/editor/nodes/image-1/image",
        json={"revision": 1, "alignment": "outside"},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/editor/test_complex_nodes.py -v`

Expected: FAIL because endpoints do not exist.

- [ ] **Step 3: Implement typed validators and forms**

Lists contain 1–100 string items. Tables contain 1–100 rows and 1–20 columns with equal row lengths. Image alignment is `left`, `center` or `right`; width is integer 10–100 percent; alt text is required. Convert validated payloads into `UpdateData` operations and use the same revision conflict handling as text edits.

- [ ] **Step 4: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/editor -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/src/docgen/editor Проекты/DocGen/app/src/docgen/templates/editor Проекты/DocGen/app/tests/editor
git commit -m "feat: edit complex DocGen blocks"
```

### Task 5: Plan grounded chat edits

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/chat/schemas.py`
- Create: `Проекты/DocGen/app/src/docgen/chat/service.py`
- Create: `Проекты/DocGen/app/tests/chat/test_service.py`

**Interfaces:**
- Produces: `ChatEditRequest(message: str, expected_revision: int)`
- Produces: `ChatEditPlan(summary: str, operations: list[DocumentOperation], evidence_block_ids: list[str])`
- Produces: `ChatEditResult(summary: str, document: WorkingDocument, revision: int)`
- Produces: `ChatService.edit(project_id: str, request: ChatEditRequest) -> ChatEditResult`

- [ ] **Step 1: Write failing grounded-chat tests**

```python
def test_chat_applies_grounded_plan(chat_service, fake_model):
    fake_model.result = ChatEditPlan(summary="Уточнён актор", operations=[UpdateText(node_id="actor", text="Оператор")], evidence_block_ids=["s1:b2"])
    result = chat_service.edit("p1", ChatEditRequest(message="Уточни актора", expected_revision=2))
    assert result.revision == 3
    assert find_node(result.document, "actor").text == "Оператор"


def test_chat_rejects_unknown_evidence(chat_service, fake_model):
    fake_model.result = ChatEditPlan(summary="Добавлен лимит", operations=[UpdateText(node_id="limit", text="10 000")], evidence_block_ids=["unknown"])
    with pytest.raises(ChatGroundingError, match="Для этой правки нет подтверждения в источниках"):
        chat_service.edit("p1", ChatEditRequest(message="Добавь лимит", expected_revision=2))
```

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/chat/test_service.py -v`

Expected: FAIL because chat service does not exist.

- [ ] **Step 3: Implement the chat planning prompt**

Send the current document, normalized source blocks and user request to `TextModel.generate_json(system=CHAT_SYSTEM_PROMPT, user=serialized_context, schema=ChatEditPlan)`. The system prompt requires Russian output, operations only against existing node IDs unless inserting, evidence IDs for every factual addition, no deletion beyond the user's request, and an empty operation list when evidence is missing.

- [ ] **Step 4: Validate and atomically apply the plan**

Reject blank messages, more than 4000 characters, unknown evidence block IDs, unknown node IDs, duplicate inserted IDs and unsupported operation types. Apply the complete list through `DocumentEditService.apply`; do not retry on revision conflict or validation failure.

- [ ] **Step 5: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/chat/test_service.py -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/src/docgen/chat Проекты/DocGen/app/tests/chat
git commit -m "feat: plan grounded DocGen chat edits"
```

### Task 6: Add the chat panel and post-edit recheck

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/chat/routes.py`
- Create: `Проекты/DocGen/app/src/docgen/templates/chat/panel.html`
- Create: `Проекты/DocGen/app/src/docgen/templates/chat/message.html`
- Create: `Проекты/DocGen/app/src/docgen/templates/chat/error.html`
- Modify: `Проекты/DocGen/app/src/docgen/templates/editor/document.html`
- Modify: `Проекты/DocGen/app/src/docgen/main.py`
- Modify: `Проекты/DocGen/app/src/docgen/generation/routes.py`
- Create: `Проекты/DocGen/app/tests/chat/test_routes.py`

**Interfaces:**
- Produces: `POST /projects/{id}/chat`
- Consumes: existing `POST /projects/{id}/jobs/check` against current working document

- [ ] **Step 1: Write failing chat route tests**

```python
def test_chat_returns_message_and_refreshes_document(client, project_with_document, fake_chat):
    response = client.post(f"/projects/{project_with_document.id}/chat", data={"message": "Сделай заголовок короче", "revision": 1})
    assert response.status_code == 200
    assert "Заголовок сокращён" in response.text
    assert "HX-Trigger" in response.headers


def test_chat_grounding_error_preserves_document(client, project_with_document, fake_chat):
    response = client.post(f"/projects/{project_with_document.id}/chat", data={"message": "Добавь неподтверждённый факт", "revision": 1})
    assert response.status_code == 422
    assert "нет подтверждения в источниках" in response.text
```

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/chat/test_routes.py -v`

Expected: FAIL with 404.

- [ ] **Step 3: Implement the chat panel**

Render one input, submit button, request indicator and session-only visible message list. Do not persist chat history. On success return the assistant summary and `HX-Trigger: {"docgen:document-updated": {"revision": <n>}}`; the editor listens and reloads. Map grounding/validation to 422, conflicts to 409 and local-model failure to 503.

- [ ] **Step 4: Enable repeat checking after edits**

The existing check route must load the current saved `WorkingDocument`, not re-read the originally uploaded document. A new completed check replaces the current report and associates it with the document revision it checked; the UI labels older reports `Документ изменён после проверки`.

- [ ] **Step 5: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/chat tests/generation -v && python -m ruff check .`

Expected: PASS; no Ruff errors.

```bash
git add Проекты/DocGen/app/src/docgen/chat Проекты/DocGen/app/src/docgen/templates/chat Проекты/DocGen/app/src/docgen/templates/editor/document.html Проекты/DocGen/app/src/docgen/main.py Проекты/DocGen/app/src/docgen/generation/routes.py Проекты/DocGen/app/tests
git commit -m "feat: add DocGen chat editing"
```

### Task 7: Verify the complete Stage 3 journey

**Files:**
- Create: `Проекты/DocGen/app/tests/test_stage3_journey.py`
- Modify: `Проекты/DocGen/app/README.md`

**Interfaces:**
- Consumes: editor, chat, persistence and checking interfaces from stages 1–3

- [ ] **Step 1: Write the integration journey**

```python
def test_edit_chat_save_restart_and_recheck(app_factory, seeded_document, fake_model):
    client = app_factory()
    client.patch(TEXT_URL, data={"text": "Ручная правка", "revision": 1})
    client.post(CHAT_URL, data={"message": "Уточни результат по источнику", "revision": 2})
    client.close()

    restarted = app_factory()
    editor = restarted.get(EDITOR_URL)
    assert "Ручная правка" in editor.text
    assert "Уточнённый результат" in editor.text
    response = restarted.post(CHECK_URL, data={"template_id": "use-case"})
    assert response.status_code == 202
```

- [ ] **Step 2: Run the Stage 3 journey**

Run: `cd Проекты/DocGen/app && python -m pytest tests/test_stage3_journey.py -v`

Expected: PASS without external network access.

- [ ] **Step 3: Document editor and chat constraints**

Add to README: supported node types, autosave behavior, 409 conflict recovery, lack of history/chat persistence, grounding rules, and the repeated-check workflow.

- [ ] **Step 4: Run complete verification and commit**

Run: `cd Проекты/DocGen/app && python -m pytest -v && python -m ruff check .`

Expected: PASS; no Ruff errors.

```bash
git add Проекты/DocGen/app/tests/test_stage3_journey.py Проекты/DocGen/app/README.md
git commit -m "test: verify DocGen editing journey"
```
