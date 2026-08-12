# Formatta 3.1.1 Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Стабилизировать новый интерфейс Formatta: корректно запускать сборку и самостоятельную проверку по выбранному шаблону, сохранять визуальные правки без потери структуры и grounding, применять спокойную корпоративную палитру.

**Architecture:** Верхний селектор шаблона синхронизируется с отдельными формами сборки и проверки. Элементы визуального редактора связаны с `WorkingDocument.nodes` через `data-node-id`; сервер преобразует очищенный HTML обратно в структурированные узлы, сохраняя смысловые метаданные существующих узлов. HTML хранится как визуальное представление и сбрасывается после сборки или чат-правки. Пользовательский бренд меняется на Formatta, внутренний пакет `docgen` и переменные `DOCGEN_*` сохраняются для совместимости.

**Tech Stack:** FastAPI, SQLAlchemy, SQLite, Pydantic, Jinja2, HTMX, vanilla JavaScript, static CSS, pytest, Ruff.

## Global Constraints

- Этап 4, экспорт и шаблоны оформления не реализуются.
- Самостоятельная проверка одного загруженного файла не требует предварительной сборки или повторного выбора файла.
- Ручное сохранение обновляет `WorkingDocument.nodes`, но не меняет `template_id`, `section_id`, provenance и служебные флаги существующих узлов; новые узлы получают флаг `manual-edit` без вымышленных provenance.
- Чат проверяет факты по нормализованным загруженным источникам.
- В пользовательском интерфейсе используется только название `Formatta`.
- Заголовки внутри листа документа могут быть синими как часть корпоративного оформления; интерфейсные заголовки и тексты остаются графитовыми.
- Верхняя панель — `#15569B`; основной action — `#1B5EAA`; основной текст — `#202428`; вторичный текст — `#5F6873`; фон — `#F4F7FA`; границы — `#D6E5F8`.
- Каждый кодовый пункт выполняется через падающий регрессионный тест.

---

### Task 1: Единый выбранный шаблон и самостоятельная проверка

**Files:**
- Modify: `app/src/docgen/templates/projects/detail.html`
- Modify: `app/src/docgen/templates/projects/source_panel.html`
- Modify: `app/src/docgen/static/js/docgen2-editor.js`
- Test: `app/tests/projects/test_routes.py`
- Test: `app/tests/generation/test_routes.py`

**Interfaces:**
- Consumes: `POST /projects/{project_id}/jobs/assemble` with `template_id`.
- Consumes: `POST /projects/{project_id}/jobs/check` with `template_id` and optional `target_source_id`.
- Produces: hidden `template_id` fields in `assembleForm` and `checkForm`, synchronized from `#templateSelect`.

- [ ] **Step 1: Write failing workspace tests**

```python
def test_workspace_check_is_available_for_one_uploaded_document(client):
    project_url = _project_with_markdown_source(client)
    page = BeautifulSoup(client.get(project_url).text, "html.parser")
    review = page.find("button", id="reviewButton")
    check_form = page.find("form", id="checkForm")

    assert review is not None
    assert review.has_attr("disabled") is False
    assert check_form.find("input", attrs={"name": "template_id"})["value"] == "use-case"
```

```python
def test_workspace_forms_share_selected_template_contract(client):
    project_url = _project_with_markdown_source(client)
    page = BeautifulSoup(client.get(project_url).text, "html.parser")

    assert page.find("select", id="templateSelect")["data-template-source"] == ""
    for form_id in ("assembleForm", "checkForm"):
        field = page.find("form", id=form_id).find("input", attrs={"name": "template_id"})
        assert field["data-template-target"] == ""
```

- [ ] **Step 2: Run the tests and confirm the current failure**

Run: `cd app && .venv/bin/python -m pytest tests/projects/test_routes.py -k 'workspace_check_is_available or workspace_forms_share' -v`

Expected: FAIL because the review button is disabled without a generated document and the forms do not share one selected template.

- [ ] **Step 3: Implement template synchronization and button availability**

In `detail.html`, keep one visible `#templateSelect`, add `data-template-source`, and remove the single-form binding. In both forms in `source_panel.html`, add:

```html
<input type="hidden" name="template_id" value="{{ templates[0].id if templates else '' }}" data-template-target>
```

Set the review button disabled only when `not has_document and not check_targets`. In `docgen2-editor.js`, initialize and update every `[data-template-target]` from `[data-template-source]` on page load and `change`.

- [ ] **Step 4: Run route tests**

Run: `cd app && .venv/bin/python -m pytest tests/projects/test_routes.py tests/generation/test_routes.py -q`

Expected: PASS; the backend continues auto-selecting a sole check target and stores the submitted template ID.

- [ ] **Step 5: Commit**

```bash
git add app/src/docgen/templates/projects/detail.html app/src/docgen/templates/projects/source_panel.html app/src/docgen/static/js/docgen2-editor.js app/tests/projects/test_routes.py app/tests/generation/test_routes.py
git commit -m "fix: stabilize workspace check controls"
```

### Task 2: Структурное сохранение визуального редактора

**Files:**
- Modify: `app/src/docgen/documents/models.py`
- Modify: `app/src/docgen/db.py`
- Modify: `app/src/docgen/documents/repository.py`
- Modify: `app/src/docgen/editor/routes.py`
- Modify: `app/src/docgen/projects/routes.py`
- Modify: `app/src/docgen/templates/projects/work_panel.html`
- Modify: `app/src/docgen/static/js/docgen2-editor.js`
- Test: `app/tests/editor/test_routes.py`
- Test: `app/tests/documents/test_repository.py`

**Interfaces:**
- Produces: nullable `ProjectArtifact.workspace_html`.
- Produces: `DocumentRepository.get_workspace_html(project_id: str) -> str | None`.
- Produces: `workspace_document(current: WorkingDocument, title: str, html: str) -> WorkingDocument`.
- Produces: `DocumentRepository.save_workspace(project_id: str, expected_revision: int, document: WorkingDocument, html: str) -> int | None`.
- Consumes: `Docgen2SavePayload(title: str, html: str, revision: int)`.

- [ ] **Step 1: Write failing repository and route tests**

```python
def test_workspace_save_updates_nodes_and_preserves_semantic_metadata(client, project_with_document):
    original = _stored_document(client, project_with_document.id)
    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Исправленный FAQ",
            "html": '<h2 data-node-id="n1">Новый заголовок</h2>',
            "revision": 1,
        },
    )
    saved = _stored_document(client, project_with_document.id)

    assert response.status_code == 200
    assert saved.title == "Исправленный FAQ"
    assert saved.template_id == original.template_id
    assert saved.nodes[0].text == "Новый заголовок"
    assert saved.nodes[0].section_id == original.nodes[0].section_id
    assert saved.nodes[0].provenance == original.nodes[0].provenance
```

```python
def test_stale_workspace_save_returns_conflict(client, project_with_document):
    first = _save_workspace(client, project_with_document.id, revision=1)
    second = _save_workspace(client, project_with_document.id, revision=1)
    assert first.status_code == 200
    assert second.status_code == 409
```

- [ ] **Step 2: Run the tests and confirm destructive replacement**

Run: `cd app && .venv/bin/python -m pytest tests/editor/test_routes.py -k 'workspace_save_preserves or stale_workspace' -v`

Expected: FAIL because the current route replaces all nodes with `docgen2-workspace` and hard-codes `template_id="use-case"`.

- [ ] **Step 3: Add storage and SQLite migration**

Add `workspace_html: Mapped[str | None] = mapped_column(Text)` to `ProjectArtifact`. In `_migrate_project_artifacts`, execute this migration when absent:

```python
if "workspace_html" not in columns:
    connection.exec_driver_sql(
        "ALTER TABLE project_artifacts ADD COLUMN workspace_html TEXT"
    )
```

Implement repository read/save methods. `save_workspace` atomically verifies `expected_revision`, stores the already validated `WorkingDocument` and `workspace_html`, increments the revision and clears a stale report. It returns `None` if the expected revision is stale. `save_document` and `replace_document` clear `workspace_html`, so assembly and chat never leave a stale visual representation.

- [ ] **Step 4: Adapt route, rendering and client revision**

Add `revision: int = Field(ge=1)` to `Docgen2SavePayload`. Render each semantic node with `data-node-id`, `data-kind` and optional `data-section-id`. Extend the sanitizer to retain only those data attributes. Implement `workspace_document`: recognized IDs reuse the existing node metadata while updating text/list/table content and order; deleted elements remove nodes; new supported elements become new nodes with `flags=["manual-edit"]` and empty provenance. Reject duplicate or unknown claimed node IDs with `422`. Return `409` without committing on a stale revision. Pass `workspace_html` from `project_detail_response` and `editor_view` to `work_panel.html`; add `data-revision` to the editor and update it in JavaScript after a successful save.

- [ ] **Step 5: Run storage and editor tests**

Run: `cd app && .venv/bin/python -m pytest tests/documents/test_repository.py tests/editor/test_routes.py tests/test_stage3_journey.py -q`

Expected: PASS; visual HTML survives reload and its edits are present in the semantic document used by chat and repeat check.

- [ ] **Step 6: Commit**

```bash
git add app/src/docgen/documents app/src/docgen/db.py app/src/docgen/editor/routes.py app/src/docgen/projects/routes.py app/src/docgen/templates/projects/work_panel.html app/src/docgen/static/js/docgen2-editor.js app/tests/documents/test_repository.py app/tests/editor/test_routes.py app/tests/test_stage3_journey.py
git commit -m "fix: preserve semantic document during visual edits"
```

### Task 3: Бренд Formatta и спокойная палитра

**Files:**
- Modify: `app/src/docgen/templates/base.html`
- Modify: `app/src/docgen/templates/projects/index.html`
- Modify: `app/src/docgen/templates/projects/detail.html`
- Modify: `app/src/docgen/templates/projects/work_panel.html`
- Modify: `app/src/docgen/templates/projects/action_panel.html`
- Modify: `app/src/docgen/templates/chat/panel.html`
- Modify: `app/src/docgen/static/css/tailwind-source.css`
- Modify: `app/src/docgen/static/css/docgen.css`
- Test: `app/tests/test_offline_ui.py`
- Test: `app/tests/projects/test_routes.py`

**Interfaces:**
- Produces: user-visible product name `Formatta` only.
- Produces: CSS tokens `--formatta-topbar`, `--formatta-primary`, `--formatta-ink`, `--formatta-muted`, `--formatta-bg`, `--formatta-border`.

- [ ] **Step 1: Write failing brand and palette tests**

```python
def test_workspace_uses_formatta_brand_only(client):
    page = client.get("/projects").text
    assert "Formatta" in page
    assert "DocGen" not in page
```

```python
def test_workspace_css_uses_calm_palette(client):
    css = client.get("/static/css/docgen.css").text.lower()
    for token in ("#15569b", "#1b5eaa", "#202428", "#5f6873", "#f4f7fa", "#d6e5f8"):
        assert token in css
    assert "#60bcff" not in css
    assert "#3196df" not in css
```

- [ ] **Step 2: Run the tests and confirm current brand/color failures**

Run: `cd app && .venv/bin/python -m pytest tests/test_offline_ui.py tests/projects/test_routes.py -k 'formatta_brand_only or calm_palette' -v`

Expected: FAIL because old CSS contains the cyan gradient and some user-facing templates still say DocGen.

- [ ] **Step 3: Apply the approved visual system**

Keep the topbar solid `#15569B`. Set the primary button to `#1B5EAA`, interface headings/body text to `#202428`, muted copy to `#5F6873`, page background to `#F4F7FA`, and borders/soft section headers to `#D6E5F8`. Keep file names, panel headings and chat copy graphite. Allow headings inside `.document-canvas` to remain corporate blue because they belong to the document, not the application chrome. Keep other blue usage limited to links, focus, active indicators and primary actions. Replace user-visible `DocGen`, `DocGen / Formatta` and `DocGen2` labels with `Formatta`; do not rename Python imports, route paths, static filenames or `DOCGEN_*` settings.

- [ ] **Step 4: Update checked-in generated CSS and run UI tests**

Apply identical token changes to `tailwind-source.css` and served `docgen.css`, then run:

Run: `cd app && .venv/bin/python -m pytest tests/test_offline_ui.py tests/projects/test_routes.py -q`

Expected: PASS; markup remains CSP-safe and all user-visible branding is Formatta.

- [ ] **Step 5: Commit**

```bash
git add app/src/docgen/templates app/src/docgen/static/css/tailwind-source.css app/src/docgen/static/css/docgen.css app/tests/test_offline_ui.py app/tests/projects/test_routes.py
git commit -m "style: apply Formatta corporate workspace theme"
```

### Task 4: Сквозная регрессия этапов 1–3

**Files:**
- Modify: `app/tests/test_stage3_journey.py`
- Modify: `app/README.md`

**Interfaces:**
- Consumes: existing project/source, assemble/check, editor and chat routes.
- Produces: one regression journey protecting the stabilized workflow.

- [ ] **Step 1: Add the complete in-process journey**

```python
def test_formatta_workspace_preserves_template_through_edit_and_recheck(client):
    # Create document, upload one Markdown source, persist a generated FAQ fixture,
    # save visual HTML with the current revision, perform a grounded chat edit,
    # enqueue check with template_id="faq", and assert the current document still
    # has template_id="faq" and its original provenance-bearing nodes.
```

- [ ] **Step 2: Run the journey**

Run: `cd app && .venv/bin/python -m pytest tests/test_stage3_journey.py -q`

Expected: PASS without external network access.

- [ ] **Step 3: Update acceptance instructions**

In `app/README.md`, title the product Formatta while retaining documented internal commands `docgen.main`, `docgen.jobs.worker` and `DOCGEN_*`. Document the manual order: upload source, select FAQ, assemble, save visual edit, edit through chat, check current document, then check the uploaded source.

- [ ] **Step 4: Run final verification**

Run: `cd app && .venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && git diff --check`

Expected: all tests pass; Ruff prints `All checks passed!`; `git diff --check` prints nothing.

- [ ] **Step 5: Commit**

```bash
git add app/tests/test_stage3_journey.py app/README.md
git commit -m "test: cover stabilized Formatta workflow"
```
