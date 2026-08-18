import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

_NODE = shutil.which("node")


def test_csp_keeps_inline_code_disabled(client: TestClient) -> None:
    response = client.get("/projects")

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "style-src 'self'" in csp
    assert "unsafe-inline" not in csp


@pytest.mark.skipif(_NODE is None, reason="Node.js is required for editor script tests")
def test_editor_reapplies_sanitized_styles_under_strict_csp(client: TestClient) -> None:
    script = client.get("/static/js/docgen2-editor.js")
    assert script.status_code == 200
    harness = f"""
const values = {{}};
const formatted = {{
  getAttribute(name) {{
    return name === "style" ? "color:purple;font-style:italic" : null;
  }},
  removeAttribute() {{}},
  style: {{ setProperty(name, value) {{ values[name] = value; }} }},
}};
const element = (extra = {{}}) => ({{
  dataset: {{}},
  addEventListener() {{}},
  querySelector() {{ return null; }},
  querySelectorAll() {{ return []; }},
  ...extra,
}});
const canvas = element({{
  focus() {{}},
  querySelectorAll(selector) {{ return selector === "[style]" ? [formatted] : []; }},
}});
const editor = element({{
  querySelector(selector) {{
    return selector === "#docgen2DocumentCanvas" ? canvas : null;
  }},
}});
globalThis.document = {{
  body: {{}},
  querySelector(selector) {{ return selector === "#docgen2Editor" ? editor : null; }},
  querySelectorAll() {{ return []; }},
  addEventListener() {{}},
}};
globalThis.window = {{}};
{script.text}
if (values.color !== "purple" || values["font-style"] !== "italic") {{
  throw new Error(`styles were not restored: ${{JSON.stringify(values)}}`);
}}
"""

    subprocess.run([_NODE or "node", "-e", harness], check=True, capture_output=True, text=True)
