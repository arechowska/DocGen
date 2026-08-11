# Web Interface Editor Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the DocGen project page as a three-column corporate workspace and replace the assembled-document preview area with the existing server-rendered editor.

**Architecture:** Keep the current FastAPI, Jinja, HTMX, and local Tailwind pipeline. Introduce reusable Jinja fragments for the workspace panels and editor surface, then make project detail, standalone editor, and successful assemble responses render the same editor fragment where appropriate.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Jinja2 templates, HTMX 2.0.8 vendored locally, Tailwind CSS 3.4.17 generated locally, pytest, BeautifulSoup, Ruff.

## Global Constraints

- Work only on branch `feature/web-interface`; do not merge or push without a separate user request.
- Do not read, print, copy, or commit `.env` values.
- Keep AI and Confluence integration behavior unchanged.
- Do not add React, Tiptap, CDN resources, external fonts, external icon libraries, inline JavaScript handlers, inline styles, or `unsafe-inline` CSP.
- Use corporate cabinet styling from `Материалы/Веб-прототипы/my.colvir.com/DESIGNE.md`: light `#f9f9fc` app background, white panels, editor soft surface near `#f3f6fa`, text near `#101828`, blue `#60bcff`, active blue near `#3196df`, borders near `#e3e8ec`, large panel radius 16 px.
- Use layout-agent structure: sources on the left, editor in the center, generation/check/chat on the right.
- Use DocGen2 only as visual reference for the editor: title/status row, toolbar, gray scrollable stage, white document sheet.
- Preserve standard HTTP fallbacks for existing forms.
- Preserve `/projects/{project_id}/document` as a standalone read-only preview route.
- Regenerate `app/src/docgen/static/css/docgen.css` with pinned `tailwindcss@3.4.17` whenever template or Tailwind source classes change, then update the SHA-256 in `app/src/docgen/static/vendor/VERSIONS.md`.

---

## File Structure

- Modify `app/src/docgen/projects/routes.py`: pass current `document` and `revision` to the project page.
- Modify `app/src/docgen/generation/routes.py`: return the editor fragment for successful HTMX assemble jobs while keeping full-page `/document` preview behavior.
- Modify `app/src/docgen/editor/routes.py`: render the new standalone editor shell and reusable editor surface.
- Modify `app/src/docgen/templates/base.html`: update global body font/background classes.
- Modify `app/src/docgen/templates/projects/detail.html`: replace the single card layout with the corporate workspace shell.
- Create `app/src/docgen/templates/projects/source_panel.html`: left workspace panel for existing source upload/list controls.
- Create `app/src/docgen/templates/projects/work_panel.html`: center workspace panel that includes the editor surface or empty state.
- Create `app/src/docgen/templates/projects/action_panel.html`: right workspace panel for generation/check controls and chat.
- Create `app/src/docgen/templates/editor/surface.html`: reusable editor area with empty and document states.
- Modify `app/src/docgen/templates/editor/document.html`: standalone route wrapper around `editor/surface.html`.
- Modify `app/src/docgen/templates/editor/node.html`: restyle node cards and retarget whole-document actions to the reusable editor container.
- Modify `app/src/docgen/templates/generation/setup.html`: restyle setup controls for the right panel while keeping form actions and targets.
- Modify `app/src/docgen/templates/generation/status.html`: restyle status cards and make polling target the right panel status area.
- Modify `app/src/docgen/templates/chat/panel.html`: make chat fit inside the right panel; keep existing route and message target.
- Modify `app/src/docgen/templates/generation/result.html`: keep it as read-only preview for `/document` and full-page artifact views.
- Modify `app/src/docgen/static/css/tailwind-source.css`: add local component classes for workspace grid, editor stage, document sheet, focus rings, and HTMX indicators.
- Modify `app/src/docgen/static/css/docgen.css`: generated output only.
- Modify `app/src/docgen/static/vendor/VERSIONS.md`: update generated CSS hash after regeneration.
- Modify `app/tests/projects/test_routes.py`: add project-page workspace and embedded editor tests.
- Modify `app/tests/editor/test_routes.py`: assert standalone editor uses the shared editor surface and retargeted controls.
- Modify `app/tests/generation/test_routes.py`: assert successful HTMX assemble swaps in the editor and full-page artifact routes keep preview behavior.
- Modify `app/tests/test_offline_ui.py`: extend CSP/offline/static assertions for the new templates and CSS.
- Modify `app/tests/test_stage3_journey.py`: keep the existing journey green and add a project-page assertion after restart if useful during execution.

---

### Task 1: Project Detail Context And Workspace Empty State

**Files:**
- Modify: `app/src/docgen/projects/routes.py`
- Modify: `app/src/docgen/templates/base.html`
- Modify: `app/src/docgen/templates/projects/detail.html`
- Create: `app/src/docgen/templates/projects/source_panel.html`
- Create: `app/src/docgen/templates/projects/work_panel.html`
- Create: `app/src/docgen/templates/projects/action_panel.html`
- Create: `app/src/docgen/templates/editor/surface.html`
- Test: `app/tests/projects/test_routes.py`

**Interfaces:**
- Consumes: `DocumentRepository(session).get_document_with_revision(project_id) -> tuple[WorkingDocument, int] | None`
- Produces template context keys: `document: WorkingDocument | None`, `revision: int | None`
- Produces DOM ids: `project-workspace`, `project-source-panel`, `project-editor-panel`, `project-action-panel`, `editor-shell`, `generation-status`, `chat-panel`

- [ ] **Step 1: Write the failing workspace empty-state test**

Append this to `app/tests/projects/test_routes.py`:

```python
from bs4 import BeautifulSoup


def test_project_detail_renders_three_area_workspace_without_document(
    client: TestClient,
) -> None:
    created = client.post("/projects", data={"name": "Интерфейс"}, follow_redirects=False)
    project_url = created.headers["location"]

    response = client.get(project_url)

    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.find(id="project-workspace") is not None
    assert soup.find(id="project-source-panel") is not None
    assert soup.find(id="project-editor-panel") is not None
    assert soup.find(id="project-action-panel") is not None
    editor_shell = soup.find(id="editor-shell")
    assert editor_shell is not None
    assert editor_shell.get("data-state") == "empty"
    assert "Соберите документ" in editor_shell.get_text(" ")
    assert soup.find(id="generation-setup") is not None
    assert soup.find(id="chat-panel") is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run from `D:\AI\Git\DocGen\app`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\projects\test_routes.py::test_project_detail_renders_three_area_workspace_without_document -v
```

Expected: FAIL because `project-workspace` and `editor-shell` empty state do not exist on the project page.

- [ ] **Step 3: Pass document context from the project detail route**

In `app/src/docgen/projects/routes.py`, replace the current `has_document` lookup inside `project_detail_response` with a stored document tuple:

```python
    documents = DocumentRepository(session)
    stored_document = documents.get_document_with_revision(project_id)
    document = stored_document[0] if stored_document is not None else None
    revision = stored_document[1] if stored_document is not None else None
```

Then set these context values:

```python
            "document": document,
            "revision": revision,
            "has_document": document is not None,
            "has_report": documents.get_report(project_id) is not None,
```

- [ ] **Step 4: Update the global shell**

In `app/src/docgen/templates/base.html`, change the body class to:

```html
  <body class="bg-[#f9f9fc] font-[Roboto,'Segoe_UI',Arial,sans-serif] text-[#101828]">
```

- [ ] **Step 5: Create the reusable editor surface with empty state**

Create `app/src/docgen/templates/editor/surface.html`:

```html
<section
  class="docgen-editor-shell"
  id="editor-shell"
  data-project-id="{{ project_id }}"
  data-state="{% if document %}ready{% else %}empty{% endif %}"
  {% if revision is not none %}data-revision="{{ revision }}"{% endif %}
  {% if document %}
    hx-get="/projects/{{ project_id }}/editor"
    hx-trigger="docgen:document-updated from:body"
    hx-target="#editor-shell"
    hx-swap="outerHTML"
  {% endif %}
>
  {% if document %}
    <div class="docgen-editor-header">
      <div class="min-w-0">
        <p class="docgen-eyebrow">Рабочий документ</p>
        <h2 class="truncate text-xl font-semibold text-[#101828]">{{ document.title }}</h2>
      </div>
      <span class="docgen-status-pill">Ревизия {{ revision }}</span>
    </div>
    <div class="docgen-editor-toolbar" aria-label="Инструменты редактора">
      {% for kind, label in [("heading", "Раздел"), ("paragraph", "Абзац"), ("list", "Список"), ("table", "Таблица")] %}
        <button
          class="docgen-secondary-button"
          type="button"
          hx-post="/projects/{{ project_id }}/editor/nodes"
          hx-vals='{"kind": "{{ kind }}", "revision": "{{ revision }}"}'
          hx-target="#editor-shell"
          hx-swap="outerHTML"
        >{{ label }}</button>
      {% endfor %}
    </div>
    <div class="docgen-editor-stage">
      <div class="docgen-document-sheet">
        <div class="space-y-3">
          {% for node in document.nodes %}
            {% include "editor/node.html" %}
          {% endfor %}
        </div>
      </div>
    </div>
  {% else %}
    <div class="docgen-editor-empty">
      <div class="docgen-empty-sheet" aria-hidden="true">
        <span></span>
        <span></span>
        <span></span>
      </div>
      <div>
        <p class="docgen-eyebrow">Рабочий документ</p>
        <h2 class="mt-2 text-xl font-semibold text-[#101828]">Соберите документ</h2>
        <p class="mt-3 text-sm leading-6 text-[#667085]">
          Загрузите источники слева и запустите сборку в правой панели. После успешной сборки здесь откроется редактор.
        </p>
        <a class="docgen-primary-button mt-5 inline-flex" href="#generation-setup">К сборке</a>
      </div>
    </div>
  {% endif %}
</section>
```

- [ ] **Step 6: Split project detail into three panels**

Replace the content block in `app/src/docgen/templates/projects/detail.html` with:

```html
{% block content %}
  <main class="min-h-screen">
    <section class="docgen-topbar">
      <div class="mx-auto flex w-full max-w-[1600px] flex-wrap items-center justify-between gap-4 px-6 py-6">
        <div class="min-w-0">
          <a href="/projects" class="text-sm font-medium text-white/90">Все проекты</a>
          <div class="mt-3">{% include "projects/name_form.html" %}</div>
        </div>
        <form action="/projects/{{ project.id }}/delete" method="post" hx-delete="/projects/{{ project.id }}" hx-confirm="Удалить проект?">
          <button class="docgen-topbar-button" type="submit">Удалить проект</button>
        </form>
      </div>
    </section>

    <section id="project-workspace" class="docgen-workspace mx-auto w-full max-w-[1600px] px-6 py-6">
      {% include "projects/source_panel.html" %}
      {% include "projects/work_panel.html" %}
      {% include "projects/action_panel.html" %}
    </section>
  </main>
{% endblock %}
```

Create `app/src/docgen/templates/projects/source_panel.html`:

```html
<aside id="project-source-panel" class="docgen-panel">
  <div class="mb-5">
    <p class="docgen-eyebrow">Материалы</p>
    <h2 class="mt-1 text-lg font-semibold text-[#101828]">Источники</h2>
  </div>
  {% include "sources/list.html" %}
</aside>
```

Create `app/src/docgen/templates/projects/work_panel.html`:

```html
<section id="project-editor-panel" class="min-w-0">
  {% include "editor/surface.html" %}
</section>
```

Create `app/src/docgen/templates/projects/action_panel.html`:

```html
<aside id="project-action-panel" class="docgen-panel space-y-6">
  {% include "generation/setup.html" %}
  {% if document %}
    {% include "chat/panel.html" %}
  {% endif %}
</aside>
```

- [ ] **Step 7: Run the empty-state test to verify it passes**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\projects\test_routes.py::test_project_detail_renders_three_area_workspace_without_document -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add app/src/docgen/projects/routes.py app/src/docgen/templates/base.html app/src/docgen/templates/projects/detail.html app/src/docgen/templates/projects/source_panel.html app/src/docgen/templates/projects/work_panel.html app/src/docgen/templates/projects/action_panel.html app/src/docgen/templates/editor/surface.html app/tests/projects/test_routes.py
git commit -m "feat: add project workspace empty state"
```

### Task 2: Embedded Editor Reuse And Retargeted Controls

**Files:**
- Modify: `app/src/docgen/templates/editor/document.html`
- Modify: `app/src/docgen/templates/editor/node.html`
- Modify: `app/src/docgen/editor/routes.py`
- Test: `app/tests/projects/test_routes.py`
- Test: `app/tests/editor/test_routes.py`

**Interfaces:**
- Consumes from Task 1: `editor/surface.html` accepts `project_id`, `document`, and `revision`.
- Produces: standalone editor page contains `editor-page` wrapper and the same `editor-shell` surface; all add/delete/move document-level actions target `#editor-shell`.

- [ ] **Step 1: Write the failing embedded editor test**

Append this to `app/tests/projects/test_routes.py`:

```python
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument


def test_project_detail_embeds_editor_when_document_exists(client: TestClient) -> None:
    created = client.post("/projects", data={"name": "Редактор"}, follow_redirects=False)
    project_url = created.headers["location"]
    project_id = project_url.rsplit("/", 1)[-1]
    with client.app.state.session_factory() as session:
      DocumentRepository(session).save_document(
          project_id,
          WorkingDocument(
              title="Техническое задание",
              template_id="use-case",
              nodes=[DocumentNode(id="intro", kind=NodeKind.PARAGRAPH, text="Введение")],
          ),
      )
      session.commit()

    response = client.get(project_url)

    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    editor_shell = soup.find(id="editor-shell")
    assert editor_shell is not None
    assert editor_shell.get("data-state") == "ready"
    assert editor_shell.get("data-revision") == "1"
    assert "Техническое задание" in editor_shell.get_text(" ")
    assert soup.find(id="node-intro") is not None
    assert soup.find(id="chat-panel") is not None
```

- [ ] **Step 2: Write the failing standalone shared-surface test**

Append this to `app/tests/editor/test_routes.py`:

```python
def test_standalone_editor_uses_shared_surface(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.get(f"/projects/{project_with_document.id}/editor")

    assert response.status_code == 200
    assert 'id="editor-page"' in response.text
    assert 'id="editor-shell"' in response.text
    assert 'data-state="ready"' in response.text
    assert 'hx-target="#editor-shell"' in response.text
    assert 'id="chat-panel"' in response.text
```

- [ ] **Step 3: Run both tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\projects\test_routes.py::test_project_detail_embeds_editor_when_document_exists tests\editor\test_routes.py::test_standalone_editor_uses_shared_surface -v
```

Expected: FAIL because the standalone editor still owns the old layout and project detail does not render document context yet if Task 1 was not completed.

- [ ] **Step 4: Refactor the standalone editor route wrapper**

Replace `app/src/docgen/templates/editor/document.html` with:

```html
{% extends "base.html" %}

{% block title %}Редактор DocGen{% endblock %}

{% block content %}
<main id="editor-page" class="mx-auto flex w-full max-w-[1400px] gap-6 px-6 py-8">
  <section class="min-w-0 flex-1">
    <div class="mb-5 flex flex-wrap items-center justify-between gap-3">
      <a class="text-sm font-medium text-[#3196df]" href="/projects/{{ project_id }}">К проекту</a>
    </div>
    {% include "editor/surface.html" %}
  </section>
  {% include "chat/panel.html" %}
</main>
{% endblock %}
```

- [ ] **Step 5: Retarget document-level node controls to the shared surface**

In `app/src/docgen/templates/editor/node.html`, keep `hx-target="#node-{{ node.id }}"` for autosave operations that return a single node, and ensure these whole-document operations use `hx-target="#editor-shell"` and `hx-swap="outerHTML"`:

```html
hx-post="/projects/{{ project_id }}/editor/nodes"
hx-target="#editor-shell"
hx-swap="outerHTML"
```

```html
hx-post="/projects/{{ project_id }}/editor/nodes/{{ node.id }}/move"
hx-target="#editor-shell"
hx-swap="outerHTML"
```

```html
hx-delete="/projects/{{ project_id }}/editor/nodes/{{ node.id }}"
hx-target="#editor-shell"
hx-swap="outerHTML"
```

- [ ] **Step 6: Run embedded editor tests to verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\projects\test_routes.py::test_project_detail_embeds_editor_when_document_exists tests\editor\test_routes.py::test_standalone_editor_uses_shared_surface tests\editor\test_routes.py::test_insert_paragraph_after_selected_node tests\editor\test_routes.py::test_move_node_down_persists_order tests\editor\test_routes.py::test_delete_node_removes_it_from_document -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/src/docgen/templates/editor/document.html app/src/docgen/templates/editor/node.html app/tests/projects/test_routes.py app/tests/editor/test_routes.py
git commit -m "feat: reuse editor surface across project and editor pages"
```

### Task 3: Assemble Success Swaps The Center Editor

**Files:**
- Modify: `app/src/docgen/generation/routes.py`
- Modify: `app/src/docgen/templates/generation/result.html`
- Test: `app/tests/generation/test_routes.py`

**Interfaces:**
- Consumes from Task 1: `editor/surface.html` can render a ready document when given `document` and `revision`.
- Produces: `_editor_response(request, project_id, document, revision, warnings=()) -> Response` for successful HTMX assemble responses.
- Preserves: `_document_response(..., standalone=True)` for `/projects/{project_id}/document` and non-HTMX job pages.

- [ ] **Step 1: Write the failing HTMX assemble swap test**

Modify `test_succeeded_job_swaps_to_saved_document` in `app/tests/generation/test_routes.py` so the request simulates HTMX and expects the editor surface:

```python
def test_succeeded_assemble_job_swaps_to_editor_surface(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())
    with _session(client) as session:
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(project_with_source.id, JobKind.ASSEMBLE, "use-case")
        assert repository.claim_next() is not None
        succeeded_job = repository.mark_succeeded(job.id)

    response = client.get(
        f"/projects/{project_with_source.id}/jobs/{succeeded_job.id}",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'id="editor-shell"' in response.text
    assert 'data-state="ready"' in response.text
    assert "Оплата заказа" in response.text
    assert 'id="generation-status"' not in response.text
    assert 'hx-trigger="every 2s"' not in response.text
```

- [ ] **Step 2: Add the full-page preview preservation test**

Append this to `app/tests/generation/test_routes.py`:

```python
def test_document_view_keeps_read_only_preview(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())

    response = client.get(
        f"/projects/{project_with_source.id}/document",
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 200
    assert "Собранный документ" in response.text
    assert 'id="document-start"' in response.text
    assert 'id="editor-shell"' not in response.text
```

- [ ] **Step 3: Run the tests to verify the assemble test fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\generation\test_routes.py::test_succeeded_assemble_job_swaps_to_editor_surface tests\generation\test_routes.py::test_document_view_keeps_read_only_preview -v
```

Expected: first test FAILS because success currently returns `generation/result.html`; second test PASSES.

- [ ] **Step 4: Add an editor response helper**

In `app/src/docgen/generation/routes.py`, add this helper below `_document_response`:

```python
def _editor_response(
    request: Request,
    project_id: str,
    document: WorkingDocument,
    revision: int,
    *,
    warnings: tuple[str, ...] = (),
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="editor/surface.html",
        context={
            "project_id": project_id,
            "document": document,
            "revision": revision,
            "warnings": warnings,
        },
    )
```

- [ ] **Step 5: Use the editor response for HTMX assemble success**

In `_job_response`, inside the `if job.kind is JobKind.ASSEMBLE` block, change the success branch to:

```python
            if document is not None:
                if _wants_full_page(request):
                    return _document_response(
                        request,
                        job.project_id,
                        document,
                        standalone=True,
                        warnings=job.warning_messages,
                    )
                return _editor_response(
                    request,
                    job.project_id,
                    document,
                    job.result_document_revision or 1,
                    warnings=job.warning_messages,
                )
```

- [ ] **Step 6: Run generation route tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\generation\test_routes.py::test_succeeded_assemble_job_swaps_to_editor_surface tests\generation\test_routes.py::test_document_view_keeps_read_only_preview tests\generation\test_routes.py::test_cancel_race_renders_completed_result_instead_of_cancellation_notice tests\generation\test_routes.py::test_check_route_job_runs_once_and_swaps_to_saved_report -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/src/docgen/generation/routes.py app/tests/generation/test_routes.py
git commit -m "feat: swap assemble success into editor"
```

### Task 4: Corporate Workspace Styling And Responsive Behavior

**Files:**
- Modify: `app/src/docgen/static/css/tailwind-source.css`
- Modify: `app/src/docgen/templates/generation/setup.html`
- Modify: `app/src/docgen/templates/generation/status.html`
- Modify: `app/src/docgen/templates/chat/panel.html`
- Modify: `app/src/docgen/templates/editor/node.html`
- Test: `app/tests/test_offline_ui.py`

**Interfaces:**
- Consumes DOM ids from Tasks 1-3.
- Produces local CSS classes: `docgen-topbar`, `docgen-topbar-button`, `docgen-workspace`, `docgen-panel`, `docgen-eyebrow`, `docgen-primary-button`, `docgen-secondary-button`, `docgen-status-pill`, `docgen-editor-shell`, `docgen-editor-header`, `docgen-editor-toolbar`, `docgen-editor-stage`, `docgen-document-sheet`, `docgen-editor-empty`, `docgen-empty-sheet`.

- [ ] **Step 1: Write the failing CSS contract test**

Append this to `app/tests/test_offline_ui.py`:

```python
def test_workspace_css_contains_corporate_layout_tokens(client: TestClient) -> None:
    stylesheet = client.get("/static/css/docgen.css")

    assert stylesheet.status_code == 200
    css = stylesheet.text.lower()
    for token in (
        ".docgen-workspace",
        "grid-template-columns:320px minmax(0,1fr) 320px",
        "#60bcff",
        "#3196df",
        "#f9f9fc",
        "#f3f6fa",
        ".docgen-document-sheet",
        "@media (max-width:1023px)",
    ):
        assert token in css
```

- [ ] **Step 2: Write the no-inline/no-CDN expanded template test**

Extend `test_templates_have_no_inline_handlers_styles_or_public_executable_assets` with these assertions inside the loop:

```python
        assert "onclick=" not in markup, path
        assert "onchange=" not in markup, path
        assert "fonts.googleapis.com" not in markup, path
        assert "lucide" not in markup.lower(), path
```

- [ ] **Step 3: Run offline UI tests to verify the CSS contract fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_offline_ui.py::test_workspace_css_contains_corporate_layout_tokens tests\test_offline_ui.py::test_templates_have_no_inline_handlers_styles_or_public_executable_assets -v
```

Expected: first test FAILS because classes are not generated yet; second test may pass before restyling.

- [ ] **Step 4: Add component CSS to Tailwind source**

Append this to `app/src/docgen/static/css/tailwind-source.css`:

```css
@layer components {
  .docgen-topbar {
    background: linear-gradient(135deg, #3196df 0%, #60bcff 100%);
  }

  .docgen-topbar-button {
    @apply rounded-2xl border border-white/40 bg-white/15 px-4 py-2 text-sm font-medium text-white transition hover:bg-white/25 focus:outline-none focus:ring-2 focus:ring-white/80;
  }

  .docgen-workspace {
    display: grid;
    grid-template-columns: 320px minmax(0, 1fr) 320px;
    gap: 24px;
    align-items: start;
  }

  .docgen-panel {
    @apply rounded-2xl border border-[#e3e8ec] bg-white p-5;
  }

  .docgen-eyebrow {
    @apply text-xs font-semibold uppercase tracking-normal text-[#667085];
  }

  .docgen-primary-button {
    @apply rounded-2xl bg-[#3196df] px-4 py-2 text-sm font-semibold text-white transition hover:bg-[#247fbe] focus:outline-none focus:ring-2 focus:ring-[#60bcff];
  }

  .docgen-secondary-button {
    @apply rounded-2xl border border-[#d8e1e8] bg-white px-3 py-2 text-sm font-medium text-[#344054] transition hover:bg-[#f3f6fa] focus:outline-none focus:ring-2 focus:ring-[#60bcff];
  }

  .docgen-status-pill {
    @apply shrink-0 rounded-full border border-[#d8e1e8] bg-[#f3f6fa] px-3 py-1 text-xs font-medium text-[#475467];
  }

  .docgen-editor-shell {
    @apply min-w-0 overflow-hidden rounded-2xl border border-[#e3e8ec] bg-white;
  }

  .docgen-editor-header {
    @apply flex flex-wrap items-center justify-between gap-3 border-b border-[#e3e8ec] px-5 py-4;
  }

  .docgen-editor-toolbar {
    @apply flex flex-wrap gap-2 border-b border-[#e3e8ec] bg-[#f9f9fc] px-5 py-3;
  }

  .docgen-editor-stage {
    @apply max-h-[72vh] overflow-auto bg-[#f3f6fa] px-4 py-6;
  }

  .docgen-document-sheet {
    @apply mx-auto min-h-[640px] w-full max-w-[860px] rounded-2xl border border-[#e3e8ec] bg-white p-6;
  }

  .docgen-editor-empty {
    @apply grid min-h-[540px] gap-6 bg-[#f3f6fa] p-6 md:grid-cols-[minmax(0,1fr)_minmax(240px,360px)] md:items-center;
  }

  .docgen-empty-sheet {
    @apply mx-auto flex min-h-[420px] w-full max-w-[520px] flex-col gap-4 rounded-2xl border border-[#e3e8ec] bg-white p-8;
  }

  .docgen-empty-sheet span {
    @apply block h-3 rounded-full bg-[#e3e8ec];
  }

  @media (max-width: 1023px) {
    .docgen-workspace {
      grid-template-columns: minmax(0, 1fr);
    }
  }
}
```

- [ ] **Step 5: Restyle generation setup for the right panel**

In `app/src/docgen/templates/generation/setup.html`, keep ids, form actions, `hx-post`, `hx-target="#generation-status"`, and `hx-swap="outerHTML"`. Replace large spacing and two-column cards with compact panel markup:

```html
{% if setup_fragment %}
<section id="generation-status" class="rounded-2xl bg-[#fff2f2] px-4 py-3" role="alert" aria-live="polite">
  <p class="text-sm text-[#b42318]">{{ generation_error }}</p>
</section>
{% else %}
<section id="generation-setup">
  <div class="flex flex-wrap items-center justify-between gap-3">
    <div>
      <p class="docgen-eyebrow">AI-конвейер</p>
      <h2 class="mt-1 text-lg font-semibold text-[#101828]">Сборка и проверка</h2>
    </div>
    <div class="flex gap-3 text-sm">
      {% if has_document %}<a class="font-medium text-[#3196df]" href="/projects/{{ project.id }}/document">Preview</a>{% endif %}
      {% if has_report %}<a class="font-medium text-[#3196df]" href="/projects/{{ project.id }}/report">Отчёт</a>{% endif %}
    </div>
  </div>
  ...
</section>
{% endif %}
```

Use `docgen-secondary-button` for secondary actions and `docgen-primary-button` for primary submit buttons. Preserve every `name`, `id`, `required`, hidden field, and route value from the existing template.

- [ ] **Step 6: Restyle status, chat, and node templates**

In `generation/status.html`, keep `id="generation-status"` and active polling attributes. Use `docgen-panel`-compatible compact classes and preserve cancel/retry form actions.

In `chat/panel.html`, remove the fixed `max-w-sm border-l pl-6` layout and use:

```html
<section class="space-y-3" id="chat-panel">
```

Keep `hx-post="/projects/{{ project_id }}/chat"`, `hx-target="#chat-messages"`, `name="message"`, and hidden `revision`.

In `editor/node.html`, restyle article and controls using `docgen-secondary-button` and visible focus classes. Preserve all `id`, `data-node-id`, `data-kind`, `data-revision`, `name`, and HTMX route attributes.

- [ ] **Step 7: Regenerate Tailwind CSS and update the recorded hash**

Run from `D:\AI\Git\DocGen\app`:

```powershell
npx --yes tailwindcss@3.4.17 -i src/docgen/static/css/tailwind-source.css -o src/docgen/static/css/docgen.css --minify --content "src/docgen/templates/**/*.html"
```

Then compute the hash:

```powershell
(Get-FileHash -Algorithm SHA256 -LiteralPath .\src\docgen\static\css\docgen.css).Hash.ToLower()
```

Update the SHA-256 value for `../css/docgen.css` in `app/src/docgen/static/vendor/VERSIONS.md` to the exact command output.

- [ ] **Step 8: Run offline UI tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_offline_ui.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```powershell
git add app/src/docgen/static/css/tailwind-source.css app/src/docgen/static/css/docgen.css app/src/docgen/static/vendor/VERSIONS.md app/src/docgen/templates/generation/setup.html app/src/docgen/templates/generation/status.html app/src/docgen/templates/chat/panel.html app/src/docgen/templates/editor/node.html app/tests/test_offline_ui.py
git commit -m "style: apply corporate workspace design"
```

### Task 5: Error Placement, Existing Journeys, And Package Assets

**Files:**
- Modify: `app/src/docgen/generation/routes.py`
- Modify: `app/src/docgen/templates/projects/action_panel.html`
- Modify: `app/tests/generation/test_routes.py`
- Modify: `app/tests/test_stage3_journey.py`
- Modify: `app/tests/test_package_install.py`

**Interfaces:**
- Consumes: project detail context keys from Task 1 and editor response behavior from Task 3.
- Produces: full-page setup errors still render project detail with `document`, `revision`, `has_document`, and `has_report`; right-panel errors target `generation-status`.

- [ ] **Step 1: Write the failing full-page setup error preservation test**

Append this to `app/tests/generation/test_routes.py`:

```python
def test_full_page_generation_error_keeps_workspace_context(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())

    response = client.post(
        f"/projects/{project_with_source.id}/jobs/assemble",
        data={"template_id": "missing"},
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 422
    assert 'id="project-workspace"' in response.text
    assert 'id="editor-shell"' in response.text
    assert 'data-state="ready"' in response.text
    assert "Шаблон не найден" in response.text
```

- [ ] **Step 2: Run the test to verify it fails if `_setup_error` lacks document context**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\generation\test_routes.py::test_full_page_generation_error_keeps_workspace_context -v
```

Expected: FAIL if the full-page error context does not include `document` and `revision`; PASS if Task 1 logic was already copied into `_setup_error`.

- [ ] **Step 3: Add document context to full-page setup errors**

In `_setup_error` in `app/src/docgen/generation/routes.py`, inside the `_wants_full_page(request)` branch, compute:

```python
        stored_document = documents.get_document_with_revision(project.id)
        document = stored_document[0] if stored_document is not None else None
        revision = stored_document[1] if stored_document is not None else None
```

Then add these context values:

```python
                "document": document,
                "revision": revision,
                "has_document": document is not None,
                "has_report": documents.get_report(project.id) is not None,
```

- [ ] **Step 4: Add a Stage 3 project-page regression assertion**

In `app/tests/test_stage3_journey.py`, after the restarted editor assertions, add:

```python
        project_page = restarted.get(f"/projects/{project_id}")
        assert project_page.status_code == 200
        assert 'id="project-workspace"' in project_page.text
        assert 'id="editor-shell"' in project_page.text
        assert "Ручная правка" in project_page.text
        assert "Уточнённый результат" in project_page.text
```

- [ ] **Step 5: Keep wheel asset assertions current**

In `app/tests/test_package_install.py`, ensure the package test still asserts:

```python
    assert "docgen/templates/editor/surface.html" in members
    assert "docgen/templates/projects/source_panel.html" in members
    assert "docgen/templates/projects/work_panel.html" in members
    assert "docgen/templates/projects/action_panel.html" in members
    assert "docgen/static/css/docgen.css" in members
```

- [ ] **Step 6: Run journey and package tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\generation\test_routes.py::test_full_page_generation_error_keeps_workspace_context tests\test_stage3_journey.py tests\test_package_install.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/src/docgen/generation/routes.py app/tests/generation/test_routes.py app/tests/test_stage3_journey.py app/tests/test_package_install.py
git commit -m "test: preserve workspace flows and packaged templates"
```

### Task 6: Final Verification And Browser Smoke

**Files:**
- Modify only if verification exposes a defect in files touched by Tasks 1-5.

**Interfaces:**
- Consumes all prior task outputs.
- Produces final evidence: focused tests, full tests, Ruff, diff check, CSS hash, browser smoke notes.

- [ ] **Step 1: Run focused UI and route tests**

Run from `D:\AI\Git\DocGen\app`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\projects\test_routes.py tests\editor\test_routes.py tests\generation\test_routes.py tests\test_offline_ui.py tests\test_stage3_journey.py tests\test_package_install.py -v
```

Expected: PASS.

- [ ] **Step 2: Run the full test suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

Expected: PASS with no failures. If Windows-specific tests are already documented as deselected in this repo, use the same exact deselect list used by the existing team practice and record it in the final evidence.

- [ ] **Step 3: Run Ruff**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
```

Expected: PASS.

- [ ] **Step 4: Check whitespace and generated CSS hash**

Run from repo root:

```powershell
git diff --check
```

Expected: no output and exit code 0.

Run from `D:\AI\Git\DocGen\app`:

```powershell
(Get-FileHash -Algorithm SHA256 -LiteralPath .\src\docgen\static\css\docgen.css).Hash.ToLower()
```

Expected: exactly matches the `../css/docgen.css` SHA-256 recorded in `src/docgen/static/vendor/VERSIONS.md`.

- [ ] **Step 5: Run browser smoke on wide and narrow viewports**

Start the app from `D:\AI\Git\DocGen\app` on an unused port:

```powershell
.\.venv\Scripts\python.exe -m uvicorn docgen.main:app --host 127.0.0.1 --port 8000
```

In a browser automation tool, check:

```text
Viewport 1440x900:
- GET /projects
- create a project
- upload a small Markdown source
- confirm project page has no horizontal scroll
- confirm left, center, right panels are visible
- start assemble with test model settings or a test fixture route available in the local app
- after success, confirm #editor-shell has data-state="ready"
- edit a paragraph and confirm the changed text remains visible

Viewport 390x844:
- open the same project page
- confirm no horizontal scroll
- confirm source panel appears before editor panel
- confirm editor panel appears before action panel
```

Expected: no page errors, no console errors, no external resource requests, no horizontal scroll.

- [ ] **Step 6: Inspect final git diff**

Run:

```powershell
git status --short
git diff --stat
git diff -- app/src/docgen/projects/routes.py app/src/docgen/generation/routes.py app/src/docgen/editor/routes.py
```

Expected: changes are limited to UI templates, route context for existing documents, tests, generated CSS, and asset metadata. No `.env` changes are staged or printed.

- [ ] **Step 7: Commit final fixes if any were needed**

If Step 6 revealed fixes after the previous task commits, commit only those fixes:

```powershell
git add <exact fixed files>
git commit -m "fix: polish workspace editor integration"
```

If no fixes were needed, do not create an empty commit.

## Self-Review

- Spec coverage: Tasks 1-2 cover three workspace areas, reusable embedded editor, empty and ready states. Task 3 covers successful assemble replacement and preview compatibility. Task 4 covers corporate styling, responsive layout, offline CSS, CSP-friendly markup, and accessible controls. Task 5 covers error placement, existing Stage 3 journey, chat, and packaging. Task 6 covers final full verification and browser smoke.
- Placeholder scan: no deferred markers remain; every task has concrete files, commands, assertions, and expected outcomes.
- Type consistency: `document` and `revision` are introduced once in route context and consumed consistently by `editor/surface.html`, `editor/document.html`, `projects/work_panel.html`, `_editor_response`, and chat/editor controls.
