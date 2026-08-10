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
    root = Path("var/editor-complex-tests") / uuid4().hex
    settings = Settings(
        database_url=f"sqlite:///{root / 'test.db'}",
        data_dir=root / "data",
        confluence_hosts=("wiki.example.test",),
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def project_with_table(client: TestClient) -> Project:
    return _project_with_node(
        client,
        DocumentNode(
            id="table-1",
            kind=NodeKind.TABLE,
            data={"rows": [["A", "B"], ["C", "D"]]},
        ),
    )


@pytest.fixture
def project_with_image(client: TestClient) -> Project:
    return _project_with_node(
        client,
        DocumentNode(
            id="image-1",
            kind=NodeKind.IMAGE,
            text="Схема",
            data={"alignment": "center", "width": 80, "alt": "Схема процесса"},
        ),
    )


def test_table_rejects_ragged_rows(
    client: TestClient, project_with_table: Project
) -> None:
    response = client.patch(
        f"/projects/{project_with_table.id}/editor/nodes/table-1/table",
        json={"revision": 1, "rows": [["A", "B"], ["C"]]},
    )

    assert response.status_code == 422
    assert "Все строки таблицы должны иметь одинаковое число ячеек" in response.text


def test_image_alignment_is_limited(
    client: TestClient, project_with_image: Project
) -> None:
    response = client.patch(
        f"/projects/{project_with_image.id}/editor/nodes/image-1/image",
        json={"revision": 1, "alignment": "outside"},
    )

    assert response.status_code == 422


def _project_with_node(client: TestClient, node: DocumentNode) -> Project:
    with _session(client) as session:
        project = Project(name="Проект со сложным блоком")
        session.add(project)
        session.flush()
        document = WorkingDocument(
            title="Рабочий документ",
            template_id="use-case",
            nodes=[node],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        return project


@contextmanager
def _session(client: TestClient) -> Iterator[Session]:
    session = client.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
