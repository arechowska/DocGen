from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from docgen.chat.errors import ChatError, ChatErrorCode
from docgen.chat.routes import _source_blocks_from_project
from docgen.chat.schemas import ChatEditRequest, ChatEditResult
from docgen.chat.service import ChatGroundingError
from docgen.config import Settings
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import (
    CheckFinding,
    CheckReport,
    DocumentNode,
    NodeKind,
    Severity,
    WorkingDocument,
)
from docgen.main import create_app
from docgen.models import Project


class FakeChat:
    def edit(self, project_id: str, request: ChatEditRequest) -> ChatEditResult:
        assert project_id
        if "неподтверждённый" in request.message:
            raise ChatGroundingError("Для этой правки нет подтверждения в источниках")
        for code in ChatErrorCode:
            if request.message == code.value:
                raise ChatError(code)
        return ChatEditResult(
            summary="Заголовок сокращён",
            document=WorkingDocument(
                title="Документ",
                template_id="use-case",
                nodes=[DocumentNode(id="n1", kind=NodeKind.HEADING, text="Коротко")],
            ),
            revision=request.expected_revision + 1,
        )

    def apply_finding_fix(
        self, project_id: str, finding: CheckFinding, expected_revision: int
    ) -> ChatEditResult:
        assert project_id
        assert finding.suggestion
        return ChatEditResult(
            summary="Правка внесена по отчёту",
            document=WorkingDocument(
                title="Документ",
                template_id="use-case",
                nodes=[DocumentNode(id="n1", kind=NodeKind.HEADING, text="Исправлено")],
            ),
            revision=expected_revision + 1,
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


@pytest.fixture
def project_with_report(client: TestClient, project_with_document: Project) -> Project:
    session = client.app.state.session_factory()
    try:
        report = CheckReport(
            template_id="use-case",
            findings=[
                CheckFinding(
                    code="c1",
                    severity=Severity.WARNING,
                    confidence=0.9,
                    message="Заголовок слишком длинный",
                    suggestion="Сократи заголовок",
                    node_id="n1",
                    rule_id="structure-1",
                ),
                CheckFinding(
                    code="c2",
                    severity=Severity.INFO,
                    confidence=0.8,
                    message="Замечание без предложенного исправления",
                    node_id="n1",
                    rule_id="terminology-1",
                ),
            ],
        )
        DocumentRepository(session).save_report(
            project_with_document.id, report, expected_document_revision=1
        )
        session.commit()
        return project_with_document
    finally:
        session.close()


def test_apply_finding_fix_returns_message_and_refreshes_document(
    client: TestClient, project_with_report: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_report.id}/report/findings/structure-1/apply-fix",
        data={"revision": "1"},
    )

    assert response.status_code == 200
    assert "Правка внесена по отчёту" in response.text
    assert "HX-Trigger" in response.headers


def test_apply_finding_fix_without_suggestion_is_rejected(
    client: TestClient, project_with_report: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_report.id}/report/findings/terminology-1/apply-fix",
        data={"revision": "1"},
    )

    assert response.status_code == 404
    assert "нет предложенного исправления" in response.text


def test_apply_finding_fix_for_unknown_rule_is_rejected(
    client: TestClient, project_with_report: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_report.id}/report/findings/unknown-rule/apply-fix",
        data={"revision": "1"},
    )

    assert response.status_code == 404


def test_apply_finding_fix_without_revision_returns_readable_error(
    client: TestClient, project_with_report: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_report.id}/report/findings/structure-1/apply-fix",
    )

    assert response.status_code == 422
    assert "ревизию" in response.text


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


@pytest.mark.parametrize(
    ("code", "expected_status", "expected_text"),
    [
        (ChatErrorCode.SOURCES_MISSING, 422, "не добавлены источники"),
        (ChatErrorCode.SOURCE_UNAVAILABLE, 503, "не удалось прочитать"),
        (ChatErrorCode.RELEVANT_FRAGMENT_MISSING, 422, "не найден фрагмент"),
        (ChatErrorCode.MODEL_INVALID_JSON, 502, "неверном формате"),
        (ChatErrorCode.EVIDENCE_MISSING, 422, "не содержит подтверждения"),
        (ChatErrorCode.REVISION_CONFLICT, 409, "уже изменён"),
    ],
)
def test_chat_renders_stable_error_code_message_and_next_action(
    client: TestClient,
    project_with_document: Project,
    code: ChatErrorCode,
    expected_status: int,
    expected_text: str,
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/chat",
        data={"message": code.value, "revision": "1"},
    )

    assert response.status_code == expected_status
    assert f'data-error-code="{code.value}"' in response.text
    assert expected_text in response.text
    assert "Что сделать:" in response.text


def test_chat_without_message_returns_readable_error(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(f"/projects/{project_with_document.id}/chat", data={})

    assert response.status_code == 422
    assert "Введите сообщение" in response.text
    assert "Field required" not in response.text


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
