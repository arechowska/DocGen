from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from docgen.chat.schemas import ChatEditRequest, ChatEditResult
from docgen.config import Settings
from docgen.documents.edit_service import DocumentEditService
from docgen.documents.operations import UpdateText
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.main import create_app
from docgen.models import Project, Source, SourceKind


class JourneyChat:
    def __init__(self, session: Session) -> None:
        self._session = session

    def edit(self, project_id: str, request: ChatEditRequest) -> ChatEditResult:
        result = DocumentEditService(DocumentRepository(self._session)).apply(
            project_id,
            request.expected_revision,
            [UpdateText(node_id="result", text="Уточнённый результат")],
        )
        return ChatEditResult(
            summary="Результат уточнён",
            document=result.document,
            revision=result.revision,
        )


def test_edit_chat_save_restart_and_recheck() -> None:
    root = Path("var/stage3-journey") / uuid4().hex
    settings = Settings(
        database_url=f"sqlite:///{root / 'test.db'}",
        data_dir=root / "data",
        confluence_hosts=("wiki.example.test",),
        local_text_base_url="http://text-model.test/v1",
        local_text_model="text-model",
        local_vision_base_url="http://vision-model.test/v1",
        local_vision_model="vision-model",
        trusted_integration_hosts=("text-model.test", "vision-model.test"),
    )

    app = create_app(settings)
    app.state.chat_service_factory = lambda request, session: JourneyChat(session)
    with TestClient(app) as client:
        project_id = _seed_project(client)
        chat_url = f"/projects/{project_id}/chat"
        response = client.post(
            f"/projects/{project_id}/editor/save",
            json={
                "title": "Документ Stage 3",
                "html": (
                    '<p data-node-id="manual">Ручная правка</p>'
                    '<p data-node-id="result">Исходный результат</p>'
                ),
                "revision": 1,
            },
        )
        assert response.status_code == 200

        response = client.post(
            chat_url,
            data={
                "message": "Уточни результат по источнику",
                "revision": "2",
            },
        )
        assert response.status_code == 200
        session = client.app.state.session_factory()
        try:
            repository = DocumentRepository(session)
            saved = repository.get_document(project_id)
            assert saved is not None
            assert saved.nodes[0].text == "Ручная правка"
            assert saved.nodes[1].text == "Уточнённый результат"
            assert repository.get_workspace_html(project_id) is None
        finally:
            session.close()

        second_chat = client.post(
            chat_url,
            data={
                "message": "Ещё раз уточни результат",
                "revision": "3",
            },
        )
        assert second_chat.status_code == 200

        second_visual_save = client.post(
            f"/projects/{project_id}/editor/save",
            json={
                "title": "Документ Stage 3",
                "html": (
                    '<p data-node-id="manual">Ручная правка после чата</p>'
                    '<p data-node-id="result">Уточнённый результат</p>'
                ),
                "revision": 4,
            },
        )
        assert second_visual_save.status_code == 200
        assert second_visual_save.json()["revision"] == 5

    restarted_app = create_app(settings)
    restarted_app.state.chat_service_factory = lambda request, session: JourneyChat(session)
    with TestClient(restarted_app) as restarted:
        editor = restarted.get(f"/projects/{project_id}/editor")
        assert editor.status_code == 200
        assert "Ручная правка после чата" in editor.text
        assert "Уточнённый результат" in editor.text

        project_page = restarted.get(f"/projects/{project_id}")
        assert project_page.status_code == 200
        assert 'id="project-workspace"' in project_page.text
        assert 'id="docgen2Editor"' in project_page.text
        assert 'id="docgen2DocumentCanvas"' in project_page.text
        assert "Ручная правка после чата" in project_page.text
        assert "Уточнённый результат" in project_page.text

        response = restarted.post(
            f"/projects/{project_id}/jobs/check",
            data={"template_id": "use-case"},
        )
        assert response.status_code == 202


def _seed_project(client: TestClient) -> str:
    session = client.app.state.session_factory()
    try:
        project = Project(name="Stage 3")
        session.add(project)
        session.flush()
        source = Source(
            project_id=project.id,
            kind=SourceKind.FILE,
            display_name="source.md",
            media_type="text/markdown",
            size_bytes=20,
            storage_path="projects/source.md",
            status="stored",
        )
        session.add(source)
        document = WorkingDocument(
            title="Документ Stage 3",
            template_id="use-case",
            nodes=[
                DocumentNode(id="manual", kind=NodeKind.PARAGRAPH, text="Исходный текст"),
                DocumentNode(id="result", kind=NodeKind.PARAGRAPH, text="Исходный результат"),
            ],
        )
        DocumentRepository(session).save_document(project.id, document)
        session.commit()
        return project.id
    finally:
        session.close()
