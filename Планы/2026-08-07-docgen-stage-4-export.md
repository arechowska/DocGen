# DocGen Stage 4 Export and Formatting Templates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить выбор выходного формата и одного из доступных для него шаблонов оформления, затем формировать DOCX, PDF, HTML и Markdown без потери проекта.

**Architecture:** Каталог оформления загружает отдельные YAML-манифесты и проверяет совместимость шаблона с форматом. Каждый exporter реализует единый протокол и преобразует сохранённый `WorkingDocument` напрямую, без повторной AI-генерации. Экспорт выполняется существующим фоновым worker, сохраняет последний файл для комбинации формат–шаблон–ревизия и предоставляет скачивание только после успешной атомарной записи.

**Tech Stack:** стек этапов 1–3 плюс python-docx, Jinja2, WeasyPrint, Markdown-it-py, PyYAML, pytest и XML/ZIP-проверки стандартной библиотеки.

## Global Constraints

- Формат и шаблон оформления выбираются раздельно; один формат может иметь несколько шаблонов.
- Шаблон оформления не меняет смысл, порядок или факты рабочего документа.
- Поддерживаемые форматы: DOCX, PDF, HTML и Markdown.
- DOCX, PDF и HTML поддерживают стили, колонтитулы, нумерацию, таблицы и брендирование в пределах возможностей формата.
- Markdown сохраняет структуру и разметку без неподдерживаемого визуального оформления.
- Экспорт использует конкретную сохранённую ревизию документа и не запускает AI-модель.
- Ошибка экспорта не изменяет и не удаляет проект или рабочий документ.
- Все временные файлы пишутся внутри каталога проекта и атомарно заменяются только после полной проверки.
- Первый встроенный стиль `docgen-light` следует `DESIGN.md`: белые и `#f5f5f7` поверхности, основной текст `#1d1d1f`, вторичный `#707070`, акцент `#0071e3`, системный шрифт с fallback Inter.

## File Structure

```text
Проекты/DocGen/app/src/docgen/
├── formatting/
│   ├── schemas.py                 # формат и манифест оформления
│   ├── catalog.py                 # загрузка/валидация каталога
│   └── templates/
│       ├── docgen-light-docx.yaml
│       ├── docgen-light.docx
│       ├── docgen-light-html.yaml
│       ├── docgen-light.html.j2
│       ├── docgen-light.css
│       ├── docgen-light-pdf.yaml
│       ├── docgen-light-pdf.html.j2
│       ├── docgen-light-pdf.css
│       └── docgen-light-markdown.yaml
├── export/
│   ├── protocol.py
│   ├── storage.py
│   ├── markdown.py
│   ├── html.py
│   ├── docx.py
│   ├── pdf.py
│   ├── service.py
│   └── routes.py
└── templates/export/{select,status,error}.html

Проекты/DocGen/app/tests/
├── formatting/test_catalog.py
├── export/test_markdown.py
├── export/test_html.py
├── export/test_docx.py
├── export/test_pdf.py
├── export/test_service.py
├── export/test_routes.py
└── test_stage4_journey.py
```

---

### Task 1: Define and validate formatting templates

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/formatting/schemas.py`
- Create: `Проекты/DocGen/app/src/docgen/formatting/catalog.py`
- Create: `Проекты/DocGen/app/tests/formatting/test_catalog.py`

**Interfaces:**
- Produces: `OutputFormat.DOCX/PDF/HTML/MARKDOWN`
- Produces: `FormattingTemplate(id, name, format, renderer, assets, options)`
- Produces: `FormattingCatalog.list(format: OutputFormat | None = None) -> list[FormattingTemplate]`
- Produces: `FormattingCatalog.get(format: OutputFormat, template_id: str) -> FormattingTemplate`

- [ ] **Step 1: Write failing catalog tests**

```python
def test_catalog_filters_templates_by_format(catalog):
    assert [t.id for t in catalog.list(OutputFormat.DOCX)] == ["docgen-light"]
    assert catalog.get(OutputFormat.HTML, "docgen-light").renderer == "html"


def test_template_cannot_claim_another_format(tmp_path):
    write_yaml(tmp_path / "bad.yaml", {"id": "bad", "format": "pdf", "renderer": "docx", "assets": []})
    with pytest.raises(FormattingTemplateError, match="Renderer docx несовместим с форматом pdf"):
        FormattingCatalog(tmp_path).list()
```

- [ ] **Step 2: Run tests and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/formatting -v`

Expected: FAIL because formatting catalog does not exist.

- [ ] **Step 3: Implement strict manifest schemas**

Forbid unknown keys. Require unique IDs within each format, Russian display names, renderer equal to format, and existence of every relative asset under the catalog directory. Reject absolute paths and `..` path segments. Sort by `name`, then `id`.

```python
class FormattingTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    format: OutputFormat
    renderer: OutputFormat
    assets: list[str]
    options: dict[str, str | int | float | bool] = Field(default_factory=dict)
```

- [ ] **Step 4: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/formatting -v`

Expected: PASS using complete test-local manifests and assets created by the test fixtures.

```bash
git add Проекты/DocGen/app/src/docgen/formatting Проекты/DocGen/app/tests/formatting
git commit -m "feat: define DocGen formatting catalog"
```

### Task 2: Implement Markdown export

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/export/protocol.py`
- Create: `Проекты/DocGen/app/src/docgen/export/markdown.py`
- Create: `Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light-markdown.yaml`
- Create: `Проекты/DocGen/app/tests/export/test_markdown.py`

**Interfaces:**
- Produces: protocol `Exporter.render(document: WorkingDocument, template: FormattingTemplate) -> RenderedFile`
- Produces: `RenderedFile(filename: str, media_type: str, content: bytes)`
- Produces: `MarkdownExporter.render(document: WorkingDocument, template: FormattingTemplate) -> RenderedFile`

- [ ] **Step 1: Write exact-output tests**

```python
def test_markdown_renders_supported_nodes(document_all_kinds, markdown_template):
    rendered = MarkdownExporter().render(document_all_kinds, markdown_template)
    text = rendered.content.decode("utf-8")
    assert "# Заголовок" in text
    assert "- Пункт" in text
    assert "| Колонка 1 | Колонка 2 |" in text
    assert "> **Нет данных в источниках:**" in text
    assert rendered.filename.endswith(".md")
```

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/export/test_markdown.py -v`

Expected: FAIL because exporter protocol does not exist.

- [ ] **Step 3: Implement deterministic Markdown rendering**

Escape table pipes and newlines; render headings at levels 1–6, unordered/ordered lists, GFM tables, images with relative export asset names, and gaps as blockquotes. Separate top-level nodes with one blank line, end with one newline, encode UTF-8, and derive a filesystem-safe filename from the document title.

- [ ] **Step 4: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/export/test_markdown.py -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/src/docgen/export Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light-markdown.yaml Проекты/DocGen/app/tests/export/test_markdown.py
git commit -m "feat: export DocGen Markdown"
```

### Task 3: Implement standalone HTML export

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/export/html.py`
- Create: `Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light.html.j2`
- Create: `Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light.css`
- Create: `Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light-html.yaml`
- Create: `Проекты/DocGen/app/tests/export/test_html.py`

**Interfaces:**
- Produces: `HtmlExporter.render(document: WorkingDocument, template: FormattingTemplate) -> RenderedFile`

- [ ] **Step 1: Write safety and completeness tests**

```python
def test_html_is_standalone_and_escaped(document_with_image, html_template):
    rendered = HtmlExporter(image_loader=fake_image_loader).render(document_with_image, html_template)
    html = rendered.content.decode("utf-8")
    assert "<style>" in html
    assert "data:image/png;base64," in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
```

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/export/test_html.py -v`

Expected: FAIL because `HtmlExporter` does not exist.

- [ ] **Step 3: Implement Jinja rendering with embedded assets**

Use a dedicated Jinja `Environment(autoescape=True)` and load only catalog-validated template/CSS paths. Convert stored images to `data:` URLs after verifying MIME type and storage containment. Render semantic `<h1>`–`<h6>`, `<p>`, `<ul>/<ol>`, `<table>`, `<figure>` and `<aside class="gap">`; include `<meta charset="utf-8">` and no external resources.

- [ ] **Step 4: Add DocGen Light HTML assets**

Use the exact colors in Global Constraints, `font-family: Inter, system-ui, sans-serif`, content width `960px`, 28px card radius, no shadows, responsive tables and print-safe margins. Include document title in `<title>`.

- [ ] **Step 5: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/export/test_html.py -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/src/docgen/export/html.py Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light*html* Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light.css Проекты/DocGen/app/tests/export/test_html.py
git commit -m "feat: export standalone DocGen HTML"
```

### Task 4: Implement DOCX export with Word styles

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/export/docx.py`
- Create: `Проекты/DocGen/app/tools/build_default_docx_template.py`
- Create: `Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light.docx`
- Create: `Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light-docx.yaml`
- Create: `Проекты/DocGen/app/tests/export/test_docx.py`

**Interfaces:**
- Produces: `DocxExporter.render(document: WorkingDocument, template: FormattingTemplate) -> RenderedFile`
- Produces: repeatable command `python tools/build_default_docx_template.py`

- [ ] **Step 1: Write structural DOCX tests**

```python
def test_docx_uses_template_styles_headers_and_tables(document_all_kinds, docx_template):
    rendered = DocxExporter(image_loader=fake_image_loader).render(document_all_kinds, docx_template)
    package = Document(BytesIO(rendered.content))
    assert package.paragraphs[0].style.name == "DG Title"
    assert package.sections[0].header.paragraphs[0].text == "DocGen"
    assert len(package.tables) == 1
    assert package.core_properties.title == document_all_kinds.title
```

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/export/test_docx.py -v`

Expected: FAIL because `DocxExporter` and its asset do not exist.

- [ ] **Step 3: Build the deterministic base template**

The script creates a blank `Document`, 20 mm page margins, header `DocGen`, footer with `PAGE` field, and styles `DG Title`, `DG Heading 1`–`DG Heading 6`, `DG Body`, `DG List`, `DG Gap`, `DG Caption`, and `DG Table`. Set Arial as the Word-safe fallback, exact DocGen colors, spacing and table borders. Saving twice from the same dependency version must produce functionally identical style definitions; tests compare XML properties, not ZIP bytes.

- [ ] **Step 4: Implement node-to-Word rendering**

Load the selected `.docx`, remove any body placeholder paragraph, append nodes using style mapping from YAML, set numbered/bulleted paragraphs, create tables with header row repeat, insert validated local images with configured width/alignment, add alt text to drawing properties, and render gaps with `DG Gap`. Never mutate the template asset in place.

- [ ] **Step 5: Generate asset, run tests and commit**

Run:

```bash
cd Проекты/DocGen/app
python tools/build_default_docx_template.py
python -m pytest tests/export/test_docx.py -v
```

Expected: template file exists; tests PASS.

```bash
git add Проекты/DocGen/app/src/docgen/export/docx.py Проекты/DocGen/app/tools Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light.docx Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light-docx.yaml Проекты/DocGen/app/tests/export/test_docx.py
git commit -m "feat: export styled DocGen DOCX"
```

### Task 5: Implement PDF export

**Files:**
- Modify: `Проекты/DocGen/app/pyproject.toml`
- Create: `Проекты/DocGen/app/src/docgen/export/pdf.py`
- Create: `Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light-pdf.html.j2`
- Create: `Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light-pdf.css`
- Create: `Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light-pdf.yaml`
- Create: `Проекты/DocGen/app/tests/export/test_pdf.py`

**Interfaces:**
- Produces: `PdfExporter.render(document: WorkingDocument, template: FormattingTemplate) -> RenderedFile`

- [ ] **Step 1: Add WeasyPrint and write PDF tests**

Add `weasyprint>=65,<66`.

```python
def test_pdf_has_valid_header_and_expected_text(document_all_kinds, pdf_template):
    rendered = PdfExporter(image_loader=fake_image_loader).render(document_all_kinds, pdf_template)
    assert rendered.content.startswith(b"%PDF-")
    pdf = pymupdf.open(stream=rendered.content, filetype="pdf")
    text = "".join(page.get_text() for page in pdf)
    assert "Заголовок" in text
    assert "DocGen" in text
```

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/export/test_pdf.py -v`

Expected: FAIL because `PdfExporter` does not exist.

- [ ] **Step 3: Implement print-specific HTML and CSS**

Reuse the safe HTML node renderer but a separate print template. Define A4, 20 mm margins, running header `DocGen`, page-number footer, `break-inside: avoid` for tables/figures, repeating table headers, embedded images and fonts available inside the contour. Call `HTML(string=html, base_url=validated_template_dir).write_pdf()` and map engine failures to `ExportError("Не удалось сформировать PDF")` without changing project state.

- [ ] **Step 4: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/export/test_pdf.py -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/pyproject.toml Проекты/DocGen/app/src/docgen/export/pdf.py Проекты/DocGen/app/src/docgen/formatting/templates/docgen-light-pdf* Проекты/DocGen/app/tests/export/test_pdf.py
git commit -m "feat: export styled DocGen PDF"
```

### Task 6: Add atomic export storage and service

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/export/storage.py`
- Create: `Проекты/DocGen/app/src/docgen/export/service.py`
- Modify: `Проекты/DocGen/app/src/docgen/jobs/models.py`
- Modify: `Проекты/DocGen/app/src/docgen/jobs/runner.py`
- Create: `Проекты/DocGen/app/tests/export/test_service.py`

**Interfaces:**
- Produces: `ExportRequest(project_id, document_revision, format, template_id)`
- Produces: `ExportResult(relative_path, filename, media_type, size_bytes, document_revision)`
- Produces: `ExportService.export(request: ExportRequest) -> ExportResult`
- Adds: `JobKind.EXPORT`

- [ ] **Step 1: Write failing service tests**

```python
def test_export_uses_exact_document_revision(export_service, stored_document):
    result = export_service.export(ExportRequest(project_id="p1", document_revision=4, format=OutputFormat.HTML, template_id="docgen-light"))
    assert result.document_revision == 4
    assert export_storage.resolve(result.relative_path).exists()


def test_failed_export_preserves_previous_file_and_document(export_service, failing_exporter):
    previous = seed_export_file("p1", "document.html", b"old")
    with pytest.raises(ExportError):
        export_service.export(html_request(revision=4))
    assert previous.read_bytes() == b"old"
    assert document_repository.get_document("p1").revision == 4
```

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/export/test_service.py -v`

Expected: FAIL because export service does not exist.

- [ ] **Step 3: Implement exporter registry and revision validation**

Map each `OutputFormat` to exactly one exporter. Load template with both format and ID. Reject a request when its revision differs from the current document with `Документ изменён; запустите экспорт повторно`. Render fully in memory, verify non-empty content and expected file signature/encoding, then write.

- [ ] **Step 4: Implement atomic export storage**

Write to `<project>/exports/.<format>-<template>.part`, fsync, then `Path.replace()` `<project>/exports/<safe-title>-<template>.<ext>`. Store result metadata on the job. Delete `.part` on failure; do not remove the last successful target.

- [ ] **Step 5: Register export jobs, run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/export/test_service.py tests/jobs -v`

Expected: PASS.

```bash
git add Проекты/DocGen/app/src/docgen/export Проекты/DocGen/app/src/docgen/jobs Проекты/DocGen/app/tests/export/test_service.py
git commit -m "feat: run atomic DocGen exports"
```

### Task 7: Add format/template selection and download routes

**Files:**
- Create: `Проекты/DocGen/app/src/docgen/export/routes.py`
- Create: `Проекты/DocGen/app/src/docgen/templates/export/select.html`
- Create: `Проекты/DocGen/app/src/docgen/templates/export/status.html`
- Create: `Проекты/DocGen/app/src/docgen/templates/export/error.html`
- Modify: `Проекты/DocGen/app/src/docgen/templates/editor/document.html`
- Modify: `Проекты/DocGen/app/src/docgen/main.py`
- Create: `Проекты/DocGen/app/tests/export/test_routes.py`

**Interfaces:**
- Produces: `GET /projects/{id}/export/templates?format=<format>`
- Produces: `POST /projects/{id}/export`
- Produces: `GET /projects/{id}/exports/{job_id}/status`
- Produces: `GET /projects/{id}/exports/{job_id}/download`

- [ ] **Step 1: Write failing selection and download tests**

```python
def test_format_selection_returns_only_matching_templates(client, project_with_document):
    response = client.get(f"/projects/{project_with_document.id}/export/templates?format=docx")
    assert response.status_code == 200
    assert "DocGen Light" in response.text
    assert 'value="docgen-light"' in response.text


def test_download_has_safe_headers(client, completed_export_job):
    response = client.get(f"/projects/p1/exports/{completed_export_job.id}/download")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/")
    assert "attachment" in response.headers["content-disposition"]
```

Also test invalid format/template pair, stale revision, pending job 409, failed job 409, another project's job 404, and missing file 410.

- [ ] **Step 2: Run and verify failure**

Run: `cd Проекты/DocGen/app && python -m pytest tests/export/test_routes.py -v`

Expected: FAIL with 404.

- [ ] **Step 3: Implement dependent selectors and job creation**

Changing format sends HTMX GET and replaces only the template selector. Submit includes current document revision, format and template ID. Return 202 status fragment and poll the existing job endpoint every two seconds until completion. Display `Формирование файла…`, a download button on success, and a retry action on failure.

- [ ] **Step 4: Implement secure download**

Resolve paths only through `ExportStorage`, verify job ownership and succeeded status, and set media types: DOCX `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, PDF `application/pdf`, HTML `text/html; charset=utf-8`, Markdown `text/markdown; charset=utf-8`. Use RFC 5987 UTF-8 filename encoding and prevent inline display with `Content-Disposition: attachment`.

- [ ] **Step 5: Run tests and commit**

Run: `cd Проекты/DocGen/app && python -m pytest tests/export/test_routes.py -v && python -m ruff check .`

Expected: PASS; no Ruff errors.

```bash
git add Проекты/DocGen/app/src/docgen/export/routes.py Проекты/DocGen/app/src/docgen/templates/export Проекты/DocGen/app/src/docgen/templates/editor/document.html Проекты/DocGen/app/src/docgen/main.py Проекты/DocGen/app/tests/export/test_routes.py
git commit -m "feat: add DocGen export interface"
```

### Task 8: Verify all formats and the Stage 4 journey

**Files:**
- Create: `Проекты/DocGen/app/tests/export/test_format_matrix.py`
- Create: `Проекты/DocGen/app/tests/test_stage4_journey.py`
- Modify: `Проекты/DocGen/app/README.md`

**Interfaces:**
- Consumes: all export catalogs, exporters, jobs and routes

- [ ] **Step 1: Add a format/template matrix test**

```python
@pytest.mark.parametrize("format_name,signature", [("docx", b"PK"), ("pdf", b"%PDF-"), ("html", b"<!doctype html>"), ("markdown", b"# ")])
def test_every_catalog_template_exports(format_name, signature, catalog, export_service):
    for template in catalog.list(OutputFormat(format_name)):
        result = export_service.export(request_for(format_name, template.id))
        assert export_storage.resolve(result.relative_path).read_bytes().lower().startswith(signature.lower())
```

- [ ] **Step 2: Add the full user-journey test**

Create a project with a saved document containing every node kind. For each format, request export, run the worker once, download the result, verify media type/signature and confirm document revision/content did not change. Force one exporter failure and assert the project, document and previous successful export remain intact.

- [ ] **Step 3: Run Stage 4 acceptance tests**

Run: `cd Проекты/DocGen/app && python -m pytest tests/export tests/test_stage4_journey.py -v`

Expected: PASS without external network access.

- [ ] **Step 4: Document template authoring and runtime dependencies**

Document manifest fields, allowed asset paths, format compatibility, how to add a second style, how to rebuild the default DOCX asset, WeasyPrint system prerequisites, supported node mapping and download behavior. State that administrators change configuration/assets on disk; there is no template-management UI.

- [ ] **Step 5: Run the complete MVP verification**

Run: `cd Проекты/DocGen/app && python -m pytest -v && python -m ruff check .`

Expected: all stages PASS; Ruff reports no errors.

- [ ] **Step 6: Commit**

```bash
git add Проекты/DocGen/app/tests Проекты/DocGen/app/README.md
git commit -m "test: verify DocGen export journey"
```
