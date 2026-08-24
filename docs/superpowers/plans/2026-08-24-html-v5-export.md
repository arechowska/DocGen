# HTML v5 Result Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export the saved DocGen editor result as one autonomous HTML file that preserves supported rich formatting and uses the Colvir one-page template v5 visual language.

**Architecture:** Keep `WorkingDocument` as the only export input. Extend `HtmlExporter` with narrowly scoped preparation helpers for sanitized rich fragments, node styles, section grouping, unique anchors, and embedded font assets; render those prepared values through the existing catalog-selected Jinja template. Replace only the HTML template resources and leave the editor, routes, service, document models, and other exporters unchanged.

**Tech Stack:** Python 3.12+, BeautifulSoup 4, Jinja2 3.1, MarkupSafe, Pydantic document models, pytest 8, standalone HTML/CSS with embedded OTF/TTF and image data URLs.

## Global Constraints

- Modify only `app/src/docgen/export/html.py`, the `docgen-light` HTML manifest/assets under `app/src/docgen/formatting/templates/`, and HTML-export tests.
- Do not modify DocGen UI templates, editor JavaScript, document models, routes, export service, or DOCX/PDF/Markdown exporters.
- Use the saved `WorkingDocument` revision already supplied by `ExportService`; do not read `workspace_html` separately.
- Use `Материалы/Шаблоны/Шаблон страницы/versions/v5/one-page-course-template.html` as the visual source.
- Produce one UTF-8 HTML file with no CDN, network-loaded stylesheet, script, font, icon, or image.
- Preserve safe editor rich content and normalized styles while removing scripts, event handlers, unsafe URLs, and unsupported attributes.
- Show a contents card only when the document has at least two top-level section headings.
- Do not alter current filename generation, storage, route, or inline-open behavior.
- Follow TDD: every behavior change starts with a focused failing test.

---

### Task 1: Preserve and sanitize editor rich content

**Files:**
- Modify: `app/tests/export/test_html.py`
- Modify: `app/src/docgen/export/html.py`
- Modify: `app/src/docgen/formatting/templates/docgen-light.html.j2`

**Interfaces:**
- Consumes: `DocumentNode.data["html"]`, `DocumentNode.data["items_html"]`, `DocumentNode.data["style"]`, and `DocumentNode.data["item_styles"]`.
- Produces: `safe_rich_html(value: object, fallback: str = "") -> Markup`.
- Produces: `safe_style_attribute(value: object) -> Markup`.
- Produces: Jinja context callables `rich_html` and `style_attribute`.

- [ ] **Step 1: Add failing paragraph rich-content and XSS tests**

Add tests that build a paragraph with both safe formatting and hostile markup:

```python
def test_html_preserves_safe_editor_rich_text_and_removes_xss(html_template):
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-html",
        nodes=[
            DocumentNode(
                kind=NodeKind.PARAGRAPH,
                text="Важный текст",
                data={
                    "html": (
                        '<strong>Важный</strong> <em>текст</em>'
                        '<script>alert(1)</script>'
                        '<a href="javascript:alert(2)" onclick="alert(3)">ссылка</a>'
                    ),
                    "style": {"text-align": "center", "position": "fixed"},
                },
            )
        ],
    )

    html = HtmlExporter().render(document, html_template).content.decode("utf-8")

    assert "<strong>Важный</strong>" in html
    assert "<em>текст</em>" in html
    assert "text-align:center" in html
    assert "<script" not in html
    assert "javascript:" not in html
    assert "onclick" not in html
    assert "position:" not in html
```

- [ ] **Step 2: Run the paragraph test and verify RED**

Run: `cd app && python -m pytest tests/export/test_html.py::test_html_preserves_safe_editor_rich_text_and_removes_xss -v`

Expected: FAIL because the current template renders only `node.text` and escapes the stored rich fragment.

- [ ] **Step 3: Implement the minimal rich-fragment sanitizer**

In `html.py`, use `BeautifulSoup` and `Markup` with an explicit allowlist:

```python
_RICH_TAGS = frozenset({"a", "b", "br", "code", "em", "i", "mark", "s", "span", "strong", "sub", "sup", "u"})
_RICH_ATTRIBUTES = {"a": frozenset({"href", "title"}), "span": frozenset({"style"})}

def safe_rich_html(value: object, fallback: str = "") -> Markup:
    source = value if isinstance(value, str) and value else escape(fallback)
    soup = BeautifulSoup(str(source), "html.parser")
    # Remove script/style/template nodes; unwrap unsupported tags; retain only
    # allowlisted attributes; normalize style with normalized_style_attribute;
    # reject href schemes other than #, /, http://, https://, and mailto:.
    return Markup("".join(str(item) for item in soup.contents))

def safe_style_attribute(value: object) -> Markup:
    if isinstance(value, dict):
        style = normalized_style_attribute(";".join(f"{k}:{v}" for k, v in value.items()))
    elif isinstance(value, str):
        style = normalized_style_attribute(value)
    else:
        style = ""
    return Markup(f' style="{escape(style)}"') if style else Markup("")
```

Pass both helpers into `jinja_template.render(...)`. Update paragraph and heading branches to render `rich_html(node.data.get("html"), node.text or "")` and apply `style_attribute(node.data.get("style"))`.

- [ ] **Step 4: Run the paragraph test and verify GREEN**

Run: `cd app && python -m pytest tests/export/test_html.py::test_html_preserves_safe_editor_rich_text_and_removes_xss -v`

Expected: PASS.

- [ ] **Step 5: Add failing rich-list tests**

```python
def test_html_preserves_rich_list_items_and_individual_styles(html_template):
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-html",
        nodes=[DocumentNode(
            kind=NodeKind.LIST,
            data={
                "ordered": True,
                "items": ["Первый", "Второй"],
                "items_html": ["<strong>Первый</strong>", "<em>Второй</em>"],
                "item_styles": ["text-align:left", "text-align:right;position:fixed"],
                "style": {"margin-left": "24px"},
            },
        )],
    )

    html = HtmlExporter().render(document, html_template).content.decode("utf-8")

    assert "<strong>Первый</strong>" in html
    assert "<em>Второй</em>" in html
    assert "text-align:right" in html
    assert "margin-left:24px" in html
    assert "position:" not in html
```

- [ ] **Step 6: Run the list test and verify RED**

Run: `cd app && python -m pytest tests/export/test_html.py::test_html_preserves_rich_list_items_and_individual_styles -v`

Expected: FAIL because the template uses only `items` and ignores list/item styles.

- [ ] **Step 7: Render rich list values with safe fallbacks**

In `docgen-light.html.j2`, index `items_html` and `item_styles` defensively. For every item, use its rich value only when the list has a matching index; otherwise use the plain item. Apply the normalized list style to `<ol>/<ul>` and the matching normalized item style to `<li>`.

- [ ] **Step 8: Run focused and full HTML tests**

Run: `cd app && python -m pytest tests/export/test_html.py -v`

Expected: all HTML exporter tests PASS.

- [ ] **Step 9: Commit the rich-content slice**

```bash
git add app/src/docgen/export/html.py app/src/docgen/formatting/templates/docgen-light.html.j2 app/tests/export/test_html.py
git commit -m "feat: preserve rich editor content in HTML export"
```

### Task 2: Group editor content into sections and build contents links

**Files:**
- Modify: `app/tests/export/test_html.py`
- Modify: `app/src/docgen/export/html.py`
- Modify: `app/src/docgen/formatting/templates/docgen-light.html.j2`

**Interfaces:**
- Consumes: `WorkingDocument.nodes` in saved editor order.
- Produces: frozen dataclass `HtmlSection(anchor: str | None, heading: DocumentNode | None, nodes: tuple[DocumentNode, ...])`.
- Produces: frozen dataclass `HtmlDocumentView(title: str, introduction: tuple[DocumentNode, ...], sections: tuple[HtmlSection, ...], show_contents: bool)`.
- Produces: `prepare_document_view(document: WorkingDocument) -> HtmlDocumentView`.

- [ ] **Step 1: Add failing tests for conditional contents and section cards**

```python
def test_html_builds_contents_for_two_level_one_sections(html_template):
    document = WorkingDocument(
        title="Документ",
        template_id="docgen-light-html",
        nodes=[
            DocumentNode(kind=NodeKind.PARAGRAPH, text="Введение"),
            DocumentNode(kind=NodeKind.HEADING, text="Первый раздел", data={"level": 1}),
            DocumentNode(kind=NodeKind.PARAGRAPH, text="Первое содержание"),
            DocumentNode(kind=NodeKind.HEADING, text="Второй раздел", data={"level": 1}),
            DocumentNode(kind=NodeKind.PARAGRAPH, text="Второе содержание"),
        ],
    )

    html = HtmlExporter().render(document, html_template).content.decode("utf-8")

    assert '<nav id="contents"' in html
    assert 'href="#section-1"' in html
    assert 'href="#section-2"' in html
    assert html.index("Введение") < html.index("Первый раздел")
    assert html.count('class="section card-shell') == 3


def test_html_omits_contents_for_zero_or_one_section(html_template):
    for headings in ([], ["Единственный раздел"]):
        nodes = [DocumentNode(kind=NodeKind.HEADING, text=text, data={"level": 1}) for text in headings]
        document = WorkingDocument(title="Документ", template_id="docgen-light-html", nodes=nodes)
        html = HtmlExporter().render(document, html_template).content.decode("utf-8")
        assert '<nav id="contents"' not in html
```

- [ ] **Step 2: Run both tests and verify RED**

Run: `cd app && python -m pytest tests/export/test_html.py -k "contents_for_two or omits_contents" -v`

Expected: FAIL because the exporter has no prepared section view or conditional contents card.

- [ ] **Step 3: Implement deterministic section preparation**

Add `HtmlSection`, `HtmlDocumentView`, and `prepare_document_view`. A top-level heading is a section boundary only when `node.kind is NodeKind.HEADING` and its normalized level is `1`. Nodes before the first boundary go to `introduction`; following nodes go to the current section. Assign anchors by encounter order as `section-1`, `section-2`, and so on, avoiding text-derived unsafe IDs. Set `show_contents = len(sections) >= 2`.

Pass `view=prepare_document_view(document)` to Jinja. Render the introduction as one card when nonempty, render every section as one card, and emit a `<nav id="contents">` only when `view.show_contents` is true.

- [ ] **Step 4: Run both tests and verify GREEN**

Run: `cd app && python -m pytest tests/export/test_html.py -k "contents_for_two or omits_contents" -v`

Expected: PASS.

- [ ] **Step 5: Add and pass nested-node and repeated-heading regression tests**

Add a test with two identically named level-one headings, a level-two heading inside the first section, and nested child nodes. Assert anchors remain exactly `section-1` and `section-2`, the level-two heading stays inside the first section, and every nested child still appears once.

Run: `cd app && python -m pytest tests/export/test_html.py -k "section or nested" -v`

Expected: PASS with no duplicate IDs or duplicated node content.

- [ ] **Step 6: Run all HTML tests and commit**

Run: `cd app && python -m pytest tests/export/test_html.py -v`

Expected: all tests PASS.

```bash
git add app/src/docgen/export/html.py app/src/docgen/formatting/templates/docgen-light.html.j2 app/tests/export/test_html.py
git commit -m "feat: structure HTML export as linked sections"
```

### Task 3: Apply the v5 page design with embedded local resources

**Files:**
- Modify: `app/tests/export/test_html.py`
- Modify: `app/src/docgen/export/html.py`
- Modify: `app/src/docgen/formatting/templates/docgen-light-html.yaml`
- Replace: `app/src/docgen/formatting/templates/docgen-light.html.j2`
- Replace: `app/src/docgen/formatting/templates/docgen-light.css`
- Create: `app/src/docgen/formatting/templates/Akrobat-Bold.otf`
- Create: `app/src/docgen/formatting/templates/Roboto-Regular.ttf`
- Create: `app/src/docgen/formatting/templates/Roboto-Light.ttf`
- Create: `app/src/docgen/formatting/templates/Roboto-Bold.ttf`

**Interfaces:**
- Consumes: the four workspace font sources under `D:/AI/Материалы/Фирменный стиль/Шрифты/`.
- Produces: `asset_data_url(name: str) -> str` restricted to catalog-declared assets and the template directory.
- Produces: autonomous v5-derived HTML with hero, cards, contents navigation, tables, lists, gap blocks, images, print rules, and an inline-SVG back-to-top control.

- [ ] **Step 1: Add failing v5 identity and autonomy tests**

```python
def test_html_uses_v5_visual_tokens_and_embeds_fonts(html_template):
    document = WorkingDocument(title="Документ", template_id="docgen-light-html", nodes=[])
    html = HtmlExporter().render(document, html_template).content.decode("utf-8")

    for token in ("#f4f7fb", "#17324a", "#1163AE", "#0f3f69", "border-radius:22px"):
        assert token in html
    assert 'font-family:"Akrobat"' in html
    assert 'font-family:"Roboto"' in html
    assert "data:font/" in html


def test_html_v5_export_has_no_external_runtime_dependency(document_with_image, html_template):
    html = HtmlExporter(image_loader=fake_image_loader).render(document_with_image, html_template).content.decode("utf-8")

    assert "cdn.jsdelivr.net" not in html
    assert "<link" not in html
    assert "<script" not in html
    assert "@import" not in html
    assert "url(http" not in html
    assert "data:image/png;base64," in html
    assert "<svg" in html
```

- [ ] **Step 2: Run both tests and verify RED**

Run: `cd app && python -m pytest tests/export/test_html.py -k "v5_visual or external_runtime" -v`

Expected: FAIL because current DocGen Light assets use a different visual system and do not embed fonts.

- [ ] **Step 3: Copy the exact v5 font files into the formatting catalog**

Copy these files without modifying their bytes:

```text
D:/AI/Материалы/Фирменный стиль/Шрифты/Akrobat/Akrobat-Bold.otf
  -> app/src/docgen/formatting/templates/Akrobat-Bold.otf
D:/AI/Материалы/Фирменный стиль/Шрифты/Roboto/Roboto-Regular.ttf
  -> app/src/docgen/formatting/templates/Roboto-Regular.ttf
D:/AI/Материалы/Фирменный стиль/Шрифты/Roboto/Roboto-Light.ttf
  -> app/src/docgen/formatting/templates/Roboto-Light.ttf
D:/AI/Материалы/Фирменный стиль/Шрифты/Roboto/Roboto-Bold.ttf
  -> app/src/docgen/formatting/templates/Roboto-Bold.ttf
```

Add all four filenames to `docgen-light-html.yaml.assets` so catalog validation controls access.

- [ ] **Step 4: Implement catalog-restricted font embedding**

Add `_read_asset_bytes(name)` with the same resolved-path containment check as `_read_asset_text`. Add `asset_data_url(name)` that rejects undeclared assets, derives `font/otf` for `.otf` and `font/ttf` for `.ttf`, base64-encodes the bytes, and returns a data URL. Pass a mapping for the four font files into Jinja so the template emits four explicit `@font-face` declarations with weights 700, 400, 300, and 700 respectively.

- [ ] **Step 5: Replace the page shell and CSS with the required v5 subset**

Port the v5 variables and visual rules exactly where applicable:

```css
:root {
  --bg:#f4f7fb;
  --surface:#ffffff;
  --surface-soft:#f8fbfe;
  --text:#17324a;
  --muted:#5b7286;
  --primary:#1163AE;
  --primary-dark:#0f3f69;
  --line:#d7e3ee;
  --shadow:0 16px 40px rgba(15,63,105,.10);
}
.card-shell {
  background:rgba(255,255,255,.96);
  border:1px solid var(--line);
  box-shadow:var(--shadow);
  border-radius:22px;
}
```

Implement the Bootstrap layout classes actually used by v5 directly in semantic project CSS instead of copying Bootstrap: a centered max-width page shell, responsive padding, hero, section card, left accent, lists, horizontally scrollable tables, figures, gap callouts, and print margins. Use an inline SVG inside the fixed back-to-top link and target `#contents` when contents exists, otherwise `#top`.

- [ ] **Step 6: Run the v5 tests and verify GREEN**

Run: `cd app && python -m pytest tests/export/test_html.py -k "v5_visual or external_runtime" -v`

Expected: PASS.

- [ ] **Step 7: Run catalog and complete HTML tests**

Run: `cd app && python -m pytest tests/formatting/test_catalog.py tests/export/test_html.py -v`

Expected: all selected tests PASS and the catalog accepts every declared font asset.

- [ ] **Step 8: Commit the v5 asset slice**

```bash
git add app/src/docgen/export/html.py app/src/docgen/formatting/templates/docgen-light-html.yaml app/src/docgen/formatting/templates/docgen-light.html.j2 app/src/docgen/formatting/templates/docgen-light.css app/src/docgen/formatting/templates/Akrobat-Bold.otf app/src/docgen/formatting/templates/Roboto-Regular.ttf app/src/docgen/formatting/templates/Roboto-Light.ttf app/src/docgen/formatting/templates/Roboto-Bold.ttf app/tests/export/test_html.py
git commit -m "feat: apply autonomous v5 page design to HTML export"
```

### Task 4: Verify the complete HTML export without changing other behavior

**Files:**
- Modify only if a missing assertion is demonstrated: `app/tests/export/test_html.py`
- No production files beyond the scope already listed.

**Interfaces:**
- Consumes: the completed `HtmlExporter`, catalog assets, existing export service, storage, and HTML open route.
- Produces: verification evidence that the HTML path works end to end and other export paths remain unchanged.

- [ ] **Step 1: Run focused HTML/export integration tests**

Run:

```bash
cd app
python -m pytest tests/export/test_html.py tests/export/test_service.py tests/export/test_routes.py tests/workflows/test_export.py -v
```

Expected: all selected tests PASS; existing route assertions still prove HTML opens inline and non-HTML downloads behave as before.

- [ ] **Step 2: Run formatting, editor, and all exporter regression tests**

Run:

```bash
cd app
python -m pytest tests/formatting tests/export tests/editor -v
```

Expected: all selected tests PASS with zero failures.

- [ ] **Step 3: Run static checks on changed Python and test files**

Run:

```bash
cd app
python -m ruff check src/docgen/export/html.py tests/export/test_html.py
```

Expected: exit code 0 with no diagnostics.

- [ ] **Step 4: Verify scope and autonomous-output invariants**

Run from repository root:

```bash
git diff --check
git status --short
git diff --name-only fc20747..HEAD
```

Expected: no whitespace errors; changed production files are limited to `html.py`, the `docgen-light` HTML manifest/assets, and the four font assets; tests are limited to HTML export; planning documents are the only other changes.

- [ ] **Step 5: Commit any final test-only correction, otherwise leave the verified commits unchanged**

If Step 1 demonstrates a missing end-to-end assertion, add only that assertion to `app/tests/export/test_html.py`, rerun Steps 1–4, then commit:

```bash
git add app/tests/export/test_html.py
git commit -m "test: verify v5 HTML export end to end"
```
