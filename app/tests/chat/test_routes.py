from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from docgen.chat.errors import ChatError, ChatErrorCode
from docgen.chat.routes import _source_blocks_from_project, _source_snapshot_from_project
from docgen.chat.schemas import ChatEditRequest, ChatEditResult, FindingFixProposal
from docgen.chat.service import ChatGroundingError, ChatService
from docgen.config import Settings
from docgen.documents.operations import UpdateText
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
from docgen.templates_catalog.loader import TemplateCatalog


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

    def propose_finding_fix(
        self,
        project_id: str,
        finding: CheckFinding,
        expected_revision: int,
        **_: object,
    ) -> FindingFixProposal:
        assert project_id
        assert finding.suggestion
        return FindingFixProposal(
            summary="Предложено сокращение заголовка",
            operations=[UpdateText(node_id="n1", text="Исправлено")],
            document=WorkingDocument(
                title="Документ",
                template_id="use-case",
                nodes=[DocumentNode(id="n1", kind=NodeKind.HEADING, text="Исправлено")],
            ),
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


def test_finding_fix_is_previewed_then_applied_and_keeps_stale_report(
    client: TestClient, project_with_report: Project
) -> None:
    preview = client.post(
        f"/projects/{project_with_report.id}/report/findings/structure-1/propose-fix",
        data={"revision": "1"},
    )

    assert preview.status_code == 200
    assert "Предлагаемая правка" in preview.text
    assert "Длинный заголовок" in preview.text
    assert "Исправлено" in preview.text
    marker = "/report/fix-proposals/"
    proposal_id = preview.text.split(marker, 1)[1].split("/apply", 1)[0]

    applied = client.post(
        f"/projects/{project_with_report.id}/report/fix-proposals/{proposal_id}/apply"
    )

    assert applied.status_code == 200
    assert "изменения добавлены в редактор" in applied.text
    assert "Документ изменён после этой проверки" in applied.text
    assert "Проверить снова" in applied.text
    assert 'hx-trigger="load"' in applied.text
    assert 'name="target_source_id"' not in applied.text
    assert "HX-Trigger" in applied.headers

    session = client.app.state.session_factory()
    try:
        stored = DocumentRepository(session).get_document_with_revision(
            project_with_report.id
        )
        assert stored is not None
        assert stored[1] == 2
        assert stored[0].nodes[0].text == "Исправлено"
        assert DocumentRepository(session).get_report(project_with_report.id) is None
        assert (
            DocumentRepository(session).get_latest_report_record(
                project_with_report.id
            )
            is not None
        )
    finally:
        session.close()


def test_structure_fix_appends_only_missing_fields_through_preview_and_apply(
    client: TestClient, project_with_document: Project
) -> None:
    template = TemplateCatalog().get("use-case")
    contract = template.structure_check
    assert contract is not None
    metadata = next(table for table in contract.required_tables if table.id == "metadata")
    original_row = [metadata.required_labels[0], "UC-77"]
    session = client.app.state.session_factory()
    try:
        documents = DocumentRepository(session)
        revision = documents.save_document(
            project_with_document.id,
            WorkingDocument(
                title="Текущий документ",
                template_id="no-template",
                nodes=[
                    DocumentNode(
                        id="metadata-table",
                        kind=NodeKind.TABLE,
                        data={"rows": [original_row]},
                    ),
                    DocumentNode(
                        id="body", kind=NodeKind.PARAGRAPH, text="Неизменяемый текст"
                    ),
                ],
            ),
        )
        documents.save_report(
            project_with_document.id,
            CheckReport(
                template_id="use-case",
                findings=[
                    CheckFinding(
                        code="template-structure-mismatch",
                        severity=Severity.ERROR,
                        confidence=1,
                        message="В таблице отсутствуют обязательные поля",
                        suggestion="Добавить отсутствующие поля",
                        rule_id=contract.rule_id,
                    )
                ],
            ),
            expected_document_revision=revision,
        )
        session.commit()
    finally:
        session.close()

    class ModelMustNotRun:
        def generate_json(self, **_: object) -> object:
            raise AssertionError("Модель не должна вызываться для точечной структуры")

    client.app.state.chat_service_factory = lambda request, session: ChatService(
        documents=DocumentRepository(session),
        model=ModelMustNotRun(),  # type: ignore[arg-type]
        source_blocks=lambda _: [],
    )
    preview = client.post(
        f"/projects/{project_with_document.id}/report/findings/{contract.rule_id}/propose-fix",
        data={"revision": str(revision)},
        headers={"HX-Target": "report-fix-preview-use-case-template-form"},
    )

    assert preview.status_code == 200
    assert "Предлагаемая правка" in preview.text
    assert "UC-77" in preview.text
    assert metadata.required_labels[1] in preview.text
    assert '&quot;rows&quot;' not in preview.text
    assert 'hx-target="#report-fix-preview-use-case-template-form"' in preview.text
    proposal_id = preview.text.split("/report/fix-proposals/", 1)[1].split(
        "/apply", 1
    )[0]

    applied = client.post(
        f"/projects/{project_with_document.id}/report/fix-proposals/{proposal_id}/apply",
        headers={"HX-Target": "report-fix-preview-use-case-template-form"},
    )

    assert applied.status_code == 200
    assert "Добавлено в редактор" in applied.text
    assert "Вернуться в чат" in applied.text
    assert 'hx-trigger="load"' not in applied.text
    session = client.app.state.session_factory()
    try:
        stored = DocumentRepository(session).get_document_with_revision(
            project_with_document.id
        )
        assert stored is not None
        assert stored[1] == revision + 1
        table = next(node for node in stored[0].nodes if node.id == "metadata-table")
        assert table.data["rows"][0] == original_row
        assert [row[0] for row in table.data["rows"][1:]] == list(
            metadata.required_labels[1:]
        )
        assert next(node for node in stored[0].nodes if node.id == "body").text == (
            "Неизменяемый текст"
        )
    finally:
        session.close()


def test_apply_finding_fix_without_suggestion_is_rejected(
    client: TestClient, project_with_report: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_report.id}/report/findings/terminology-1/propose-fix",
        data={"revision": "1"},
    )

    assert response.status_code == 404
    assert "нет предложенного исправления" in response.text


def test_apply_finding_fix_for_unknown_rule_is_rejected(
    client: TestClient, project_with_report: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_report.id}/report/findings/unknown-rule/propose-fix",
        data={"revision": "1"},
    )

    assert response.status_code == 404


def test_apply_finding_fix_without_revision_returns_readable_error(
    client: TestClient, project_with_report: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_report.id}/report/findings/structure-1/propose-fix",
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
        snapshot = _source_snapshot_from_project(
            session, client.app.state.settings, project_with_document.id
        )
    finally:
        session.close()

    assert [block.text for block in blocks] == ["Подтверждённый факт"]
    assert not any(block.id.startswith("document:") for block in blocks)
    assert [source.display_name for source in snapshot.sources] == ["source.md"]
