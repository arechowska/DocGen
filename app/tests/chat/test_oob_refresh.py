from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from docgen.chat.schemas import ChatEditRequest, ChatEditResult
from docgen.config import Settings
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.main import create_app
from docgen.models import Project


class StyledFakeChat:
    def edit(self, project_id: str, request: ChatEditRequest) -> ChatEditResult:
        assert project_id
        return ChatEditResult(
            summary="Applied style",
            document=WorkingDocument(
                title="Document",
                template_id="use-case",
                nodes=[
                    DocumentNode(
                        id="n1",
                        kind=NodeKind.PARAGRAPH,
                        text="Styled text",
                        data={"style": {"color": "blue", "font-weight": "700"}},
                    )
                ],
            ),
            revision=request.expected_revision + 1,
        )


@pytest.fixture
def client() -> Iterator[TestClient]:
    root = Path("var/chat-oob-tests") / uuid4().hex
    settings = Settings(
        database_url=f"sqlite:///{root / 'test.db'}",
        data_dir=root / "data",
        confluence_hosts=("wiki.example.test",),
    )
    app = create_app(settings)
    app.state.chat_service_factory = lambda request, session: StyledFakeChat()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def project_with_document(client: TestClient) -> Project:
    session = client.app.state.session_factory()
    try:
        project = Project(name="Project")
        session.add(project)
        session.flush()
        document = WorkingDocument(
            title="Document",
            template_id="use-case",
            nodes=[DocumentNode(id="n1", kind=NodeKind.PARAGRAPH, text="Plain text")],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        return project
    finally:
        session.close()


def test_chat_response_includes_oob_editor_refresh(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/chat",
        data={"message": "style edit", "revision": "1"},
    )

    assert response.status_code == 200
    assert "Applied style" in response.text
    assert 'id="docgen2Editor"' in response.text
    assert 'hx-swap-oob="outerHTML"' in response.text
    assert 'style="color:blue;font-weight:700"' in response.text


def test_chat_oob_editor_uses_chat_result_instead_of_stale_workspace_html(
    client: TestClient, project_with_document: Project
) -> None:
    session = client.app.state.session_factory()
    try:
        document = DocumentRepository(session).get_document(project_with_document.id)
        assert document is not None
        DocumentRepository(session).save_workspace(
            project_with_document.id,
            expected_revision=1,
            document=document,
            html='<p data-node-id="n1">Plain saved snapshot</p>',
        )
        session.commit()
    finally:
        session.close()

    response = client.post(
        f"/projects/{project_with_document.id}/chat",
        data={"message": "style edit", "revision": "2"},
    )

    assert response.status_code == 200
    assert "Plain saved snapshot" not in response.text
    assert 'style="color:blue;font-weight:700"' in response.text
