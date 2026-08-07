from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from docgen.projects.models import Project
from docgen.projects.repository import ProjectRepository


@pytest.fixture
def existing_project(client: TestClient) -> Project:
    session = client.app.state.session_factory()
    try:
        project = ProjectRepository(session).create("Существующий проект")
        session.commit()
        return project
    finally:
        session.close()


def test_projects_page_lists_projects_and_create_button(
    client: TestClient, existing_project: Project
) -> None:
    response = client.get("/projects")

    assert response.status_code == 200
    assert existing_project.name in response.text
    assert "Создать проект" in response.text


def test_create_project_redirects_to_detail(client: TestClient) -> None:
    response = client.post("/projects", data={"name": "Новый Use Case"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/projects/")

    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "Новый Use Case" in detail.text


def test_missing_project_returns_404(client: TestClient) -> None:
    response = client.get("/projects/missing")

    assert response.status_code == 404
    assert "Проект не найден" in response.text


def test_autosave_renames_project_and_returns_name_form(
    client: TestClient, existing_project: Project
) -> None:
    response = client.patch(
        f"/projects/{existing_project.id}",
        data={"name": "Переименованный проект"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "Переименованный проект" in response.text
    form = _single_form(response.text)
    assert form["hx-patch"].startswith("/projects/")
    assert form["hx-swap"] == "outerHTML"

    session = client.app.state.session_factory()
    try:
        project = ProjectRepository(session).get(existing_project.id)
        assert project is not None
        assert project.name == "Переименованный проект"
    finally:
        session.close()


def test_blank_autosave_returns_inline_error(
    client: TestClient, existing_project: Project
) -> None:
    response = client.patch(
        f"/projects/{existing_project.id}",
        data={"name": "   "},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert "Название проекта обязательно" in response.text
    assert 'value="   "' in response.text
    form = _single_form(response.text)
    assert form["hx-swap"] == "outerHTML"
    assert "422" in form["hx-on::before-swap"]
    assert "shouldSwap = true" in form["hx-on::before-swap"]


def test_delete_project_redirects_to_listing(
    client: TestClient, existing_project: Project
) -> None:
    response = client.delete(f"/projects/{existing_project.id}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/projects"
    assert "Существующий проект" not in client.get("/projects").text


def test_htmx_delete_redirects_the_browser_to_listing(
    client: TestClient, existing_project: Project
) -> None:
    response = client.delete(
        f"/projects/{existing_project.id}",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.headers["hx-redirect"] == "/projects"


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form":
            self.forms.append(dict(attrs))


def _single_form(markup: str) -> dict[str, str | None]:
    parser = _FormParser()
    parser.feed(markup)
    assert len(parser.forms) == 1
    return parser.forms[0]
