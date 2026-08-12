from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select

from docgen.jobs.models import Job
from docgen.sources.repository import SourceRepository


def test_ui_uses_local_assets_and_restrictive_csp(client: TestClient) -> None:
    response = client.get("/projects")

    assert response.status_code == 200
    assert 'src="/static/vendor/htmx-2.0.8.min.js"' in response.text
    assert 'src="/static/js/docgen2-editor.js"' in response.text
    assert 'href="/static/css/docgen.css"' in response.text
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
        "grid-template-columns:clamp(300px,17vw,360px) minmax(620px,1fr)",
        "grid-template-rows:minmax(0,2fr) minmax(0,1fr)",
        ".sources-panel",
        ".editor-card",
        ".editor-topline",
        ".document-canvas",
        ".tool-button",
        ".table-menu",
        ".table-size-grid",
        ".chat-panel",
        ".result-panel",
        ".project-card-link{min-height:76px;display:grid;grid-template-columns:44px minmax(0,1fr);align-items:center",
        ".project-delete-button{position:absolute;top:10px;right:10px",
        "#60bcff",
        "#3196df",
        "#f9f9fc",
        "#f3f6fa",
        "@media (max-width:1023px)",
    ):
        assert token in css


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


def test_shared_ui_script_confirms_project_delete_forms(client: TestClient) -> None:
    script = client.get("/static/js/docgen2-editor.js")

    assert script.status_code == 200
    source = script.text
    assert "form[data-confirm-delete]" in source
    assert "window.confirm(message)" in source
    assert "event.preventDefault()" in source


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
globalThis.document = {{
  querySelector(selector) {{
    if (selector === "[data-template-source]") return source;
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
"""

    subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)


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
globalThis.document = {{
  querySelector(selector) {{
    if (selector === "#docgen2Editor") return currentEditor;
    return null;
  }},
  querySelectorAll() {{ return []; }},
  addEventListener(type, listener) {{ documentListeners.set(type, listener); }},
  execCommand() {{}},
}};
globalThis.window = {{ getSelection() {{ return null; }} }};
globalThis.fetch = async (url, options) => {{
  posted = {{ url, payload: JSON.parse(options.body) }};
  return {{ ok: true, async json() {{ return {{ revision: 2 }}; }} }};
}};
{script.text}
currentEditor = editor;
documentListeners.get("htmx:afterSwap")();
await saveButton.listeners.get("click")();
await new Promise((resolve) => setTimeout(resolve, 0));
if (posted?.url !== "/projects/p1/editor/save") throw new Error("save was not posted");
if (posted.payload.revision !== 1) throw new Error("revision was not posted");
if (editor.dataset.revision !== "2") throw new Error("revision was not updated");
"""

    subprocess.run(
        ["node", "--input-type=module", "-e", harness],
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
