from html.parser import HTMLParser

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.jobs.models import JobKind
from docgen.jobs.repository import JobRepository
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
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.find(class_="app-shell") is not None
    topbar = soup.find(class_="topbar")
    assert topbar is not None
    assert topbar.find(class_="brand-mark").get_text(strip=True) == "F"
    hero = topbar.find(class_="projects-hero")
    assert hero is not None
    assert hero.find("h1").get_text(strip=True) == "Корпоративный кабинет подготовки документов"
    assert "Проекты" not in hero.get_text(" ")
    projects_panel = soup.find(class_="projects-panel")
    assert projects_panel is not None
    assert "создайте новый документ" not in projects_panel.get_text(" ").lower()
    assert projects_panel.find("form", attrs={"action": "/projects"}) is not None
    project_card = projects_panel.find(class_="project-card")
    assert project_card is not None
    project_link = project_card.find("a", class_="project-card-link")
    assert project_link is not None
    assert project_link.get("href") == f"/projects/{existing_project.id}"
    assert "Открыть" not in project_card.get_text(" ")
    delete_form = project_card.find("form", attrs={"data-confirm-delete": ""})
    assert delete_form is not None
    assert delete_form.get("action") == f"/projects/{existing_project.id}/delete"
    assert delete_form.get("method") == "post"
    delete_button = delete_form.find("button")
    assert delete_button is not None
    assert delete_button.get_text(strip=True) == ""
    assert delete_button.get("aria-label") == f"Удалить проект {existing_project.name}"
    assert delete_button.find("svg", attrs={"aria-hidden": "true"}) is not None


def test_create_project_redirects_to_detail(client: TestClient) -> None:
    response = client.post("/projects", data={"name": "Новый Use Case"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/projects/")

    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "Formatta" in detail.text
    assert "Рабочее пространство" not in detail.text
    soup = BeautifulSoup(detail.text, "html.parser")
    projects_link = soup.find("a", attrs={"data-action": "back-to-projects"})
    assert projects_link is not None
    assert projects_link.get("href") == "/projects"
    assert projects_link.get_text(strip=True) == "← К выбору проекта"
    assert soup.find(class_="topbar").find("a", attrs={"data-action": "back-to-projects"}) is None
    assert soup.find(id="sourcesPanel").find("a", attrs={"data-action": "back-to-projects"}) is not None
    assert soup.find(attrs={"data-role": "current-project-name"}) is None
    title_input = soup.find("input", id="docgen2EditorTitle")
    assert title_input is not None
    assert title_input.get("value") == "Новый Use Case"


def _project_with_markdown_source(client: TestClient) -> str:
    created = client.post("/projects", data={"name": "Проверка источника"}, follow_redirects=False)
    project_url = created.headers["location"]
    response = client.post(
        f"{project_url}/sources/files",
        files={"file": ("case.md", b"# Case", "text/markdown")},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    return project_url


def test_workspace_check_is_available_for_one_uploaded_document(client: TestClient) -> None:
    project_url = _project_with_markdown_source(client)
    page = BeautifulSoup(client.get(project_url).text, "html.parser")
    review = page.find("button", id="reviewButton")
    check_form = page.find("form", id="checkForm")
    template_select = page.find("select", id="templateSelect")

    assert review is not None
    assert review.has_attr("disabled") is False
    assert check_form is not None
    assert template_select is not None
    selected_template = template_select.find("option", selected=True)
    assert selected_template is not None
    assert selected_template["value"] == "use-case"
    assert check_form.find("input", attrs={"name": "template_id"})["value"] == selected_template["value"]


def test_workspace_forms_share_selected_template_contract(client: TestClient) -> None:
    project_url = _project_with_markdown_source(client)
    page = BeautifulSoup(client.get(project_url).text, "html.parser")

    template_select = page.find("select", id="templateSelect")
    assert template_select is not None
    assert template_select["data-template-source"] == ""
    selected_template = template_select.find("option", selected=True)
    assert selected_template is not None
    for form_id in ("assembleForm", "checkForm"):
        field = page.find("form", id=form_id).find("input", attrs={"name": "template_id"})
        assert field["data-template-target"] == ""
        assert field["value"] == selected_template["value"]


def test_workspace_source_update_swaps_review_button_state(client: TestClient) -> None:
    created = client.post("/projects", data={"name": "Обновление источников"}, follow_redirects=False)
    project_url = created.headers["location"]

    response = client.post(
        f"{project_url}/sources/files",
        files={"file": ("case.md", b"# Case", "text/markdown")},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    fragment = BeautifulSoup(response.text, "html.parser")
    review = fragment.find("button", id="reviewButton")
    assert review is not None
    assert review["hx-swap-oob"] == "outerHTML"
    assert review.has_attr("disabled") is False

    target = fragment.find("select", id="checkTargetSelect").find("option")
    response = client.delete(
        f"{project_url}/sources/{target['value']}", headers={"HX-Request": "true"}
    )

    assert response.status_code == 200
    fragment = BeautifulSoup(response.text, "html.parser")
    review = fragment.find("button", id="reviewButton")
    assert review is not None
    assert review["hx-swap-oob"] == "outerHTML"
    assert review.has_attr("disabled") is True


def test_project_detail_renders_layout_agent_workspace_without_document(
    client: TestClient,
) -> None:
    created = client.post("/projects", data={"name": "Интерфейс"}, follow_redirects=False)
    project_url = created.headers["location"]

    response = client.get(project_url)

    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.find(class_="app-shell") is not None
    assert soup.find(class_="topbar") is not None
    assert soup.find(class_="mobile-tabs") is not None
    assert soup.find(id="sourcesPanel") is not None
    assert soup.find(id="previewPanel") is None
    editor_panel = soup.find(id="docgen2Editor")
    assert editor_panel is not None
    assert "editor-card" in editor_panel.get("class", [])
    assert editor_panel.find(class_="editor-topline") is not None
    assert editor_panel.find(class_="toolbar", attrs={"role": "toolbar"}) is not None
    assert editor_panel.find(class_="editor-scroll") is not None
    document_canvas = editor_panel.find(class_="document-canvas")
    assert document_canvas is not None
    assert document_canvas.get("contenteditable") == "true"
    assert soup.find(id="chatPanel") is not None
    assert soup.find(id="resultPanel") is not None
    assert soup.find(id="editor-shell") is None
    assert "Formatta" in soup.get_text(" ")
    assert "Рабочее пространство" not in soup.get_text(" ")
    projects_link = soup.find("a", attrs={"data-action": "back-to-projects"})
    assert projects_link is not None
    assert projects_link.get("href") == "/projects"
    assert projects_link.get_text(strip=True) == "← К выбору проекта"
    assert soup.find(class_="topbar").find("a", attrs={"data-action": "back-to-projects"}) is None
    sources_panel = soup.find(id="sourcesPanel")
    assert sources_panel.find("a", attrs={"data-action": "back-to-projects"}) is not None
    assert sources_panel.find("a", attrs={"data-action": "back-to-projects"}).find_next("h1").get_text(strip=True) == "Источники"
    project_name = soup.find(attrs={"data-role": "current-project-name"})
    assert project_name is None
    title_input = editor_panel.find("input", id="docgen2EditorTitle")
    assert title_input is not None
    assert title_input.get("value") == "Интерфейс"
    assert "Шаг 2" not in editor_panel.get_text(" ")
    assert "Документ готов к редактированию" not in editor_panel.get_text(" ")
    assert "Загрузите Markdown, DOCX или PDF либо начните редактирование здесь." in editor_panel.get_text(" ")
    template_select = soup.find(id="templateSelect")
    assert template_select is not None
    assert not template_select.has_attr("disabled")
    assert soup.find(id="formatSelect") is not None
    assert soup.find(id="buildButton") is not None
    assert soup.find(id="chat-panel") is None


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
                nodes=[
                    DocumentNode(id="intro", kind=NodeKind.PARAGRAPH, text="Введение")
                ],
            ),
        )
        session.commit()

    response = client.get(project_url)

    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    editor_panel = soup.find(id="docgen2Editor")
    assert editor_panel is not None
    title_input = editor_panel.find("input", id="docgen2EditorTitle")
    assert title_input is not None
    assert title_input.get("value") == "Редактор"
    assert "Техническое задание" in editor_panel.get_text(" ")
    assert "Введение" in editor_panel.get_text(" ")
    assert editor_panel.find("button", attrs={"aria-label": "Полужирный"}) is not None
    toolbar = editor_panel.find(class_="toolbar", attrs={"role": "toolbar"})
    assert toolbar is not None
    for label in (
        "Отменить",
        "Повторить",
        "Полужирный",
        "Курсив",
        "Подчёркивание",
        "Маркированный список",
        "Нумерованный список",
        "По левому краю",
        "По центру",
        "По правому краю",
        "Добавить ссылку",
        "Добавить изображение",
        "Добавить таблицу",
    ):
        button = toolbar.find("button", attrs={"aria-label": label})
        assert button is not None
        icon = button.find("svg", attrs={"aria-hidden": "true"})
        assert icon is not None
        assert icon.get("width") == "18"
        assert icon.get("height") == "18"
        assert icon.get("stroke") == "currentColor"
        assert icon.get("stroke-width") == "2"
    for old_text_icon in ("↶", "↷", "B", "I", "U", "•", "1.", "≣", "🔗", "▧", "▦"):
        assert old_text_icon not in toolbar.get_text(" ")
    table_button = editor_panel.find("button", attrs={"aria-label": "Добавить таблицу"})
    assert table_button is not None
    assert table_button.get("aria-expanded") == "false"
    save_button = toolbar.find("button", attrs={"data-editor-save": ""})
    assert save_button is not None
    assert save_button.get_text(strip=True) == "Сохранить"
    table_menu = editor_panel.find(id="tableMenu")
    assert table_menu is not None
    assert table_menu.get("hidden") == ""
    assert table_menu.find("select", attrs={"name": "rows"}) is not None
    assert table_menu.find("select", attrs={"name": "columns"}) is not None
    assert table_menu.find("button", attrs={"data-table-action": "insert"}) is not None
    assert table_menu.find("button", attrs={"data-table-action": "add-row"}) is not None
    assert table_menu.find("button", attrs={"data-table-action": "delete-row"}) is not None
    assert table_menu.find("button", attrs={"data-table-action": "add-column"}) is not None
    assert table_menu.find("button", attrs={"data-table-action": "delete-column"}) is not None
    assert soup.find(id="editor-shell") is None
    assert soup.find(id="chatPanel") is not None


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
    assert "hx-on::before-swap" not in form
    page = client.get(f"/projects/{existing_project.id}")
    assert '"code":"[2345].."' in page.text


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


def test_delete_project_returns_409_while_job_is_active(
    client: TestClient, existing_project: Project
) -> None:
    with client.app.state.session_factory() as session:
        JobRepository(session).enqueue(
            existing_project.id,
            JobKind.ASSEMBLE,
            "use-case",
        )

    response = client.delete(
        f"/projects/{existing_project.id}",
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "Проект обрабатывается" in response.text


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
