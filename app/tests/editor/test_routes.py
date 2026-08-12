from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from docgen.config import Settings
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.schemas import Provenance
from docgen.main import create_app
from docgen.models import Project


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


def test_editor_renders_all_node_kinds(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.get(f"/projects/{project_with_document.id}/editor")

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


def test_standalone_editor_uses_shared_surface(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.get(f"/projects/{project_with_document.id}/editor")

    assert response.status_code == 200
    assert 'id="editor-page"' in response.text
    assert 'id="editor-shell"' in response.text
    assert 'data-state="ready"' in response.text
    assert 'hx-target="#editor-shell"' in response.text
    assert 'id="chat-panel"' in response.text


def test_htmx_editor_refresh_returns_shared_surface_only(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.get(
        f"/projects/{project_with_document.id}/editor",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "<!doctype html>" not in response.text.lower()
    assert 'id="editor-page"' not in response.text
    assert 'id="editor-shell"' in response.text
    assert 'data-state="ready"' in response.text


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
    assert saved.nodes[0].data["rows"] == [["Ключ", "Значение"], ["A", "1"]]
    assert saved.nodes[1].data["items"] == ["Первый", "Второй"]
    assert saved.nodes[2].text == "Добавленный вручную абзац"
    assert saved.nodes[2].flags == ["manual-edit"]
    assert saved.nodes[2].provenance == []


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
