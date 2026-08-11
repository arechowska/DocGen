from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from docgen.chat.routes import _source_blocks_from_project
from docgen.chat.schemas import ChatEditRequest, ChatEditResult
from docgen.chat.service import ChatGroundingError
from docgen.config import Settings
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.main import create_app
from docgen.models import Project


class FakeChat:
    def edit(self, project_id: str, request: ChatEditRequest) -> ChatEditResult:
        assert project_id
        if "неподтверждённый" in request.message:
            raise ChatGroundingError("Для этой правки нет подтверждения в источниках")
        return ChatEditResult(
            summary="Заголовок сокращён",
            document=WorkingDocument(
                title="Документ",
                template_id="use-case",
                nodes=[DocumentNode(id="n1", kind=NodeKind.HEADING, text="Коротко")],
            ),
            revision=request.expected_revision + 1,
        )


@pytest.fixture
def client() -> Iterator[TestClient]:
    root = Path("var/chat-route-tests") / uuid4().hex
    settings = Settings(
        database_url=f"sqlite:///{root / 'test.db'}",
        data_dir=root / "data",
        confluence_hosts=("wiki.example.test",),
    )
    app = create_app(settings)
    app.state.chat_service_factory = lambda request, session: FakeChat()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def project_with_document(client: TestClient) -> Project:
    session = client.app.state.session_factory()
    try:
        project = Project(name="Проект с документом")
        session.add(project)
        session.flush()
        document = WorkingDocument(
            title="Документ",
            template_id="use-case",
            nodes=[DocumentNode(id="n1", kind=NodeKind.HEADING, text="Длинный заголовок")],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        return project
    finally:
        session.close()


def test_chat_returns_message_and_refreshes_document(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/chat",
        data={"message": "Сделай заголовок короче", "revision": "1"},
    )

    assert response.status_code == 200
    assert "Заголовок сокращён" in response.text
    assert "HX-Trigger" in response.headers


def test_chat_grounding_error_preserves_document(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/chat",
        data={"message": "Добавь неподтверждённый факт", "revision": "1"},
    )

    assert response.status_code == 422
    assert "нет подтверждения в источниках" in response.text


def test_chat_source_blocks_come_from_uploaded_sources(
    client: TestClient, project_with_document: Project
) -> None:
    upload = client.post(
        f"/projects/{project_with_document.id}/sources/files",
        files={
            "file": ("source.md", "# Подтверждённый факт".encode(), "text/markdown")
        },
        headers={"HX-Request": "true"},
    )
    assert upload.status_code == 200

    session = client.app.state.session_factory()
    try:
        blocks = _source_blocks_from_project(
            session, client.app.state.settings, project_with_document.id
        )
    finally:
        session.close()

    assert [block.text for block in blocks] == ["Подтверждённый факт"]
    assert not any(block.id.startswith("document:") for block in blocks)
