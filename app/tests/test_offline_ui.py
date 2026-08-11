from __future__ import annotations

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
    stylesheet = client.get("/static/css/docgen.css")
    assert stylesheet.status_code == 200
    assert ".htmx-indicator{opacity:0}" in stylesheet.text
    assert ".htmx-request .htmx-indicator" in stylesheet.text


def test_htmx_error_fragments_are_swapped_into_target() -> None:
    base_template = Path(__file__).parents[1] / "src" / "docgen" / "templates" / "base.html"

    markup = base_template.read_text(encoding="utf-8")

    assert '"code":"[2345]..","swap":true' in markup
    assert '"code":"[45]..","swap":false' not in markup


def test_templates_have_no_inline_handlers_styles_or_public_executable_assets() -> None:
    template_root = Path(__file__).parents[1] / "src" / "docgen" / "templates"

    for path in template_root.rglob("*.html"):
        markup = path.read_text(encoding="utf-8")
        assert "hx-on" not in markup, path
        assert " style=" not in markup, path
        assert "https://cdn.tailwindcss.com" not in markup, path
        assert "unpkg.com" not in markup, path


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
