# No-Template HTML Editor Build Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать первое нажатие «Собрать» для «Без шаблона» + HTML однократным импортом источника, а последующие нажатия — сохранением редактора и экспортом его точной ревизии с кнопками «Открыть» и «Скачать».

**Architecture:** Существующий маршрут импорта остаётся единственной точкой чтения источника и используется только пока рабочего документа нет. После появления документа кнопка «Собрать» отправляется в существующую форму экспорта; JavaScript перехватывает это действие, сохраняет редактор, обновляет ревизию и отдельным событием запускает штатный экспорт. Сервер связывает результат с запрошенной ревизией и условно показывает вторую кнопку только для HTML-документа без смыслового шаблона.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Jinja2, HTMX, browser JavaScript, pytest, BeautifulSoup, Node.js test harness.

## Global Constraints

- Изменять только сочетание смыслового шаблона «Без шаблона» и формата HTML.
- Первый запуск требует ровно один источник и импортирует его только один раз.
- Повторный запуск не читает источник и экспортирует сохранённую ревизию редактора.
- Повторный запуск сохраняет несохранённые изменения редактора до постановки экспорта в очередь.
- Обычное «Сохранить в проект» не должно само запускать HTML-сборку в целевом сценарии.
- HTML формируется существующим экспортным конвейером и шаблоном страницы версии 5; код HTML-экспортёра не меняется.
- Для целевого результата одновременно доступны «Открыть» и «Скачать»; остальные панели результатов не меняются.
- При ошибке импорта, сохранения или экспорта нельзя показывать устаревший файл как результат новой ревизии.

## File Structure

- `app/src/docgen/templates/projects/detail.html` — передаёт выбранный смысловой шаблон при загрузке вариантов оформления.
- `app/src/docgen/templates/projects/source_panel.html` — содержит отдельную форму первоначального импорта.
- `app/src/docgen/templates/projects/build_button.html` — сообщает JavaScript, существует ли рабочий документ.
- `app/src/docgen/templates/export/template_options.html` — выбирает автоматические либо явные события запуска экспорта.
- `app/src/docgen/templates/export/download_button.html` — показывает одну или две кнопки результата по серверному флагу.
- `app/src/docgen/static/js/docgen2-editor.js` — маршрутизирует «Собрать», сохраняет редактор и запускает экспорт после успешного сохранения.
- `app/src/docgen/export/routes.py` — принимает смысловой шаблон для настройки триггеров и вычисляет признак целевого HTML-результата по точной ревизии.
- `app/tests/projects/test_routes.py` — проверяет серверную разметку страницы проекта.
- `app/tests/export/test_routes.py` — проверяет условные триггеры и ссылки результата.
- `app/tests/editor/test_routes.py` — проверяет цепочку импорт → правка → экспорт новой ревизии.
- `app/tests/test_offline_ui.py` — исполняет клиентскую маршрутизацию и save-before-build в Node.js.

---

### Task 1: Conditional export triggers for explicit HTML builds

**Files:**
- Modify: `app/src/docgen/templates/projects/detail.html:17-55`
- Modify: `app/src/docgen/templates/export/template_options.html:1-18`
- Modify: `app/src/docgen/export/routes.py:66-80`
- Test: `app/tests/projects/test_routes.py`
- Test: `app/tests/export/test_routes.py`

**Interfaces:**
- Consumes: `NO_TEMPLATE_ID == "no-template"`, `OutputFormat.HTML`, current `GET /projects/{project_id}/export/templates`.
- Produces: optional query parameter `semantic_template_id: str | None` and Jinja flag `manual_html_build: bool`.

- [ ] **Step 1: Write failing route and markup tests**

Add assertions that the semantic selector participates in the formatting-template request:

```python
template_select = soup.find(id="templateSelect")
assert template_select["name"] == "semantic_template_id"
format_select = soup.find(id="formatSelect")
assert format_select["hx-include"] == "#templateSelect"
```

Add two focused tests to `app/tests/export/test_routes.py`:

```python
def test_no_template_html_options_wait_for_explicit_build(client, project_with_document):
    response = client.get(
        f"/projects/{project_with_document.id}/export/templates",
        params={"format": "html", "semantic_template_id": "no-template"},
    )
    select = BeautifulSoup(response.text, "html.parser").find("select")
    trigger = select["hx-trigger"]
    assert "docgen:html-build from:body" in trigger
    assert "docgen:document-updated from:body" not in trigger


def test_other_export_options_keep_document_update_trigger(client, project_with_document):
    response = client.get(
        f"/projects/{project_with_document.id}/export/templates",
        params={"format": "html", "semantic_template_id": "faq"},
    )
    trigger = BeautifulSoup(response.text, "html.parser").find("select")["hx-trigger"]
    assert "docgen:document-updated from:body" in trigger
    assert "docgen:html-build from:body" not in trigger
```

- [ ] **Step 2: Run the focused tests and confirm the expected failures**

Run from `app/`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\projects\test_routes.py tests\export\test_routes.py -k "template or explicit_build or document_update_trigger" -v
```

Expected: failures because `semantic_template_id`, `hx-include`, and `manual_html_build` do not exist yet.

- [ ] **Step 3: Pass the semantic selection to the route**

In `projects/detail.html`, give `#templateSelect` the name and include it in the format request:

```html
<select
  id="templateSelect"
  name="semantic_template_id"
  data-template-source
  data-template-storage-key="docgen:template:{{ project_id }}"
>
```

```html
<select
  id="formatSelect"
  name="format"
  hx-get="/projects/{{ project_id }}/export/templates"
  hx-include="#templateSelect"
  hx-trigger="load, change"
  hx-target="#export-template-select"
  hx-swap="innerHTML"
>
```

- [ ] **Step 4: Compute and render the trigger mode**

Extend `get_export_templates` in `export/routes.py` without changing the export POST contract:

```python
def get_export_templates(
    request: Request,
    project_id: str,
    format: Annotated[OutputFormat, Query()],
    semantic_template_id: Annotated[str | None, Query()] = None,
) -> Response:
    _project_or_404(session, project_id)
    catalog = _catalog(request)
    return templates.TemplateResponse(
        request=request,
        name="export/template_options.html",
        context={
            "templates": catalog.list(format),
            "project_id": project_id,
            "manual_html_build": (
                format is OutputFormat.HTML
                and semantic_template_id == NO_TEMPLATE_ID
            ),
        },
    )
```

Keep the existing dependency arguments in the real signature and add the missing `NO_TEMPLATE_ID` import. In `template_options.html`, retain `load` and `change`, but select only one document event:

```html
hx-trigger="{% if manual_html_build | default(false) %}load, change, docgen:html-build from:body{% else %}load, change, docgen:document-ready from:body, docgen:document-updated from:body{% endif %}"
```

- [ ] **Step 5: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\projects\test_routes.py tests\export\test_routes.py -k "template or explicit_build or document_update_trigger" -v
git add app/src/docgen/templates/projects/detail.html app/src/docgen/templates/export/template_options.html app/src/docgen/export/routes.py app/tests/projects/test_routes.py app/tests/export/test_routes.py
git commit -m "feat: require explicit rebuild for no-template HTML"
```

Expected: focused tests pass; non-target trigger retains its previous events.

### Task 2: Route first and repeated Build actions without rereading the source

**Files:**
- Modify: `app/src/docgen/templates/projects/source_panel.html:100-138`
- Modify: `app/src/docgen/templates/projects/build_button.html:1-14`
- Modify: `app/src/docgen/static/js/docgen2-editor.js:79-108,468-515,715-750`
- Test: `app/tests/projects/test_routes.py`
- Test: `app/tests/test_offline_ui.py`

**Interfaces:**
- Consumes: existing `POST /projects/{project_id}/editor/import-source`, `POST /projects/{project_id}/editor/save`, `#export-form`, and `docgen:html-build` from Task 1.
- Produces: form `#editorImportForm`, button dataset `hasDocument`, and element method `editor.docgenSaveWorkspace(): Promise<{revision: number, html: string} | null>`.

- [ ] **Step 1: Write failing server-rendered form tests**

In `app/tests/projects/test_routes.py`, assert the initial-import form and document state are explicit:

```python
import_form = soup.find(id="editorImportForm")
assert import_form is not None
assert import_form["action"] == f"/projects/{project_id}/editor/import-source"
assert import_form["method"] == "post"
build = soup.find(id="buildButton")
assert build["data-has-document"] == "false"
```

In the existing-document test, add:

```python
assert soup.find(id="buildButton")["data-has-document"] == "true"
```

- [ ] **Step 2: Extend the Node harness with the three routing cases**

Update `test_template_selector_syncs_every_workspace_form_target` so `buildButton.dataset` includes `hasDocument`, then assert:

```javascript
source.value = "no-template";
format.value = "html";
buildButton.dataset.hasDocument = "false";
listeners.get("change")();
if (buildButton.formTarget !== "editorImportForm") {
  throw new Error("first no-template HTML build must import the source");
}

buildButton.dataset.hasDocument = "true";
listeners.get("change")();
if (buildButton.formTarget !== "export-form") {
  throw new Error("repeated no-template HTML build must use the editor");
}

format.value = "pdf";
listeners.get("change")();
if (buildButton.formTarget !== "conversionForm") {
  throw new Error("other no-template formats must keep direct conversion");
}
```

Add a new Node harness test named `test_repeated_no_template_html_build_saves_then_exports`. It must provide an editor with revision `1`, changed canvas HTML, a successful mocked `fetch`, and an HTMX trigger recorder. Submit `#export-form` with `submitter === buildButton` and assert this strict order:

```javascript
if (events.join(",") !== "save,document-updated,html-build") {
  throw new Error(`unexpected build sequence: ${events}`);
}
if (posted.payload.html !== '<p data-node-id="n1">Ручная правка</p>') {
  throw new Error("current editor HTML was not saved");
}
if (posted.payload.revision !== 1 || revisionInput.value !== "2") {
  throw new Error("export did not receive the saved revision");
}
if (posted.url.includes("import-source")) {
  throw new Error("repeated build reread the source");
}
```

Also mock a `409` save response and assert that `docgen:html-build` is not emitted.

- [ ] **Step 3: Run the focused tests and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\projects\test_routes.py tests\test_offline_ui.py -k "build or workspace_form_target" -v
```

Expected: failures because the import form, `data-has-document`, and save-before-export handler are absent.

- [ ] **Step 4: Add the initial-import form and state marker**

Append this form beside the existing assembly and conversion forms in `source_panel.html`:

```html
<form
  id="editorImportForm"
  action="/projects/{{ project.id }}/editor/import-source"
  method="post"
></form>
```

Add the marker to `build_button.html`:

```html
data-has-document="{{ 'true' if has_document else 'false' }}"
```

Do not add `hx-post` to `editorImportForm`; the existing 303 redirect must reload the project with the newly imported editor document.

- [ ] **Step 5: Route the Build button by template, format, and document state**

Replace the two-way assignment inside `synchronizeConversion()` with a scoped decision:

```javascript
const htmlWithoutTemplate = withoutTemplate && formatSource?.value === "html";
const hasDocument = buildButton.dataset.hasDocument === "true";
const buildForm = htmlWithoutTemplate
  ? (hasDocument ? "export-form" : "editorImportForm")
  : (withoutTemplate ? "conversionForm" : "assembleForm");
buildButton.setAttribute("form", buildForm);
```

Preserve the current formatting-template synchronization. Change disabling only enough to allow a repeated target build without a remaining source:

```javascript
const needsSource = !(htmlWithoutTemplate && hasDocument);
buildButton.disabled =
  (needsSource && !sourceAvailable) ||
  (withoutTemplate && !conversionReady);
```

- [ ] **Step 6: Make editor saving return an explicit result**

Inside `saveWorkspace()`, return the parsed successful response after updating revision and emitting `docgen:document-updated`; return `null` from the catch block:

```javascript
window.htmx?.trigger?.(
  document.body,
  "docgen:document-updated",
  {revision: result.revision},
);
markDocumentReady();
setSaveStatus("Сохранено в проекте", "saved");
return result;
```

```javascript
} catch (error) {
  const message = error instanceof Error ? error.message : "Не удалось сохранить";
  setSaveStatus(message, "error");
  return null;
}
```

Expose the function on the current editor element after it is defined:

```javascript
editor.docgenSaveWorkspace = saveWorkspace;
```

Keep the existing save-button listener unchanged so ordinary saving still uses the same validation and conflict handling.

- [ ] **Step 7: Intercept only a target Build submit and export after save**

Add one delegated submit listener near the existing document-level listeners:

```javascript
document.addEventListener("submit", async (event) => {
  const buildButton = event.submitter;
  if (event.target?.id !== "export-form" || buildButton?.id !== "buildButton") return;
  const noTemplateHtml =
    document.querySelector("[data-template-source]")?.value === "no-template" &&
    document.querySelector("#formatSelect")?.value === "html";
  if (!noTemplateHtml) return;
  event.preventDefault();
  const editor = document.querySelector("#docgen2Editor");
  const saved = await editor?.docgenSaveWorkspace?.();
  if (!saved) return;
  window.htmx?.trigger?.(
    document.body,
    "docgen:html-build",
    {revision: saved.revision},
  );
});
```

The handler must not call `/editor/import-source`; initial import remains a normal submission of `editorImportForm`.

- [ ] **Step 8: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\projects\test_routes.py tests\test_offline_ui.py -k "build or workspace_form_target" -v
git add app/src/docgen/templates/projects/source_panel.html app/src/docgen/templates/projects/build_button.html app/src/docgen/static/js/docgen2-editor.js app/tests/projects/test_routes.py app/tests/test_offline_ui.py
git commit -m "feat: build no-template HTML from editor revisions"
```

Expected: first build targets import, repeated build saves current editor content and emits export only after success, other formats retain their forms.

### Task 3: Show Open and Download for the exact no-template HTML result

**Files:**
- Modify: `app/src/docgen/export/routes.py:82-138,255-270`
- Modify: `app/src/docgen/templates/export/download_button.html:1-42`
- Test: `app/tests/export/test_routes.py`

**Interfaces:**
- Consumes: `Job.requested_document_revision`, `DocumentRepository.get_document_at_revision(project_id: str, revision: int)`, `WorkingDocument.template_id`, existing open/download endpoints.
- Produces: `_is_no_template_html_export(session: Session, job: Job) -> bool` and template context `show_html_download: bool`.

- [ ] **Step 1: Write failing result-link tests**

Keep the existing semantic-template HTML test asserting only «Открыть». Add a no-template document fixture inline and assert both actions:

```python
def test_no_template_html_status_offers_open_and_download(client, project_with_document):
    with _session(client) as session:
        repository = DocumentRepository(session)
        current = repository.get_document_with_revision(project_with_document.id)
        assert current is not None
        _, revision = current
        revision = repository.save_document(
            project_with_document.id,
            WorkingDocument(
                title="Ручная версия",
                template_id="no-template",
                nodes=[DocumentNode(id="p1", kind=NodeKind.PARAGRAPH, text="Правка")],
            ),
        )
        assert revision == 2
        session.commit()
    job = _make_export_job(
        client,
        project_with_document.id,
        state="succeeded",
        export_format=OutputFormat.HTML,
        filename="document.html",
        requested_document_revision=2,
    )
    response = client.get(f"/projects/{project_with_document.id}/exports/{job.id}/status")
    assert f"/exports/{job.id}/open" in response.text
    assert f"/exports/{job.id}/download" in response.text
    assert ">Открыть</a>" in response.text
    assert ">Скачать<" in response.text
```

Add a test where the job requests a stale/unavailable revision and assert the special second link is absent. This prevents classifying a result from the current document when the job belongs to another revision.

Extend `_make_export_job` with an explicit revision argument so both cases remain readable:

```python
def _make_export_job(
    client: TestClient,
    project_id: str,
    *,
    state: str,
    write_file: bool = True,
    filename: str = "document.docx",
    export_format: OutputFormat = OutputFormat.DOCX,
    requested_document_revision: int = 1,
) -> Job:
```

Use `requested_document_revision` in both `repository.enqueue(...)` and the
constructed `ExportResult(document_revision=...)`, preserving revision `1` as
the default for all existing tests.

- [ ] **Step 2: Run tests and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\export\test_routes.py -k "html_status or html_open_link" -v
```

Expected: no-template HTML lacks the download link.

- [ ] **Step 3: Classify the result using the requested revision**

Add this helper to `export/routes.py`:

```python
def _is_no_template_html_export(session: Session, job: Job) -> bool:
    if (
        job.export_format is not OutputFormat.HTML
        or job.requested_document_revision is None
    ):
        return False
    document = DocumentRepository(session).get_document_at_revision(
        job.project_id,
        job.requested_document_revision,
    )
    return document is not None and document.template_id == NO_TEMPLATE_ID
```

Change `_status_response` to accept `session: Session`, pass it from `start_export` and `export_status`, and add:

```python
"show_html_download": _is_no_template_html_export(session, job),
```

Do not infer this flag from `job.template_id`, because that field contains the formatting-template identifier rather than the selected semantic template.

- [ ] **Step 4: Render both links only under the server flag**

In the successful HTML branch of `download_button.html`, retain «Открыть» and append:

```html
{% if show_html_download | default(false) %}
  <a class="button primary" href="/projects/{{ job.project_id }}/exports/{{ job.id }}/download">
    <span>Скачать</span>
  </a>
{% endif %}
```

Leave non-HTML downloads and other HTML results unchanged.

- [ ] **Step 5: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\export\test_routes.py -v
git add app/src/docgen/export/routes.py app/src/docgen/templates/export/download_button.html app/tests/export/test_routes.py
git commit -m "feat: offer both actions for no-template HTML"
```

Expected: target HTML has two actions; semantic-template HTML still has only «Открыть»; non-HTML still has only «Скачать».

### Task 4: Lock the import-edit-build revision chain with an integration regression

**Files:**
- Modify: `app/tests/editor/test_routes.py:300-440`

**Interfaces:**
- Consumes: existing import, save, and export routes plus the behavior completed in Tasks 1–3.
- Produces: a regression test proving the exported job targets the edited revision and the editor document remains imported rather than reimported.

- [ ] **Step 1: Write the integration test**

Add `test_no_template_html_rebuild_targets_edited_revision_without_reimport`:

```python
def test_no_template_html_rebuild_targets_edited_revision_without_reimport(client):
    with _session(client) as session:
        project = Project(name="HTML из редактора")
        session.add(project)
        session.commit()
        project_id = project.id

    client.post(
        f"/projects/{project_id}/sources/files",
        files={"file": ("source.md", b"# Source\n\nOriginal", "text/markdown")},
        headers={"HX-Request": "true"},
    )
    imported = client.post(
        f"/projects/{project_id}/editor/import-source",
        follow_redirects=False,
    )
    assert imported.status_code == 303

    saved = client.post(
        f"/projects/{project_id}/editor/save",
        json={
            "title": "Исправленная версия",
            "html": '<h1 data-node-id="h1">Source</h1><p data-node-id="p1">Ручная правка</p>',
            "revision": 1,
        },
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] == 2

    export = client.post(
        f"/projects/{project_id}/export",
        data={"format": "html", "template_id": "docgen-light", "revision": 2},
    )
    assert export.status_code == 202

    with _session(client) as session:
        document, revision = DocumentRepository(session).get_document_with_revision(project_id)
        assert revision == 2
        assert document.origin is DocumentOrigin.IMPORTED
        assert document.source_id is not None
        assert [node.text for node in document.nodes] == ["Source", "Ручная правка"]
        job = session.scalar(
            select(Job)
            .where(Job.project_id == project_id, Job.kind == JobKind.EXPORT)
            .order_by(Job.created_at.desc())
        )
        assert job is not None
        assert job.requested_document_revision == 2
        assert job.export_format is OutputFormat.HTML
```

Use the repository return-value assertions already established in this test module; adjust only fixture/helper syntax to match its imports. Do not invoke `/editor/import-source` after the edit.

- [ ] **Step 2: Run the integration test**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\editor\test_routes.py::test_no_template_html_rebuild_targets_edited_revision_without_reimport -v
```

Expected: PASS, with the job pinned to revision 2 and the saved node text equal to the editor change.

- [ ] **Step 3: Run all scoped regression suites**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\editor\test_routes.py tests\export\test_routes.py tests\export\test_html.py tests\projects\test_routes.py tests\generation\test_routes.py tests\test_offline_ui.py -v
```

Expected: all pass. In particular, existing direct conversion tests continue to prove non-target «Без шаблона» behavior is unchanged.

- [ ] **Step 4: Run formatting and the full test suite**

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m pytest -v
```

Expected: ruff reports no errors and the full suite passes; Node-backed UI tests may only be skipped when Node.js is unavailable, as documented in `app/README.md`.

- [ ] **Step 5: Commit the integration regression**

```powershell
git add app/tests/editor/test_routes.py
git commit -m "test: cover no-template HTML editor rebuild flow"
```

Expected: the branch contains four focused implementation commits after the design and plan commits.
