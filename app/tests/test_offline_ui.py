from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select

from docgen.jobs.models import Job
from docgen.sources.repository import SourceRepository

_NODE = shutil.which("node")
_requires_node = pytest.mark.skipif(
    _NODE is None,
    reason="Node.js is required for browser-script regression tests",
)


def test_ui_uses_local_assets_and_restrictive_csp(client: TestClient) -> None:
    response = client.get("/projects")

    assert response.status_code == 200
    assert 'src="/static/vendor/htmx-2.0.8.min.js"' in response.text
    page = BeautifulSoup(response.text, "html.parser")
    stylesheet_url = page.find("link", rel="stylesheet")["href"]
    editor_script_url = page.find("script", src=re.compile(r"docgen2-editor\.js"))["src"]
    assert stylesheet_url.startswith("/static/css/docgen.css?v=")
    assert editor_script_url.startswith("/static/js/docgen2-editor.js?v=")
    assert stylesheet_url.rsplit("?v=", 1)[1] == editor_script_url.rsplit("?v=", 1)[1]
    assert "https://cdn.tailwindcss.com" not in response.text
    assert "unpkg.com" not in response.text
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "unsafe-inline" not in csp
    assert client.get("/static/vendor/htmx-2.0.8.min.js").status_code == 200
    assert client.get("/static/js/docgen2-editor.js").status_code == 200
    stylesheet = client.get("/static/css/docgen.css")
    assert stylesheet.status_code == 200
    assert ".htmx-indicator{opacity:0}" in stylesheet.text
    assert ".htmx-request .htmx-indicator" in stylesheet.text


def test_htmx_error_fragments_are_swapped_into_target() -> None:
    base_template = Path(__file__).parents[1] / "src" / "docgen" / "templates" / "base.html"

    markup = base_template.read_text(encoding="utf-8")

    assert '"code":"[2345]..","swap":true' in markup
    assert '"code":"[45]..","swap":false' not in markup
    assert '"includeIndicatorStyles":false' in markup


def test_htmx_shows_safe_model_configuration_error(client: TestClient) -> None:
    """A missing model configuration must explain why a generation button did nothing."""
    page = BeautifulSoup(client.get("/projects").text, "html.parser")
    config = json.loads(page.find("meta", attrs={"name": "htmx-config"})["content"])

    rule = next(
        item
        for item in config["responseHandling"]
        if re.fullmatch(item["code"], "503")
    )

    assert rule["swap"] is True


@_requires_node
def test_chat_submit_sends_explicit_message_and_revision(client: TestClient) -> None:
    script = client.get("/static/js/docgen2-editor.js")
    assert script.status_code == 200
    harness = f"""
const documentListeners = new Map();
const input = {{ value: "Что за ошибки", focus() {{}} }};
const revision = {{ value: "4" }};
const button = {{ disabled: false }};
const banner = {{ hidden: true, textContent: "" }};
const messages = {{
  children: [],
  appendChild(node) {{
    node.remove = () => {{ this.children = this.children.filter((item) => item !== node); }};
    this.children.push(node);
  }},
}};
const form = {{
  action: "/projects/p1/chat",
  dataset: {{}},
  listeners: new Map(),
  addEventListener(type, listener) {{ this.listeners.set(type, listener); }},
  querySelector(selector) {{
    if (selector === "#chatInput") return input;
    if (selector === 'input[name="revision"]') return revision;
    if (selector === "#sendButton") return button;
    return null;
  }},
}};
let request = null;
globalThis.document = {{
  querySelector(selector) {{
    if (selector === "#chatForm") return form;
    if (selector === "#errorBanner") return banner;
    if (selector === "#chat-messages") return messages;
    return null;
  }},
  createElement() {{ return {{ className: "", textContent: "" }}; }},
  querySelectorAll() {{ return []; }},
  addEventListener(type, listener) {{ documentListeners.set(type, listener); }},
  execCommand() {{}},
}};
globalThis.window = {{
  getSelection() {{ return null; }},
  htmx: {{
    ajax(method, url, options) {{
      request = {{ method, url, options }};
      return Promise.resolve();
    }},
  }},
}};
{script.text}
let prevented = false;
await form.listeners.get("submit")({{ preventDefault() {{ prevented = true; }} }});
await new Promise((resolve) => setTimeout(resolve, 0));
if (!prevented) throw new Error("native submit was not stopped");
if (input.value !== "") throw new Error("message was not cleared after submit");
if (messages.children.length !== 1 || messages.children[0].textContent !== "Что за ошибки") {{
  throw new Error("sent message was not added to chat");
}}
if (request?.method !== "POST" || request?.url !== "/projects/p1/chat") {{
  throw new Error("chat request was not sent");
}}
if (request.options.values.message !== "Что за ошибки" || request.options.values.revision !== "4") {{
  throw new Error(`unexpected chat payload: ${{JSON.stringify(request.options.values)}}`);
}}
if (request.options.timeout !== 30000) throw new Error("chat timeout is missing");
"""

    subprocess.run(
        [_NODE or "node", "--input-type=module", "-e", harness],
        check=True,
        capture_output=True,
        text=True,
    )


@_requires_node
def test_chat_shows_thinking_state_until_request_finishes(client: TestClient) -> None:
    script = client.get("/static/js/docgen2-editor.js")
    assert script.status_code == 200
    harness = f"""
const input = {{ value: "Добавь вопросы" }};
const revision = {{ value: "4" }};
const button = {{ disabled: false }};
const banner = {{ hidden: true, textContent: "" }};
const statusText = {{ textContent: "Готово" }};
const statusBadge = {{ state: "ready", setAttribute(name, value) {{ if (name === "data-state") this.state = value; }} }};
const messages = {{
  children: [],
  appendChild(node) {{
    node.remove = () => {{ this.children = this.children.filter((item) => item !== node); }};
    this.children.push(node);
  }},
}};
const form = {{
  action: "/projects/p1/chat",
  dataset: {{}},
  listeners: new Map(),
  addEventListener(type, listener) {{ this.listeners.set(type, listener); }},
  querySelector(selector) {{
    if (selector === "#chatInput") return input;
    if (selector === 'input[name="revision"]') return revision;
    if (selector === "#sendButton") return button;
    return null;
  }},
}};
let finishRequest;
globalThis.document = {{
  querySelector(selector) {{
    if (selector === "#chatForm") return form;
    if (selector === "#errorBanner") return banner;
    if (selector === "#chat-messages") return messages;
    if (selector === "#statusBadge") return statusBadge;
    if (selector === "#statusText") return statusText;
    return null;
  }},
  createElement() {{ return {{ className: "", textContent: "" }}; }},
  querySelectorAll() {{ return []; }},
  addEventListener() {{}},
}};
globalThis.window = {{
  htmx: {{ ajax() {{ return new Promise((resolve) => {{ finishRequest = resolve; }}); }} }},
}};
{script.text}
form.listeners.get("submit")({{ preventDefault() {{}} }});
if (!button.disabled) throw new Error("send button stayed enabled");
if (statusText.textContent !== "Думаю…" || statusBadge.state !== "thinking") {{
  throw new Error("top status did not switch to thinking");
}}
if (messages.children.length !== 2 || messages.children[1].textContent !== "Думаю…") {{
  throw new Error("thinking message was not added to chat");
}}
finishRequest();
await new Promise((resolve) => setTimeout(resolve, 0));
if (button.disabled) throw new Error("send button stayed disabled");
if (statusText.textContent !== "Готово" || statusBadge.state !== "ready") {{
  throw new Error("top status did not return to ready");
}}
if (messages.children.length !== 1) throw new Error("thinking message was not removed");
"""

    subprocess.run(
        [_NODE or "node", "--input-type=module", "-e", harness],
        check=True,
        capture_output=True,
        text=True,
    )


@_requires_node
def test_chat_submit_reenables_button_when_htmx_throws(client: TestClient) -> None:
    script = client.get("/static/js/docgen2-editor.js")
    harness = f"""
const input = {{ value: "Добавь вопрос", focus() {{}} }};
const revision = {{ value: "4" }};
const button = {{ disabled: false }};
const banner = {{ hidden: true, textContent: "" }};
const form = {{
  action: "/projects/p1/chat",
  dataset: {{}},
  listeners: new Map(),
  addEventListener(type, listener) {{ this.listeners.set(type, listener); }},
  querySelector(selector) {{
    if (selector === "#chatInput") return input;
    if (selector === 'input[name="revision"]') return revision;
    if (selector === "#sendButton") return button;
    return null;
  }},
}};
globalThis.document = {{
  querySelector(selector) {{
    if (selector === "#chatForm") return form;
    if (selector === "#errorBanner") return banner;
    if (selector === "#chat-messages") return {{ appendChild() {{}} }};
    return null;
  }},
  createElement() {{ return {{ className: "", textContent: "" }}; }},
  querySelectorAll() {{ return []; }},
  addEventListener() {{}},
}};
globalThis.window = {{
  htmx: {{ ajax() {{ throw new Error("request failed"); }} }},
}};
{script.text}
let prevented = false;
form.listeners.get("submit")({{ preventDefault() {{ prevented = true; }} }});
await new Promise((resolve) => setTimeout(resolve, 0));
if (!prevented || button.disabled) throw new Error("button was not re-enabled");
if (banner.hidden || banner.textContent !== "Не удалось отправить сообщение") {{
  throw new Error("request error was not shown");
}}
"""

    subprocess.run(
        [_NODE or "node", "--input-type=module", "-e", harness],
        check=True,
        capture_output=True,
        text=True,
    )


def test_templates_have_no_inline_handlers_styles_or_public_executable_assets() -> None:
    template_root = Path(__file__).parents[1] / "src" / "docgen" / "templates"

    for path in template_root.rglob("*.html"):
        markup = path.read_text(encoding="utf-8")
        assert "hx-on" not in markup, path
        assert " style=" not in markup, path
        assert "onclick=" not in markup, path
        assert "onchange=" not in markup, path
        assert "https://cdn.tailwindcss.com" not in markup, path
        assert "fonts.googleapis.com" not in markup, path
        assert "unpkg.com" not in markup, path
        assert "lucide" not in markup.lower(), path


def test_workspace_css_contains_corporate_layout_tokens(client: TestClient) -> None:
    stylesheet = client.get("/static/css/docgen.css")

    assert stylesheet.status_code == 200
    css = stylesheet.text.lower()
    for token in (
        ".app-shell",
        ".topbar",
        ".mobile-tabs",
        ".workspace",
        "grid-template-columns:clamp(260px,16vw,320px) minmax(420px,1fr) clamp(280px,18vw,360px)",
        "grid-template-rows:minmax(0,1fr)",
        ".sources-panel",
        ".editor-card",
        ".editor-topline",
        ".document-canvas",
        ".tool-button",
        ".editor-save-status[data-state=saved]",
        "color:#28775f",
        ".editor-save-status[data-state=error]",
        "background:#fde7e7",
        ".table-menu",
        ".table-size-grid",
        ".chat-panel",
        "row-gap:12px",
        ".result-panel",
        ".project-card-link{min-height:84px;display:grid;grid-template-columns:44px minmax(0,1fr);align-items:center",
        ".project-delete-button{position:absolute;top:10px;right:10px",
        "#15569b",
        "#1b5eaa",
        "#202428",
        "#5f6873",
        "#f4f7fa",
        "#d6e5f8",
        "@media (max-width:1023px)",
    ):
        assert token in css


def test_workspace_css_uses_calm_formatta_palette(client: TestClient) -> None:
    """The served UI must not regress to the previous cyan gradient palette."""
    css = client.get("/static/css/docgen.css").text.lower()

    for token in (
        "--formatta-topbar:#15569b",
        "--formatta-primary:#1b5eaa",
        "--formatta-ink:#202428",
        "--formatta-muted:#5f6873",
        "--formatta-bg:#f4f7fa",
        "--formatta-border:#d6e5f8",
    ):
        assert token in css
    assert "#60bcff" not in css
    assert "#3196df" not in css


def test_auxiliary_workspace_screens_use_the_formatta_palette(client: TestClient) -> None:
    """Generated CSS for all reachable fragments must share the workspace theme."""
    css = client.get("/static/css/docgen.css").text.lower()

    for retired_colour in (
        "#0071e3",
        "#0066cc",
        "#101828",
        "#667085",
        "#e3e8ec",
        "#f9f9fc",
        "#f5f5f7",
        "#707070",
    ):
        assert retired_colour not in css


def test_workspace_topbar_and_document_heading_colours_have_scoped_roles(
    client: TestClient,
) -> None:
    """Blue identifies app actions and document styling, not application headings."""
    css = client.get("/static/css/docgen.css").text.lower()
    topbar = css[css.index(".topbar{") : css.index("}", css.index(".topbar{"))]

    assert "background:var(--formatta-topbar)" in topbar
    assert "linear-gradient" not in topbar
    assert (
        ".document-canvas h1,.document-canvas h2,.document-canvas h3,"
        ".document-canvas h4,.document-canvas h5{color:var(--formatta-primary)"
    ) in css
    assert ".panel-heading h1{margin:0;color:var(--lk-ink)" in css


def test_workspace_topbar_keeps_template_and_format_equally_wide(client: TestClient) -> None:
    """The export-style selector must not take space from the two primary selectors."""
    css = client.get("/static/css/docgen.css").text.lower()
    topbar = css[css.index(".topbar{") : css.index("}", css.index(".topbar{"))]

    assert (
        "grid-template-columns:minmax(150px,auto) minmax(300px,1fr) minmax(300px,1fr) "
        "minmax(220px,.65fr) auto"
    ) in topbar
    assert ".topbar-export-form{display:contents" in css


def test_heading_select_chevron_is_vertically_aligned_like_docgen2(
    client: TestClient,
) -> None:
    stylesheet = client.get("/static/css/docgen.css")

    assert stylesheet.status_code == 200
    css = stylesheet.text.lower()
    assert ".select-chevron{position:absolute;top:11px;right:9px" in css


def test_docgen2_editor_script_supports_table_menu_and_table_edits(client: TestClient) -> None:
    script = client.get("/static/js/docgen2-editor.js")

    assert script.status_code == 200
    source = script.text
    for token in (
        '[data-editor-command=\\"table\\"]',
        "tableMenu",
        "data-table-action",
        "insertTable",
        "addTableRow",
        "deleteTableRow",
        "addTableColumn",
        "deleteTableColumn",
        "closest(\"table\")",
    ):
        assert token in source


def test_docgen2_editor_toolbar_preserves_canvas_selection(client: TestClient) -> None:
    script = client.get("/static/js/docgen2-editor.js")

    assert script.status_code == 200
    source = script.text
    assert "savedSelection = range.cloneRange()" in source
    assert "selection?.addRange(savedSelection)" in source
    assert '"button[data-editor-command], button[data-editor-apply-heading]' in source
    assert "event.preventDefault()" in source
    assert "restoreSelection();" in source


def test_docgen2_editor_toolbar_can_reapply_and_clear_formatting(client: TestClient) -> None:
    script = client.get("/static/js/docgen2-editor.js")
    template = (
        Path(__file__).parents[1]
        / "src"
        / "docgen"
        / "templates"
        / "projects"
        / "work_panel.html"
    ).read_text(encoding="utf-8")

    assert script.status_code == 200
    assert "data-editor-apply-heading" in template
    assert "data-editor-clear-formatting" in template
    assert 'runCommand("formatBlock", headingSelect?.value || "p")' in script.text
    assert 'document.execCommand("removeFormat", false, null)' in script.text
    assert 'document.execCommand("formatBlock", false, "p")' in script.text
    assert "normalizeSemanticNodeAttributes();" in script.text
    assert "seenNodeIds.has(nodeId)" in script.text
    assert "element.removeAttribute(attribute)" in script.text


def test_shared_ui_script_confirms_project_delete_forms(client: TestClient) -> None:
    script = client.get("/static/js/docgen2-editor.js")

    assert script.status_code == 200
    source = script.text
    assert "form[data-confirm-delete]" in source
    assert "window.confirm(message)" in source
    assert "event.preventDefault()" in source


@_requires_node
def test_template_selector_syncs_every_workspace_form_target(client: TestClient) -> None:
    script = client.get("/static/js/docgen2-editor.js")

    assert script.status_code == 200
    harness = f"""
const listeners = new Map();
const source = {{
  value: "use-case",
  addEventListener(type, listener) {{ listeners.set(type, listener); }},
}};
const targets = [{{ value: "use-case" }}, {{ value: "use-case" }}];
const label = {{ textContent: "Собрать" }};
const buildButton = {{
  dataset: {{ sourceAvailable: "true" }},
  disabled: false,
  formTarget: "",
  setAttribute(name, value) {{ if (name === "form") this.formTarget = value; }},
  querySelector(selector) {{ return selector === "[data-build-label]" ? label : null; }},
}};
const format = {{ value: "html" }};
const formatting = {{ value: "docgen-light", disabled: false }};
const conversionFormat = {{ value: "" }};
const conversionTemplate = {{ value: "" }};
globalThis.document = {{
  querySelector(selector) {{
    if (selector === "[data-template-source]") return source;
    if (selector === "#buildButton") return buildButton;
    if (selector === "#formatSelect") return format;
    if (selector === "#export-template-select select[name='template_id']") return formatting;
    if (selector === "[data-conversion-format]") return conversionFormat;
    if (selector === "[data-conversion-template]") return conversionTemplate;
    return null;
  }},
  querySelectorAll(selector) {{
    if (selector === "[data-template-target]") return targets;
    return [];
  }},
  addEventListener() {{}},
}};
{script.text}
source.value = "faq";
listeners.get("change")();
if (targets.some((target) => target.value !== "faq")) {{
  throw new Error(`template targets were not synchronized: ${{targets.map((target) => target.value)}}`);
}}
source.value = "no-template";
listeners.get("change")();
if (buildButton.formTarget !== "conversionForm") throw new Error("conversion form was not selected");
if (label.textContent !== "Собрать") throw new Error("build button label was changed");
if (conversionFormat.value !== "html" || conversionTemplate.value !== "docgen-light") {{
  throw new Error("conversion output was not synchronized");
}}
"""

    subprocess.run([_NODE or "node", "-e", harness], check=True, capture_output=True, text=True)


@_requires_node
def test_editor_save_is_initialized_after_htmx_replaces_workspace(
    client: TestClient,
) -> None:
    script = client.get("/static/js/docgen2-editor.js")

    assert script.status_code == 200
    harness = f"""
const documentListeners = new Map();
const element = (extra = {{}}) => ({{
  dataset: {{}},
  listeners: new Map(),
  addEventListener(type, listener) {{ this.listeners.set(type, listener); }},
  querySelector() {{ return null; }},
  querySelectorAll() {{ return []; }},
  ...extra,
}});
const canvas = element({{ innerHTML: '<p data-node-id="n1">Правка</p>', focus() {{}} }});
const title = element({{ value: "После сборки" }});
const saveButton = element({{ disabled: false }});
const saveStatus = element({{ textContent: "" }});
let scheduledClear = null;
const editor = element({{
  dataset: {{ saveUrl: "/projects/p1/editor/save", revision: "1" }},
  querySelector(selector) {{
    if (selector === "#docgen2DocumentCanvas") return canvas;
    if (selector === "#docgen2EditorTitle") return title;
    if (selector === "[data-editor-save]") return saveButton;
    if (selector === "[data-editor-save-status]") return saveStatus;
    return null;
  }},
  contains() {{ return false; }},
}});
let currentEditor = null;
let posted = null;
let exportRefresh = null;
globalThis.document = {{
  body: {{}},
  querySelector(selector) {{
    if (selector === "#docgen2Editor") return currentEditor;
    return null;
  }},
  querySelectorAll() {{ return []; }},
  addEventListener(type, listener) {{ documentListeners.set(type, listener); }},
  execCommand() {{}},
}};
globalThis.window = {{
  getSelection() {{ return null; }},
  htmx: {{
    trigger(target, name, detail) {{ exportRefresh = {{ target, name, detail }}; }},
  }},
}};
globalThis.setTimeout = (callback, delay) => {{
  if (delay === 0) {{ callback(); return 0; }}
  if (delay !== 2000) throw new Error(`unexpected save-status timeout: ${{delay}}`);
  scheduledClear = callback;
  return 1;
}};
globalThis.clearTimeout = () => {{}};
globalThis.fetch = async (url, options) => {{
  posted = {{ url, payload: JSON.parse(options.body) }};
  return {{
    ok: true,
    async json() {{
      return {{ revision: 2, html: '<p data-node-id="manual-1">Правка</p>' }};
    }},
  }};
}};
{script.text}
currentEditor = editor;
documentListeners.get("htmx:afterSwap")();
await saveButton.listeners.get("click")();
await new Promise((resolve) => setTimeout(resolve, 0));
if (posted?.url !== "/projects/p1/editor/save") throw new Error("save was not posted");
if (posted.payload.revision !== 1) throw new Error("revision was not posted");
if (editor.dataset.revision !== "2") throw new Error("revision was not updated");
if (
  exportRefresh?.target !== document.body ||
  exportRefresh?.name !== "docgen:document-updated" ||
  exportRefresh?.detail?.revision !== 2
) {{
  throw new Error("export refresh was not triggered after save");
}}
if (!canvas.innerHTML.includes('data-node-id="manual-1"')) {{
  throw new Error("normalized html was not applied");
}}
if (saveStatus.textContent !== "✓") {{
  throw new Error(`unexpected save status: ${{saveStatus.textContent}}`);
}}
if (saveStatus.title !== "Сохранено в проекте") {{
  throw new Error(`unexpected save status tooltip: ${{saveStatus.title}}`);
}}
if (!scheduledClear) throw new Error("saved status clear was not scheduled");
scheduledClear();
if (saveStatus.textContent || saveStatus.title || saveStatus.ariaLabel || saveStatus.dataset.state) {{
  throw new Error("saved status was not cleared");
}}
"""

    subprocess.run(
        [_NODE or "node", "--input-type=module", "-e", harness],
        check=True,
        capture_output=True,
        text=True,
    )


@_requires_node
def test_document_update_event_refreshes_revisions_for_followup_actions(
    client: TestClient,
) -> None:
    script = client.get("/static/js/docgen2-editor.js")

    assert script.status_code == 200
    harness = f"""
const listeners = new Map();
const revisions = [{{ value: "1" }}, {{ value: "1" }}];
globalThis.document = {{
  querySelector() {{ return null; }},
  querySelectorAll(selector) {{
    if (selector === 'input[name="revision"]') return revisions;
    return [];
  }},
  addEventListener(type, listener) {{ listeners.set(type, listener); }},
}};
{script.text}
listeners.get("docgen:document-updated")({{ detail: {{ revision: 2 }} }});
if (revisions.some((input) => input.value !== "2")) {{
  throw new Error(`stale follow-up revisions: ${{revisions.map((input) => input.value)}}`);
}}
"""

    subprocess.run([_NODE or "node", "-e", harness], check=True, capture_output=True, text=True)


@_requires_node
def test_chat_submit_shows_user_message_and_pending_state(client: TestClient) -> None:
    script = client.get("/static/js/docgen2-editor.js")

    assert script.status_code == 200
    harness = f"""
const listeners = new Map();
const messages = {{
  children: [],
  appendChild(node) {{ this.children.push(node); }},
}};
const textarea = {{ value: "  Проверь формулировку  ", disabled: false }};
const button = {{ disabled: false }};
const form = {{
  dataset: {{ chatForm: "true" }},
  querySelector(selector) {{
    if (selector === "[data-chat-input]") return textarea;
    if (selector === "[data-chat-submit]") return button;
    return null;
  }},
}};
globalThis.document = {{
  createElement(tagName) {{
    return {{
      tagName,
      className: "",
      textContent: "",
      dataset: {{}},
      remove() {{ this.removed = true; }},
    }};
  }},
  querySelector(selector) {{
    if (selector === "#chat-messages") return messages;
    return null;
  }},
  querySelectorAll() {{ return []; }},
  addEventListener(type, listener) {{ listeners.set(type, listener); }},
}};
globalThis.CSS = {{ escape(value) {{ return value; }} }};
{script.text}
listeners.get("htmx:beforeRequest")({{ detail: {{ elt: form }} }});
if (messages.children.length !== 2) throw new Error(`expected user and pending messages, got ${{messages.children.length}}`);
if (messages.children[0].textContent !== "Проверь формулировку") throw new Error("user message was not appended");
if (!messages.children[1].textContent.includes("Отправляю")) throw new Error("pending message was not appended");
if (textarea.value !== "") throw new Error("chat input was not cleared");
if (!textarea.disabled || !button.disabled) throw new Error("chat controls were not disabled");
listeners.get("htmx:afterRequest")({{ detail: {{ elt: form }} }});
if (messages.children[1].removed !== true) throw new Error("pending message was not removed");
if (textarea.disabled || button.disabled) throw new Error("chat controls were not restored");
"""

    subprocess.run([_NODE or "node", "-e", harness], check=True, capture_output=True, text=True)


@_requires_node
def test_stale_editor_save_tells_user_to_refresh(client: TestClient) -> None:
    script = client.get("/static/js/docgen2-editor.js")
    assert script.status_code == 200
    harness = f"""
const element = (extra = {{}}) => ({{
  dataset: {{}},
  listeners: new Map(),
  addEventListener(type, listener) {{ this.listeners.set(type, listener); }},
  querySelector() {{ return null; }},
  querySelectorAll() {{ return []; }},
  ...extra,
}});
const canvas = element({{ innerHTML: '<p data-node-id="n1">Правка</p>', focus() {{}} }});
const title = element({{ value: "Документ" }});
const saveButton = element({{ disabled: false }});
const saveStatus = element({{ textContent: "" }});
const editor = element({{
  dataset: {{ saveUrl: "/projects/p1/editor/save", revision: "1" }},
  querySelector(selector) {{
    if (selector === "#docgen2DocumentCanvas") return canvas;
    if (selector === "#docgen2EditorTitle") return title;
    if (selector === "[data-editor-save]") return saveButton;
    if (selector === "[data-editor-save-status]") return saveStatus;
    return null;
  }},
  contains() {{ return false; }},
}});
globalThis.document = {{
  querySelector(selector) {{
    if (selector === "#docgen2Editor") return editor;
    return null;
  }},
  querySelectorAll() {{ return []; }},
  addEventListener() {{}},
  execCommand() {{}},
}};
globalThis.window = {{ getSelection() {{ return null; }} }};
globalThis.fetch = async () => ({{
  ok: false,
  status: 409,
  async json() {{ return {{ detail: "Документ уже изменён" }}; }},
}});
{script.text}
await saveButton.listeners.get("click")();
if (!saveStatus.textContent.includes("Документ уже изменён")) {{
  throw new Error(`missing conflict detail: ${{saveStatus.textContent}}`);
}}
if (!saveStatus.textContent.includes("Обнови")) {{
  throw new Error(`missing refresh action: ${{saveStatus.textContent}}`);
}}
"""

    subprocess.run(
        [_NODE or "node", "--input-type=module", "-e", harness],
        check=True,
        capture_output=True,
        text=True,
    )


def test_non_htmx_create_start_cancel_retry_flow_uses_standard_forms(
    client: TestClient,
) -> None:
    settings = client.app.state.settings
    settings.local_text_base_url = "http://localhost:11434/v1"
    settings.local_text_model = "local-text"
    settings.local_vision_base_url = "http://127.0.0.1:11435/v1"
    settings.local_vision_model = "local-vision"
    browser_headers = {"Accept": "text/html"}

    created = client.post(
        "/projects",
        data={"name": "Офлайн-проект"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    project_url = created.headers["location"]
    project_id = project_url.rsplit("/", 1)[-1]

    uploaded = client.post(
        f"{project_url}/sources/files",
        files={"file": ("case.md", b"# Case", "text/markdown")},
        headers=browser_headers,
        follow_redirects=False,
    )
    assert uploaded.status_code == 303
    assert uploaded.headers["location"] == project_url

    detail = client.get(project_url)
    assert detail.status_code == 200
    forms = BeautifulSoup(detail.text, "html.parser").find_all("form")
    assert forms
    assert all(form.get("action") and form.get("method") for form in forms)

    started = client.post(
        f"{project_url}/jobs/assemble",
        data={"template_id": "use-case"},
        headers=browser_headers,
        follow_redirects=False,
    )
    assert started.status_code == 303
    job_url = started.headers["location"]
    assert job_url.startswith(f"{project_url}/jobs/")

    job_page = client.get(job_url, headers=browser_headers)
    assert job_page.status_code == 200
    assert "<!doctype html>" in job_page.text.lower()
    cancel_form = BeautifulSoup(job_page.text, "html.parser").find(
        "form", attrs={"data-action": "cancel-job"}
    )
    assert cancel_form is not None
    assert cancel_form.get("action") == f"{job_url}/cancel"
    assert cancel_form.get("method") == "post"

    cancelled = client.post(
        f"{job_url}/cancel",
        headers=browser_headers,
        follow_redirects=False,
    )
    assert cancelled.status_code == 303
    assert cancelled.headers["location"] == job_url

    retry_page = client.get(job_url, headers=browser_headers)
    retry_form = BeautifulSoup(retry_page.text, "html.parser").find(
        "form", attrs={"data-action": "retry-job"}
    )
    assert retry_form is not None
    retried = client.post(
        retry_form["action"],
        data={"template_id": "use-case"},
        headers=browser_headers,
        follow_redirects=False,
    )
    assert retried.status_code == 303
    assert retried.headers["location"].startswith(f"{project_url}/jobs/")

    with client.app.state.session_factory() as session:
        assert len(session.scalars(select(Job).where(Job.project_id == project_id)).all()) == 2


def test_non_htmx_source_and_project_delete_fallbacks(client: TestClient) -> None:
    browser_headers = {"Accept": "text/html"}
    created = client.post(
        "/projects", data={"name": "Удаление"}, follow_redirects=False
    )
    project_url = created.headers["location"]
    project_id = project_url.rsplit("/", 1)[-1]
    client.post(
        f"{project_url}/sources/files",
        files={"file": ("case.md", b"# Case", "text/markdown")},
        headers=browser_headers,
        follow_redirects=False,
    )
    with client.app.state.session_factory() as session:
        source = SourceRepository(session).list_for_project(project_id)[0]

    deleted_source = client.post(
        f"{project_url}/sources/{source.id}/delete",
        headers=browser_headers,
        follow_redirects=False,
    )
    assert deleted_source.status_code == 303
    assert deleted_source.headers["location"] == project_url

    deleted_project = client.post(
        f"{project_url}/delete",
        headers=browser_headers,
        follow_redirects=False,
    )
    assert deleted_project.status_code == 303
    assert deleted_project.headers["location"] == "/projects"

    assert client.get(project_url).status_code == 404
