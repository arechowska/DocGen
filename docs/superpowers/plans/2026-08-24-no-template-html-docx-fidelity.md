# No-template HTML DOCX fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve DOCX sections, editor-supported inline formatting, and nested lists during the first no-template HTML import, then faithfully export the edited document.

**Architecture:** Add an opt-in DOCX workspace-fidelity path to normalization. Only the no-template HTML first-build form enables it. It emits editor-ready blocks with rebased headings, safe rich fragments, and grouped nested lists; default extraction remains unchanged.

**Tech Stack:** Python 3.12, FastAPI, python-docx, lxml, BeautifulSoup, Jinja2, pytest, Node offline UI tests.

## Global Constraints

- Enable rich DOCX import only for semantic template `no-template` and format `html`.
- Import on first build only; repeat build exports the saved editor revision without rereading a source.
- Preserve only tags and CSS properties accepted by the editor sanitizer.
- Keep other templates, formats, source types, and normal DOCX extraction unchanged.
- Retain DOCX size/archive/page-limit safeguards.

---

### Task 1: Opt-in DOCX workspace-fidelity extraction

**Files:**
- Modify: `app/src/docgen/extraction/docx.py`
- Modify: `app/src/docgen/workflows/normalize.py`
- Modify: `app/src/docgen/workflows/conversion.py`
- Test: `app/tests/extraction/test_docx.py`
- Test: `app/tests/workflows/test_conversion.py`

**Interfaces:**
- Produces: `DocxExtractor.extract_workspace(source: Source, path: Path) -> ExtractionResult`.
- Produces: `NormalizationWorkflow.run(..., workspace_docx_fidelity: bool = False) -> NormalizedProject`.
- Produces: `conversion_document(..., rebase_heading_levels: bool = False) -> WorkingDocument`.

- [ ] **Step 1: Write failing tests.**

Create a DOCX fixture with outline-level-2 headings, bold/italic/underline/strike/color runs, hyperlink, and contiguous numbered/bulleted list paragraphs with a nested bullet. Assert regular `extract()` retains the old flat shape. Assert `extract_workspace()` provides rich data:

```python
assert result.blocks[1].data['html'] == '<strong>Bold</strong><em>Italic</em><u>Under</u><s>Strike</s><span style="color:#ff0000">Red</span><a href="https://example.test">Link</a>'
assert result.blocks[2].data == {
    'ordered': True,
    'items': ['First', 'Second'],
    'items_html': ['First', 'Second<ul><li>Nested</li></ul>'],
}
```

Add a conversion test:

```python
document = conversion_document(blocks, 'Guide', rebase_heading_levels=True)
assert [node.data['level'] for node in document.nodes if node.kind is NodeKind.HEADING] == [1, 2]
```

- [ ] **Step 2: Verify RED.**

Run: `pytest app/tests/extraction/test_docx.py app/tests/workflows/test_conversion.py -q`

Expected: FAIL because the rich extractor and heading-rebase option do not exist.

- [ ] **Step 3: Implement the isolated path.**

Keep `DocxExtractor.extract()` behavior unchanged. Add `extract_workspace()` reusing its guarded package opening, document-order iteration, and page calculation. Build escaped fragments from Word runs using only `strong`, `em`, `u`, `s`, `span style="color:..."`, and safe `a` URLs. Read `numId`/`ilvl` and Word numbering format; group adjacent compatible list paragraphs into blocks with `ordered`, `items`, and `items_html`. Encode nested lists in the parent item HTML.

In `normalize.py`, call this method only when `workspace_docx_fidelity=True` and the chosen extractor is `DocxExtractor`; preserve source IDs, warnings, and page limits. In `conversion.py`, when `rebase_heading_levels=True`, subtract the smallest heading level from every heading, clamped to 1..6; default behavior stays unchanged.

- [ ] **Step 4: Verify GREEN.**

Run: `pytest app/tests/extraction/test_docx.py app/tests/workflows/test_conversion.py -q`

Expected: PASS, including existing normal-extraction tests.

- [ ] **Step 5: Commit.**

```bash
git add app/src/docgen/extraction/docx.py app/src/docgen/workflows/normalize.py app/src/docgen/workflows/conversion.py app/tests/extraction/test_docx.py app/tests/workflows/test_conversion.py
git commit -m 'feat: preserve DOCX fidelity for workspace import'
```

### Task 2: Restrict the rich path to the target first-build flow

**Files:**
- Modify: `app/src/docgen/editor/routes.py:54-123`
- Modify: `app/src/docgen/templates/projects/source_panel.html:112-118`
- Modify: `app/src/docgen/static/js/docgen2-editor.js:55-95`
- Test: `app/tests/editor/test_routes.py`
- Test: `app/tests/test_offline_ui.py`

**Interfaces:**
- Consumes: Task 1 workflow flag and conversion argument.
- Produces: `POST /projects/{project_id}/editor/import-source` accepts optional `import_profile=no-template-html`.
- Produces: `[data-editor-import-profile]` is synchronized only for the target selection.

- [ ] **Step 1: Write failing route and UI tests.**

Create a DOCX source with outline-level-2 headings and a rich list; post the target profile and assert redirect, revision 1, H1-rebased first heading, and `ordered is True`. Post without the field and assert existing normal conversion. Add Node coverage for `synchronizeConversion()`:

```js
if (importProfile) importProfile.value = htmlWithoutTemplate ? 'no-template-html' : '';
```

- [ ] **Step 2: Verify RED.**

Run: `pytest app/tests/editor/test_routes.py -k 'import_source' -q; pytest app/tests/test_offline_ui.py -k 'import_profile' -q`

Expected: FAIL because profile transmission and the target branch are absent.

- [ ] **Step 3: Wire the form and route.**

Add this field to `#editorImportForm`:

```html
<input type="hidden" name="import_profile" value="" data-editor-import-profile>
```

In `synchronizeConversion()`, populate it with `no-template-html` only when `htmlWithoutTemplate` is true. In `import_source_into_editor`, receive `import_profile: Annotated[str | None, Form()] = None`; derive `workspace_fidelity = import_profile == 'no-template-html'`; pass it to `NormalizationWorkflow.run()` and `conversion_document()`. Preserve all existing source-count checks, conflicts, provenance, redirect, and default behavior.

- [ ] **Step 4: Verify GREEN.**

Run: `pytest app/tests/editor/test_routes.py -k 'import_source' -q; pytest app/tests/test_offline_ui.py -k 'import_profile' -q`

Expected: PASS; regular imports retain their old document shape.

- [ ] **Step 5: Commit.**

```bash
git add app/src/docgen/editor/routes.py app/src/docgen/templates/projects/source_panel.html app/src/docgen/static/js/docgen2-editor.js app/tests/editor/test_routes.py app/tests/test_offline_ui.py
git commit -m 'feat: enable rich DOCX import for no-template HTML'
```

### Task 3: Preserve nested lists in the standalone HTML and test the full flow

**Files:**
- Modify: `app/src/docgen/export/html.py:20-35, 205-255`
- Modify: `app/src/docgen/formatting/templates/docgen-light.html.j2:15-32`
- Test: `app/tests/export/test_html.py`
- Test: `app/tests/editor/test_routes.py`

**Interfaces:**
- Produces: `safe_rich_html(value, fallback='', *, allow_lists=False) -> Markup`.
- Nested `ol`, `ul`, and `li` are retained only where `allow_lists=True`.

- [ ] **Step 1: Write failing export and integration tests.**

Build a document with two H1 headings and a list item `Second<ul><li><strong>Nested</strong></li></ul>`. Assert the result has contents, both anchors, parent `ol`, nested `ul`, and `strong`. Extend Task 2’s route test: save revision 2, export revision 2, assert stored HTML retains contents/nested rich list, and assert only the first build imported a source.

- [ ] **Step 2: Verify RED.**

Run: `pytest app/tests/export/test_html.py app/tests/editor/test_routes.py -k 'nested or no_template_html' -q`

Expected: FAIL because `safe_rich_html` unwraps structural list tags.

- [ ] **Step 3: Implement narrow list sanitization.**

Change the helper as follows:

```python
def safe_rich_html(value: object, fallback: str = '', *, allow_lists: bool = False) -> Markup:
    allowed_tags = _RICH_TAGS | (_RICH_LIST_TAGS if allow_lists else frozenset())
```

Keep existing attribute filtering. In the list branch of `docgen-light.html.j2`, call `rich_html(..., item, allow_lists=True)`; heading and paragraph calls keep their default.

- [ ] **Step 4: Verify GREEN and quality checks.**

Run:

```bash
pytest app/tests/export/test_html.py app/tests/editor/test_routes.py -k 'nested or no_template_html' -q
ruff check app/src/docgen/extraction/docx.py app/src/docgen/workflows/normalize.py app/src/docgen/workflows/conversion.py app/src/docgen/editor/routes.py app/src/docgen/export/html.py app/tests/extraction/test_docx.py app/tests/workflows/test_conversion.py app/tests/editor/test_routes.py app/tests/export/test_html.py app/tests/test_offline_ui.py
git diff --check
```

Expected: PASS with no whitespace errors.

- [ ] **Step 5: Commit.**

```bash
git add app/src/docgen/export/html.py app/src/docgen/formatting/templates/docgen-light.html.j2 app/tests/export/test_html.py app/tests/editor/test_routes.py
git commit -m 'fix: preserve nested DOCX lists in HTML export'
```
