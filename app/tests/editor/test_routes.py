from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from docgen.config import Settings
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
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
                DocumentNode(id="n1", kind=NodeKind.HEADING, text="Заголовок"),
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


@contextmanager
def _session(client: TestClient) -> Iterator[Session]:
    session = client.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
