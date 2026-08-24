from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from docgen.config import Settings
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, DocumentOrigin, NodeKind, WorkingDocument
from docgen.extraction.schemas import Provenance
from docgen.jobs.models import Job
from docgen.main import create_app
from docgen.models import Project
from docgen.sources.repository import SourceRepository


@pytest.fixture
def client() -> Iterator[TestClient]:
    root = Path("var/editor-route-tests") / uuid4().hex
    settings = Settings(
        database_url=f"sqlite:///{root / 'test.db'}",
        data_dir=root / "data",
        confluence_hosts=("wiki.example.test",),
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def project_with_document(client: TestClient) -> Project:
    with _session(client) as session:
        project = Project(name="Проект с документом")
        session.add(project)
        session.flush()
        document = WorkingDocument(
            title="Рабочий документ",
            template_id="use-case",
            nodes=[
                DocumentNode(
                    id="n1",
                    kind=NodeKind.HEADING,
                    section_id="summary",
                    text="Заголовок",
                    provenance=[
                        Provenance(
                            source_id="source-1",
                            locator="heading:1",
                            quote="Заголовок",
                        )
                    ],
                    flags=["reviewed"],
                ),
                DocumentNode(id="p1", kind=NodeKind.PARAGRAPH, text="Абзац"),
                DocumentNode(id="list-1", kind=NodeKind.LIST, data={"items": ["Пункт"]}),
                DocumentNode(id="table-1", kind=NodeKind.TABLE, data={"rows": [["A", "B"]]}),
                DocumentNode(id="image-1", kind=NodeKind.IMAGE, text="Схема"),
                DocumentNode(id="gap-1", kind=NodeKind.GAP),
            ],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        return project


def test_project_detail_renders_all_node_kinds(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.get(f"/projects/{project_with_document.id}")

    assert response.status_code == 200
    for marker in [
        'data-kind="heading"',
        'data-kind="paragraph"',
        'data-kind="list"',
        'data-kind="table"',
        'data-kind="image"',
        'data-kind="gap"',
    ]:
        assert marker in response.text


def test_text_autosave_returns_new_revision(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.patch(
        f"/projects/{project_with_document.id}/editor/nodes/n1/text",
        data={"text": "Исправленный текст", "revision": "1"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'data-revision="2"' in response.text


def test_text_autosave_conflict_returns_reload_prompt(
    client: TestClient, project_with_document: Project
) -> None:
    first = client.patch(
        f"/projects/{project_with_document.id}/editor/nodes/n1/text",
        data={"text": "Первая правка", "revision": "1"},
        headers={"HX-Request": "true"},
    )
    assert first.status_code == 200

    response = client.patch(
        f"/projects/{project_with_document.id}/editor/nodes/n1/text",
        data={"text": "Устаревшая правка", "revision": "1"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 409
    assert "Документ уже изменён" in response.text


def test_insert_paragraph_after_selected_node(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/editor/nodes",
        data={"kind": "paragraph", "after_node_id": "n1", "revision": "1"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "Новый абзац" in response.text


def test_move_node_down_persists_order(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/editor/nodes/n1/move",
        data={"direction": "down", "revision": "1"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    with _session(client) as session:
        document = DocumentRepository(session).get_document(project_with_document.id)
        assert document is not None
        assert document.nodes[1].id == "n1"


def test_delete_node_removes_it_from_document(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.request(
        "DELETE",
        f"/projects/{project_with_document.id}/editor/nodes/p1",
        data={"revision": "1"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    with _session(client) as session:
        document = DocumentRepository(session).get_document(project_with_document.id)
        assert document is not None
        assert [node.id for node in document.nodes] == [
            "n1",
            "list-1",
            "table-1",
            "image-1",
            "gap-1",
        ]


def test_workspace_save_preserves_semantic_metadata(
    client: TestClient, project_with_document: Project
) -> None:
    original = _stored_document(client, project_with_document.id)
    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Исправленный FAQ",
            "html": '<h2 data-node-id="n1">Новый заголовок</h2>',
            "revision": 1,
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 2
    saved = _stored_document(client, project_with_document.id)
    assert saved.title == "Исправленный FAQ"
    assert saved.template_id == original.template_id
    assert saved.nodes[0].id == "n1"
    assert saved.nodes[0].text == "Новый заголовок"
    assert saved.nodes[0].section_id == original.nodes[0].section_id
    assert saved.nodes[0].provenance == original.nodes[0].provenance
    assert saved.nodes[0].flags == original.nodes[0].flags


def test_workspace_save_creates_first_document_for_empty_project(
    client: TestClient,
) -> None:
    with _session(client) as session:
        project = Project(name="Пустой проект")
        session.add(project)
        session.commit()
        project_id = project.id

    page = BeautifulSoup(client.get(f"/projects/{project_id}").text, "html.parser")
    assert page.find("button", string="Создать документ") is None
    assert page.find(id="docgen2DocumentCanvas").get("contenteditable") == "true"
    assert page.find("button", attrs={"data-editor-save": True}) is not None

    response = client.post(
        f"/projects/{project_id}/editor/save",
        json={
            "title": "Первый документ",
            "html": "<h1>Заголовок</h1><p>Текст документа</p>",
            "revision": None,
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 1
    saved = _stored_document(client, project_id)
    assert saved.title == "Первый документ"
    assert saved.template_id == "no-template"
    assert [node.kind for node in saved.nodes] == [
        NodeKind.HEADING,
        NodeKind.PARAGRAPH,
    ]
    assert all(node.flags == ["manual-edit"] for node in saved.nodes)


def test_single_source_can_be_imported_into_editor_without_job(
    client: TestClient,
) -> None:
    with _session(client) as session:
        project = Project(name="Проект для редактирования")
        session.add(project)
        session.commit()
        project_id = project.id

    upload = client.post(
        f"/projects/{project_id}/sources/files",
        files={
            "file": (
                "case.md",
                "# Сценарий\n\nТекст для правки".encode(),
                "text/markdown",
            )
        },
        headers={"HX-Request": "true"},
    )
    assert upload.status_code == 200
    page = BeautifulSoup(client.get(f"/projects/{project_id}").text, "html.parser")
    assert page.find("button", string="Создать документ") is None
    assert page.find("button", string="Редактировать") is None

    conversion = client.post(
        f"/projects/{project_id}/convert",
        data={"output_format": "html", "formatting_template_id": "docgen-light"},
        headers={"HX-Request": "true", "Accept": "text/html"},
    )
    assert conversion.status_code == 200
    conversion_result = BeautifulSoup(conversion.text, "html.parser")
    actions = conversion_result.find(id="conversion-result-actions")
    assert actions is not None
    assert actions.find("a", string="Открыть") is not None
    action = actions.find("form", attrs={"data-editor-start-action": True})
    assert action is not None
    assert action.get("action") == f"/projects/{project_id}/editor/import-source"
    assert action.get_text(" ", strip=True) == "Редактировать"
    stylesheet = client.get("/static/css/docgen.css")
    assert stylesheet.status_code == 200
    assert ".export-result.conversion-result-buttons{flex-flow:row wrap" in stylesheet.text

    reloaded = BeautifulSoup(client.get(f"/projects/{project_id}").text, "html.parser")
    assert reloaded.find(id="docgen2Editor").find("button", string="Редактировать") is None
    assert reloaded.find(id="resultPanel").find("button", string="Редактировать") is not None

    response = client.post(
        f"/projects/{project_id}/editor/import-source",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project_id}#docgen2Editor"
    with _session(client) as session:
        stored = DocumentRepository(session).get_document_with_revision(project_id)
        sources = SourceRepository(session).list_for_project(project_id)
        assert stored is not None
        document, revision = stored
        assert revision == 1
        assert document.origin is DocumentOrigin.IMPORTED
        assert document.source_id == sources[0].id
        assert document.template_id == "no-template"
        assert document.build_template_id is None
        assert [node.text for node in document.nodes] == [
            "Сценарий",
            "Текст для правки",
        ]
        assert list(session.scalars(select(Job).where(Job.project_id == project_id))) == []

    refreshed = BeautifulSoup(client.get(f"/projects/{project_id}").text, "html.parser")
    canvas = refreshed.find(id="docgen2DocumentCanvas")
    assert canvas is not None
    assert "Сценарий" in canvas.get_text(" ", strip=True)
    assert "Текст для правки" in canvas.get_text(" ", strip=True)
    assert refreshed.find("button", string="Редактировать") is None
    result = refreshed.find(id="resultPanel")
    selected_format = refreshed.find(id="formatSelect").find("option", selected=True)
    assert result is not None
    assert result.find(id="export-result") is not None
    assert result.find(id="conversion-result-actions") is None
    assert selected_format["value"] == "html"


def test_import_source_does_not_replace_existing_editor_document(
    client: TestClient,
    project_with_document: Project,
) -> None:
    upload = client.post(
        f"/projects/{project_with_document.id}/sources/files",
        files={"file": ("case.md", b"# Replacement", "text/markdown")},
        headers={"HX-Request": "true"},
    )
    assert upload.status_code == 200
    original = _stored_document(client, project_with_document.id)

    response = client.post(
        f"/projects/{project_with_document.id}/editor/import-source",
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "Импорт не заменяет существующие правки" in response.text
    with _session(client) as session:
        stored = DocumentRepository(session).get_document_with_revision(
            project_with_document.id
        )
        assert stored == (original, 1)


@pytest.mark.parametrize("source_count", [0, 2])
def test_import_source_requires_exactly_one_source(
    client: TestClient,
    source_count: int,
) -> None:
    with _session(client) as session:
        project = Project(name="Пустой проект")
        session.add(project)
        session.commit()
        project_id = project.id

    for index in range(source_count):
        upload = client.post(
            f"/projects/{project_id}/sources/files",
            files={
                "file": (
                    f"source-{index}.md",
                    f"# Source {index}".encode(),
                    "text/markdown",
                )
            },
            headers={"HX-Request": "true"},
        )
        assert upload.status_code == 200

    response = client.post(
        f"/projects/{project_id}/editor/import-source",
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert "Для переноса в редактор нужен ровно один источник" in response.text
    with _session(client) as session:
        assert DocumentRepository(session).get_document(project_id) is None
        assert list(session.scalars(select(Job).where(Job.project_id == project_id))) == []


def test_workspace_save_updates_order_lists_tables_and_new_manual_nodes(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Новый порядок",
            "html": (
                '<table data-node-id="table-1"><tbody><tr><th>Ключ</th><th>Значение</th></tr>'
                "<tr><td>A</td><td>1</td></tr></tbody></table>"
                '<ul data-node-id="list-1"><li>Первый</li><li>Второй</li></ul>'
                "<p>Добавленный вручную абзац</p>"
            ),
            "revision": 1,
        },
    )

    assert response.status_code == 200
    saved = _stored_document(client, project_with_document.id)
    assert [node.kind for node in saved.nodes] == [
        NodeKind.TABLE,
        NodeKind.LIST,
        NodeKind.PARAGRAPH,
    ]
    assert [node.id for node in saved.nodes[:2]] == ["table-1", "list-1"]
    assert saved.nodes[0].data["headers"] == ["Ключ", "Значение"]
    assert saved.nodes[0].data["rows"] == [["A", "1"]]
    assert saved.nodes[1].data["items"] == ["Первый", "Второй"]
    assert saved.nodes[2].text == "Добавленный вручную абзац"
    assert saved.nodes[2].flags == ["manual-edit"]
    assert saved.nodes[2].provenance == []


def test_workspace_table_headers_and_rows_round_trip_losslessly(
    client: TestClient, project_with_document: Project
) -> None:
    with _session(client) as session:
        document = DocumentRepository(session).get_document(project_with_document.id)
        assert document is not None
        nodes = [
            node.model_copy(
                update={
                    "data": {
                        "headers": ["Поле", "Описание"],
                        "rows": [["Лимит", "10 000"]],
                    }
                }
            )
            if node.id == "table-1"
            else node
            for node in document.nodes
        ]
        DocumentRepository(session).save_document(
            project_with_document.id,
            document.model_copy(update={"nodes": nodes}),
        )
        session.commit()

    page = BeautifulSoup(
        client.get(f"/projects/{project_with_document.id}").text,
        "html.parser",
    )
    table = page.find("table", attrs={"data-node-id": "table-1"})
    assert table is not None
    assert [cell.get_text(strip=True) for cell in table.select("thead th")] == [
        "Поле",
        "Описание",
    ]
    assert [[cell.get_text(strip=True) for cell in row.select("td")] for row in table.select("tbody tr")] == [
        ["Лимит", "10 000"]
    ]

    table.select_one("thead th").string = "Параметр"
    table.select_one("tbody td").string = "Сумма"
    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Рабочий документ",
            "html": str(table),
            "revision": 2,
        },
    )

    assert response.status_code == 200
    saved = _stored_document(client, project_with_document.id).nodes[0]
    assert saved.data == {
        "headers": ["Параметр", "Описание"],
        "rows": [["Сумма", "10 000"]],
    }


def test_workspace_structural_conversion_removes_stale_table_data(
    client: TestClient, project_with_document: Project
) -> None:
    with _session(client) as session:
        document = DocumentRepository(session).get_document(project_with_document.id)
        assert document is not None
        nodes = [
            node.model_copy(
                update={
                    "data": {
                        "headers": ["Поле"],
                        "rows": [["Значение"]],
                        "table_style": "compact",
                    }
                }
            )
            if node.id == "table-1"
            else node
            for node in document.nodes
        ]
        DocumentRepository(session).save_document(
            project_with_document.id,
            document.model_copy(update={"nodes": nodes}),
        )
        session.commit()

    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Рабочий документ",
            "html": '<p data-node-id="table-1">Таблица стала абзацем</p>',
            "revision": 2,
        },
    )

    assert response.status_code == 200
    saved = _stored_document(client, project_with_document.id).nodes[0]
    assert saved.kind is NodeKind.PARAGRAPH
    assert saved.text == "Таблица стала абзацем"
    assert saved.data == {}


def test_repeated_workspace_save_preserves_server_assigned_manual_node_id(
    client: TestClient, project_with_document: Project
) -> None:
    first = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Ручная правка",
            "html": "<p>Новый блок</p>",
            "revision": 1,
        },
    )

    assert first.status_code == 200
    normalized_html = first.json()["html"]
    first_id = _stored_document(client, project_with_document.id).nodes[0].id
    assert f'data-node-id="{first_id}"' in normalized_html

    second = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Ручная правка",
            "html": normalized_html,
            "revision": first.json()["revision"],
        },
    )

    assert second.status_code == 200
    assert _stored_document(client, project_with_document.id).nodes[0].id == first_id


@pytest.mark.parametrize(
    "html",
    [
        '<h2 data-node-id="missing">Чужой узел</h2>',
        '<h2 data-node-id="n1">Первый</h2><h2 data-node-id="n1">Повтор</h2>',
    ],
)
def test_workspace_save_rejects_invalid_claimed_node_ids(
    client: TestClient, project_with_document: Project, html: str
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={"title": "Не сохранять", "html": html, "revision": 1},
    )

    assert response.status_code == 422
    assert _stored_document(client, project_with_document.id).title == "Рабочий документ"


def test_stale_workspace_save_returns_conflict(
    client: TestClient, project_with_document: Project
) -> None:
    first = _save_workspace(client, project_with_document.id, revision=1)
    second = _save_workspace(client, project_with_document.id, revision=1)

    assert first.status_code == 200
    assert second.status_code == 409
    assert _stored_document(client, project_with_document.id).title == "Версия 1"


def test_stale_workspace_save_returns_conflict_before_node_validation(
    client: TestClient, project_with_document: Project
) -> None:
    first = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Узел n1 удалён",
            "html": '<p data-node-id="p1">Сохранённый абзац</p>',
            "revision": 1,
        },
    )
    stale = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Устаревшая правка",
            "html": '<h2 data-node-id="n1">Устаревший узел</h2>',
            "revision": 1,
        },
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert _stored_document(client, project_with_document.id).title == "Узел n1 удалён"


def test_project_detail_restores_saved_docgen2_workspace_html(
    client: TestClient, project_with_document: Project
) -> None:
    saved = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Restored project",
            "html": '<h1 data-node-id="n1">Restored</h1><p><em>Editable</em> content</p>',
            "revision": 1,
        },
    )
    assert saved.status_code == 200

    response = client.get(f"/projects/{project_with_document.id}")

    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    title = soup.find(id="docgen2EditorTitle")
    canvas = soup.find(id="docgen2DocumentCanvas")
    assert title is not None
    assert title.get("value") == "Restored project"
    assert canvas is not None
    assert canvas.find("em") is not None
    assert canvas.get_text(" ", strip=True) == "Restored Editable content"
    assert canvas.find(attrs={"data-node-id": "n1"}) is not None
    editor = soup.find(id="docgen2Editor")
    assert editor is not None
    assert editor.get("data-revision") == "2"


def test_project_detail_renders_semantic_node_identifiers(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.get(f"/projects/{project_with_document.id}")

    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    heading = soup.find(attrs={"data-node-id": "n1"})
    assert heading is not None
    assert heading.get("data-kind") == "heading"
    assert heading.get("data-section-id") == "summary"
    assert soup.find(id="docgen2Editor").get("data-revision") == "1"


def test_project_detail_renders_semantic_node_formatting(
    client: TestClient, project_with_document: Project
) -> None:
    with _session(client) as session:
        document = DocumentRepository(session).get_document(project_with_document.id)
        assert document is not None
        nodes = [
            node.model_copy(
                update={
                    "data": {
                        "style": {
                            "color": "blue",
                            "font-weight": "700",
                            "margin-left": "24px",
                        }
                    }
                }
            )
            if node.id == "p1"
            else node
            for node in document.nodes
        ]
        DocumentRepository(session).save_document(
            project_with_document.id,
            document.model_copy(update={"nodes": nodes}),
        )
        session.commit()

    response = client.get(f"/projects/{project_with_document.id}")

    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    paragraph = soup.find("p", attrs={"data-node-id": "p1"})
    assert paragraph is not None
    assert paragraph.get("style") == "color:blue;font-weight:700;margin-left:24px"


def test_project_detail_renders_semantic_heading_level(
    client: TestClient, project_with_document: Project
) -> None:
    with _session(client) as session:
        document = DocumentRepository(session).get_document(project_with_document.id)
        assert document is not None
        nodes = [
            node.model_copy(update={"data": {"level": 1}})
            if node.id == "n1"
            else node
            for node in document.nodes
        ]
        DocumentRepository(session).save_document(
            project_with_document.id,
            document.model_copy(update={"nodes": nodes}),
        )
        session.commit()

    response = client.get(f"/projects/{project_with_document.id}")

    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    heading = soup.find("h1", attrs={"data-node-id": "n1"})
    assert heading is not None


def test_workspace_save_preserves_allowed_inline_formatting(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Styled document",
            "html": (
                '<p data-node-id="p1" style="color:blue;font-weight:700;'
                'margin-left:24px;position:absolute">Styled paragraph</p>'
            ),
            "revision": 1,
        },
    )

    assert response.status_code == 200
    assert response.json()["html"] == (
        '<p data-kind="paragraph" data-node-id="p1" '
        'style="color:blue;font-weight:700;margin-left:24px">Styled paragraph</p>'
    )
    saved = _stored_document(client, project_with_document.id).nodes[0]
    assert saved.data == {
        "style": {
            "color": "blue",
            "font-weight": "700",
            "margin-left": "24px",
        }
    }


def test_workspace_save_preserves_heading_level_in_document_data(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Heading level",
            "html": '<h1 data-node-id="n1">Top heading</h1>',
            "revision": 1,
        },
    )

    assert response.status_code == 200
    saved = _stored_document(client, project_with_document.id).nodes[0]
    assert saved.kind is NodeKind.HEADING
    assert saved.data["level"] == 1


def test_workspace_save_promotes_full_span_style_to_document_data(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Nested style",
            "html": (
                '<p data-node-id="p1">'
                '<span style="color:red;margin-left:24px">Styled paragraph</span>'
                "</p>"
            ),
            "revision": 1,
        },
    )

    assert response.status_code == 200
    saved = _stored_document(client, project_with_document.id).nodes[0]
    assert saved.data["style"] == {"color": "red", "margin-left": "24px"}
    assert saved.data["html"] == (
        '<span style="color:red;margin-left:24px">Styled paragraph</span>'
    )


def test_workspace_save_converts_browser_font_color_to_document_style(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Font color",
            "html": '<p data-node-id="p1"><font color="blue">Blue paragraph</font></p>',
            "revision": 1,
        },
    )

    assert response.status_code == 200
    saved = _stored_document(client, project_with_document.id).nodes[0]
    assert saved.data["style"] == {"color": "blue"}
    assert saved.data["html"] == '<span style="color:blue">Blue paragraph</span>'


def test_workspace_save_preserves_faq_question_list_presentation(
    client: TestClient,
) -> None:
    with _session(client) as session:
        project = Project(name="FAQ")
        session.add(project)
        session.flush()
        document = WorkingDocument(
            title="FAQ по материалам источников",
            template_id="faq",
            nodes=[
                DocumentNode(
                    id="faq-questions",
                    kind=NodeKind.LIST,
                    text="Общие вопросы",
                    data={
                        "items": [
                            "Вопрос: Первый вопрос Ответ: Первый ответ",
                            "Вопрос: Второй вопрос Ответ: Второй ответ",
                        ]
                    },
                )
            ],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        project_id = project.id

    page = BeautifulSoup(client.get(f"/projects/{project_id}").text, "html.parser")
    canvas = page.find(id="docgen2DocumentCanvas")
    assert canvas is not None
    faq_list = canvas.find("ol", attrs={"data-node-id": "faq-questions"})
    assert faq_list is not None
    assert faq_list.get("class") == ["faq-question-list"]
    assert faq_list.get("data-section-title") == "Общие вопросы"

    response = client.post(
        f"/projects/{project_id}/editor/save",
        json={
            "title": "FAQ по материалам источников",
            "html": canvas.decode_contents(),
            "revision": 1,
        },
    )

    assert response.status_code == 200
    saved_html = BeautifulSoup(response.json()["html"], "html.parser")
    saved_list = saved_html.find("ol", attrs={"data-node-id": "faq-questions"})
    assert saved_list is not None
    assert saved_list.get("class") == ["faq-question-list"]
    assert saved_list.get("data-section-title") == "Общие вопросы"


def test_workspace_save_preserves_faq_question_list_block_style(
    client: TestClient,
) -> None:
    with _session(client) as session:
        project = Project(name="FAQ")
        session.add(project)
        session.flush()
        document = WorkingDocument(
            title="FAQ по материалам источников",
            template_id="faq",
            nodes=[
                DocumentNode(
                    id="faq-questions",
                    kind=NodeKind.LIST,
                    text="Общие вопросы",
                    data={"items": ["Вопрос: Первый Ответ: Первый"]},
                )
            ],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        project_id = project.id

    response = client.post(
        f"/projects/{project_id}/editor/save",
        json={
            "title": "FAQ по материалам источников",
            "html": (
                '<ol class="faq-question-list" data-node-id="faq-questions" '
                'data-kind="list" data-section-title="Общие вопросы" '
                'style="color:green;font-style:italic">'
                "<li><strong>Вопрос:</strong> Первый<br>"
                "<strong>Ответ:</strong> Первый</li>"
                "</ol>"
            ),
            "revision": 1,
        },
    )

    assert response.status_code == 200
    saved_html = BeautifulSoup(response.json()["html"], "html.parser")
    saved_list = saved_html.find("ol", attrs={"data-node-id": "faq-questions"})
    assert saved_list is not None
    assert saved_list.get("style") == "color:green;font-style:italic"
    saved = _stored_document(client, project_id).nodes[0]
    assert saved.data["style"] == {"color": "green", "font-style": "italic"}


def test_workspace_save_promotes_common_faq_item_style_to_list_data(
    client: TestClient,
) -> None:
    with _session(client) as session:
        project = Project(name="FAQ")
        session.add(project)
        session.flush()
        document = WorkingDocument(
            title="FAQ по материалам источников",
            template_id="faq",
            nodes=[
                DocumentNode(
                    id="faq-questions",
                    kind=NodeKind.LIST,
                    text="Общие вопросы",
                    data={
                        "items": [
                            "Вопрос: Первый Ответ: Первый",
                            "Вопрос: Второй Ответ: Второй",
                        ]
                    },
                )
            ],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        project_id = project.id

    response = client.post(
        f"/projects/{project_id}/editor/save",
        json={
            "title": "FAQ по материалам источников",
            "html": (
                '<ol class="faq-question-list" data-node-id="faq-questions" '
                'data-kind="list" data-section-title="Общие вопросы">'
                '<li style="color:green;font-style:italic">'
                "<strong>Вопрос:</strong> Первый<br><strong>Ответ:</strong> Первый"
                "</li>"
                '<li><span style="color:green;font-style:italic">'
                "<strong>Вопрос:</strong> Второй<br><strong>Ответ:</strong> Второй"
                "</span></li>"
                "</ol>"
            ),
            "revision": 1,
        },
    )

    assert response.status_code == 200
    saved = _stored_document(client, project_id).nodes[0]
    assert saved.data["style"] == {"color": "green", "font-style": "italic"}


def test_workspace_save_restores_rich_list_item_html_from_document_data(
    client: TestClient,
) -> None:
    with _session(client) as session:
        project = Project(name="FAQ")
        session.add(project)
        session.flush()
        document = WorkingDocument(
            title="FAQ по материалам источников",
            template_id="faq",
            nodes=[
                DocumentNode(
                    id="faq-questions",
                    kind=NodeKind.LIST,
                    text="Общие вопросы",
                    data={"items": ["Вопрос: Первый Ответ: Первый"]},
                )
            ],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        project_id = project.id

    response = client.post(
        f"/projects/{project_id}/editor/save",
        json={
            "title": "FAQ по материалам источников",
            "html": (
                '<ol class="faq-question-list" data-node-id="faq-questions" '
                'data-kind="list" data-section-title="Общие вопросы">'
                "<li>"
                '<span style="color:green;font-family:Arial;font-size:18px;'
                'font-style:italic;font-weight:700">'
                "Вопрос: Первый<br>Ответ: Первый"
                "</span>"
                "</li>"
                "</ol>"
            ),
            "revision": 1,
        },
    )
    assert response.status_code == 200
    saved = _stored_document(client, project_id)

    with _session(client) as session:
        DocumentRepository(session).save_document(project_id, saved)
        session.commit()

    page = BeautifulSoup(client.get(f"/projects/{project_id}").text, "html.parser")
    restored = page.find("ol", attrs={"data-node-id": "faq-questions"})
    assert restored is not None
    restored_span = restored.find("span")
    assert restored_span is not None
    assert restored_span.get("style") == (
        "color:green;font-family:Arial;font-size:18px;"
        "font-style:italic;font-weight:700"
    )


def test_workspace_save_restores_list_item_style_from_document_data(
    client: TestClient,
) -> None:
    with _session(client) as session:
        project = Project(name="FAQ")
        session.add(project)
        session.flush()
        document = WorkingDocument(
            title="FAQ по материалам источников",
            template_id="faq",
            nodes=[
                DocumentNode(
                    id="faq-questions",
                    kind=NodeKind.LIST,
                    text="Общие вопросы",
                    data={
                        "items": [
                            "Вопрос: Первый Ответ: Первый",
                            "Вопрос: Второй Ответ: Второй",
                        ]
                    },
                )
            ],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        project_id = project.id

    response = client.post(
        f"/projects/{project_id}/editor/save",
        json={
            "title": "FAQ по материалам источников",
            "html": (
                '<ol class="faq-question-list" data-node-id="faq-questions" '
                'data-kind="list" data-section-title="Общие вопросы">'
                '<li style="color:green;font-style:italic">'
                "<strong>Вопрос:</strong> Первый<br><strong>Ответ:</strong> Первый"
                "</li>"
                "<li><strong>Вопрос:</strong> Второй<br><strong>Ответ:</strong> Второй</li>"
                "</ol>"
            ),
            "revision": 1,
        },
    )
    assert response.status_code == 200
    saved = _stored_document(client, project_id)

    with _session(client) as session:
        DocumentRepository(session).save_document(project_id, saved)
        session.commit()

    page = BeautifulSoup(client.get(f"/projects/{project_id}").text, "html.parser")
    restored_items = page.select('ol[data-node-id="faq-questions"] > li')
    assert len(restored_items) == 2
    assert restored_items[0].get("style") == "color:green;font-style:italic"
    assert restored_items[1].get("style") is None


def test_workspace_save_preserves_chat_applied_list_style_after_render_round_trip(
    client: TestClient,
) -> None:
    with _session(client) as session:
        project = Project(name="FAQ")
        session.add(project)
        session.flush()
        document = WorkingDocument(
            title="FAQ по материалам источников",
            template_id="faq",
            nodes=[
                DocumentNode(
                    id="faq-questions",
                    kind=NodeKind.LIST,
                    text="Общие вопросы",
                    data={
                        "items": ["Вопрос: Первый Ответ: Первый"],
                        "style": {"color": "green", "font-style": "italic"},
                    },
                )
            ],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        project_id = project.id

    page = BeautifulSoup(client.get(f"/projects/{project_id}").text, "html.parser")
    canvas = page.find(id="docgen2DocumentCanvas")
    assert canvas is not None
    rendered_list = canvas.find("ol", attrs={"data-node-id": "faq-questions"})
    assert rendered_list is not None
    assert rendered_list.get("style") == "color:green;font-style:italic"

    response = client.post(
        f"/projects/{project_id}/editor/save",
        json={
            "title": "FAQ по материалам источников",
            "html": canvas.decode_contents(),
            "revision": 1,
        },
    )

    assert response.status_code == 200
    saved = _stored_document(client, project_id).nodes[0]
    assert saved.data["style"] == {"color": "green", "font-style": "italic"}


def test_workspace_save_response_applies_list_style_to_items(
    client: TestClient,
) -> None:
    with _session(client) as session:
        project = Project(name="FAQ")
        session.add(project)
        session.flush()
        document = WorkingDocument(
            title="FAQ по материалам источников",
            template_id="faq",
            nodes=[
                DocumentNode(
                    id="faq-questions",
                    kind=NodeKind.LIST,
                    text="Общие вопросы",
                    data={
                        "items": ["Вопрос: Первый Ответ: Первый"],
                        "style": {"color": "green", "font-style": "italic"},
                    },
                )
            ],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        project_id = project.id

    response = client.post(
        f"/projects/{project_id}/editor/save",
        json={
            "title": "FAQ по материалам источников",
            "html": (
                '<ol class="faq-question-list" data-node-id="faq-questions" '
                'data-kind="list" data-section-title="Общие вопросы">'
                "<li><strong>Вопрос:</strong> Первый<br><strong>Ответ:</strong> Первый</li>"
                "</ol>"
            ),
            "revision": 1,
        },
    )

    assert response.status_code == 200
    returned_html = BeautifulSoup(response.json()["html"], "html.parser")
    returned_list = returned_html.find("ol", attrs={"data-node-id": "faq-questions"})
    assert returned_list is not None
    assert returned_list.get("style") == "color:green;font-style:italic"
    returned_item = returned_list.find("li")
    assert returned_item is not None
    assert returned_item.get("style") == "color:green;font-style:italic"


def test_workspace_save_keeps_existing_style_when_workspace_html_has_no_style(
    client: TestClient, project_with_document: Project
) -> None:
    with _session(client) as session:
        document = DocumentRepository(session).get_document(project_with_document.id)
        assert document is not None
        nodes = [
            node.model_copy(
                update={"data": {"style": {"color": "green", "font-style": "italic"}}}
            )
            if node.id == "p1"
            else node
            for node in document.nodes
        ]
        DocumentRepository(session).save_document(
            project_with_document.id,
            document.model_copy(update={"nodes": nodes}),
        )
        session.commit()

    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "FAQ по материалам источников",
            "html": '<p data-node-id="p1">Абзац без inline style</p>',
            "revision": 2,
        },
    )

    assert response.status_code == 200
    saved = _stored_document(client, project_with_document.id).nodes[0]
    assert saved.data["style"] == {"color": "green", "font-style": "italic"}


def test_workspace_save_response_restores_existing_style_to_returned_html(
    client: TestClient, project_with_document: Project
) -> None:
    with _session(client) as session:
        document = DocumentRepository(session).get_document(project_with_document.id)
        assert document is not None
        nodes = [
            node.model_copy(
                update={"data": {"style": {"color": "green", "font-style": "italic"}}}
            )
            if node.id == "p1"
            else node
            for node in document.nodes
        ]
        DocumentRepository(session).save_document(
            project_with_document.id,
            document.model_copy(update={"nodes": nodes}),
        )
        session.commit()

    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "FAQ по материалам источников",
            "html": '<p data-node-id="p1">Абзац без inline style</p>',
            "revision": 2,
        },
    )

    assert response.status_code == 200
    returned_html = BeautifulSoup(response.json()["html"], "html.parser")
    paragraph = returned_html.find("p", attrs={"data-node-id": "p1"})
    assert paragraph is not None
    assert paragraph.get("style") == "color:green;font-style:italic"


def test_workspace_save_preserves_browser_rgb_color_and_italic_style(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "RGB style",
            "html": (
                '<p data-node-id="p1" '
                'style="color: rgb(0, 128, 0); font-style: italic;">'
                "Зелёный курсив"
                "</p>"
            ),
            "revision": 1,
        },
    )

    assert response.status_code == 200
    saved = _stored_document(client, project_with_document.id).nodes[0]
    assert saved.data["style"] == {
        "color": "rgb(0, 128, 0)",
        "font-style": "italic",
    }
    returned_html = BeautifulSoup(response.json()["html"], "html.parser")
    paragraph = returned_html.find("p", attrs={"data-node-id": "p1"})
    assert paragraph is not None
    assert paragraph.get("style") == "color:rgb(0, 128, 0);font-style:italic"


def test_project_detail_after_workspace_save_keeps_existing_style_in_workspace_html(
    client: TestClient, project_with_document: Project
) -> None:
    with _session(client) as session:
        document = DocumentRepository(session).get_document(project_with_document.id)
        assert document is not None
        nodes = [
            node.model_copy(
                update={"data": {"style": {"color": "green", "font-style": "italic"}}}
            )
            if node.id == "p1"
            else node
            for node in document.nodes
        ]
        DocumentRepository(session).save_document(
            project_with_document.id,
            document.model_copy(update={"nodes": nodes}),
        )
        session.commit()

    saved = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "FAQ по материалам источников",
            "html": '<p data-node-id="p1">Абзац без inline style</p>',
            "revision": 2,
        },
    )
    assert saved.status_code == 200

    response = client.get(f"/projects/{project_with_document.id}")

    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    paragraph = page.find("p", attrs={"data-node-id": "p1"})
    assert paragraph is not None
    assert paragraph.get("style") == "color:green;font-style:italic"


def test_workspace_save_restores_rich_paragraph_html_from_document_data(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": "Rich paragraph",
            "html": (
                '<p data-node-id="p1">'
                '<font color="green" face="Arial">'
                "<b><i><u>Formatted paragraph</u></i></b>"
                "</font>"
                "</p>"
            ),
            "revision": 1,
        },
    )
    assert response.status_code == 200
    saved = _stored_document(client, project_with_document.id)

    with _session(client) as session:
        DocumentRepository(session).save_document(project_with_document.id, saved)
        session.commit()

    page = BeautifulSoup(
        client.get(f"/projects/{project_with_document.id}").text,
        "html.parser",
    )
    paragraph = page.find("p", attrs={"data-node-id": "p1"})
    assert paragraph is not None
    span = paragraph.find("span")
    assert span is not None
    assert span.get("style") == "color:green;font-family:Arial"
    assert span.find("b") is not None
    assert span.find("i") is not None
    assert span.find("u") is not None


def test_no_op_workspace_save_preserves_generated_document_title(
    client: TestClient, project_with_document: Project
) -> None:
    page = BeautifulSoup(
        client.get(f"/projects/{project_with_document.id}").text,
        "html.parser",
    )
    title = page.find(id="docgen2EditorTitle")
    canvas = page.find(id="docgen2DocumentCanvas")
    assert title is not None
    assert canvas is not None

    response = client.post(
        f"/projects/{project_with_document.id}/editor/save",
        json={
            "title": title.get("value"),
            "html": canvas.decode_contents(),
            "revision": 1,
        },
    )

    assert response.status_code == 200
    assert _stored_document(client, project_with_document.id).title == "Рабочий документ"


def test_workspace_save_keeps_project_identity_separate_from_document_title(
    client: TestClient,
) -> None:
    with _session(client) as session:
        project = Project(name="Project identity")
        session.add(project)
        session.flush()
        document = WorkingDocument(
            title="Generated title",
            template_id="use-case",
            nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Body")],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        project_id = project.id

    page = BeautifulSoup(client.get(f"/projects/{project_id}").text, "html.parser")
    title = page.find(id="docgen2EditorTitle")
    canvas = page.find(id="docgen2DocumentCanvas")
    assert title is not None
    assert title.get("value") == "Generated title"
    assert canvas is not None

    no_op = client.post(
        f"/projects/{project_id}/editor/save",
        json={
            "title": "Generated title",
            "html": canvas.decode_contents(),
            "revision": 1,
        },
    )

    assert no_op.status_code == 200
    with _session(client) as session:
        project = session.get(Project, project_id)
        stored = DocumentRepository(session).get_document(project_id)
        assert project is not None
        assert stored is not None
        assert project.name == "Project identity"
        assert stored.title == "Generated title"

    edited = client.post(
        f"/projects/{project_id}/editor/save",
        json={
            "title": "Edited document title",
            "html": no_op.json()["html"],
            "revision": no_op.json()["revision"],
        },
    )

    assert edited.status_code == 200
    with _session(client) as session:
        project = session.get(Project, project_id)
        stored = DocumentRepository(session).get_document(project_id)
        assert project is not None
        assert stored is not None
        assert project.name == "Project identity"
        assert stored.title == "Edited document title"


def _save_workspace(client: TestClient, project_id: str, revision: int):
    return client.post(
        f"/projects/{project_id}/editor/save",
        json={
            "title": f"Версия {revision}",
            "html": '<h2 data-node-id="n1">Обновлённый заголовок</h2>',
            "revision": revision,
        },
    )


def _stored_document(client: TestClient, project_id: str) -> WorkingDocument:
    with _session(client) as session:
        document = DocumentRepository(session).get_document(project_id)
        assert document is not None
        return document


@contextmanager
def _session(client: TestClient) -> Iterator[Session]:
    session = client.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
