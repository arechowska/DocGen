from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from docgen.ai.grounding import GroundingValidator
from docgen.chat.schemas import ChatEditOperation, ChatEditPlan
from docgen.chat.service import ChatService
from docgen.config import Settings
from docgen.documents.operations import UpdateData
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import CheckReport, DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.confluence import ConfluenceClient
from docgen.extraction.registry import ExtractorRegistry
from docgen.extraction.schemas import NormalizedBlock, Provenance
from docgen.jobs.models import CheckTargetKind, JobKind, JobStatus
from docgen.jobs.repository import JobRepository
from docgen.jobs.runner import JobRunner
from docgen.main import create_app
from docgen.projects.repository import ProjectRepository
from docgen.sources.repository import SourceRepository
from docgen.sources.storage import LocalStorage
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.workflows.check import CheckWorkflow
from docgen.workflows.normalize import NormalizationWorkflow


def test_formatta_workspace_preserves_template_through_edit_and_recheck(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'stage3.db'}",
        data_dir=tmp_path / "data",
        confluence_hosts=("wiki.example.test",),
        local_text_base_url="http://offline-text-model.test/v1",
        local_text_model="deterministic-text-model",
        trusted_integration_hosts=("offline-text-model.test",),
    )
    model = _JourneyModel()
    app = create_app(settings)
    app.state.chat_service_factory = lambda request, session: ChatService(
        documents=DocumentRepository(session),
        model=model,
        source_blocks=lambda project_id: _normalized_source_blocks(
            session, request.app.state.settings, project_id
        ),
    )

    with TestClient(app) as client:
        project_id, project_url = _create_document(client)
        source_id = _upload_markdown(client, project_url, project_id)
        original = _persist_generated_faq(client, project_id)

        workspace = client.get(project_url)
        assert workspace.status_code == 200
        assert 'option value="faq"' in workspace.text

        visual_save = client.post(
            f"{project_url}/editor/save",
            json={
                "title": "FAQ по работе с Formatta",
                "html": (
                    '<ul data-node-id="faq-general"><li>Что такое Formatta? '
                    "Formatta помогает собрать документ.</li></ul>"
                    '<ul data-node-id="faq-start"><li>Как начать работу?</li></ul>'
                    '<ul data-node-id="faq-work"><li>Как собрать документ?</li></ul>'
                    '<ul data-node-id="faq-errors"><li>Что делать при ошибке?</li></ul>'
                    "<p>Проверено редактором.</p>"
                ),
                "revision": 1,
            },
        )
        assert visual_save.status_code == 200
        assert visual_save.json()["revision"] == 2

        chat_edit = client.post(
            f"{project_url}/chat",
            data={
                "message": "Уточни начало работы строго по загруженному источнику",
                "revision": "2",
            },
        )
        assert chat_edit.status_code == 200
        assert "Начало работы уточнено по источнику" in chat_edit.text
        assert model.chat_evidence_id is not None

        current_check = client.post(
            f"{project_url}/jobs/check",
            data={"template_id": "faq"},
        )
        assert current_check.status_code == 202
        current_check_job_id = _job_id(current_check.text)
        _run_check_worker(client, model)
        assert _job(client, current_check_job_id).status is JobStatus.SUCCEEDED

        saved = _stored_document(client, project_id)
        assert saved.template_id == "faq"
        assert [node.id for node in saved.nodes[:4]] == [
            "faq-general",
            "faq-start",
            "faq-work",
            "faq-errors",
        ]
        assert saved.nodes[0].data["items"] == [
            "Что такое Formatta? Formatta помогает собрать документ."
        ]
        assert saved.nodes[1].data["items"] == [
            "Как начать работу? Загрузите Markdown и выберите смысловой шаблон FAQ."
        ]
        assert model.chat_evidence_text == (
            "Загрузите Markdown и выберите смысловой шаблон FAQ."
        )
        assert saved.nodes[1].data["items"][0].endswith(model.chat_evidence_text)
        assert saved.nodes[4].flags == ["manual-edit"]
        assert saved.nodes[4].provenance == []
        for expected, actual in zip(original.nodes, saved.nodes[:4], strict=True):
            assert actual.id == expected.id
            assert actual.section_id == expected.section_id
            assert actual.provenance == expected.provenance

        report = _stored_report(client, project_id)
        assert report.template_id == "faq"
        assert report.passed_rule_ids
        assert report.findings == []

    with TestClient(create_app(settings)) as restarted:
        editor = restarted.get(f"{project_url}/editor")
        assert editor.status_code == 200
        assert "FAQ по работе с Formatta" in editor.text
        assert "Загрузите Markdown и выберите смысловой шаблон FAQ" in editor.text
        assert "Проверено редактором" in editor.text

        workspace = restarted.get(project_url)
        assert workspace.status_code == 200
        assert 'id="project-workspace"' in workspace.text
        assert 'id="docgen2DocumentCanvas"' in workspace.text
        assert 'data-node-id="faq-start"' in workspace.text
        assert 'data-section-id="getting_started"' in workspace.text
        assert "Загрузите Markdown и выберите смысловой шаблон FAQ" in workspace.text
        assert "Проверено редактором" in workspace.text

        persisted = _stored_document(restarted, project_id)
        assert persisted.template_id == "faq"
        assert persisted.nodes[:4] == saved.nodes[:4]
        assert all(node.provenance for node in persisted.nodes[:4])
        assert persisted.nodes[4].flags == ["manual-edit"]
        assert persisted.nodes[4].provenance == []

        uploaded_check = restarted.post(
            f"{project_url}/jobs/check",
            data={"template_id": "faq", "target_source_id": source_id},
        )
        assert uploaded_check.status_code == 202
        uploaded_job_id = _job_id(uploaded_check.text)
        queued = _job(restarted, uploaded_job_id)
        assert queued.template_id == "faq"
        assert queued.target_source_id == source_id
        assert queued.check_target_kind is CheckTargetKind.SOURCE

        _run_check_worker(restarted, model)

        completed = _job(restarted, uploaded_job_id)
        assert completed.status is JobStatus.SUCCEEDED
        source_report = _stored_report(restarted, project_id)
        assert source_report.template_id == "faq"
        assert source_report.passed_rule_ids
        report_page = restarted.get(f"{project_url}/report")
        assert report_page.status_code == 200
        source_result = _stored_document(restarted, project_id)
        assert source_result.template_id == "faq"
        assert source_result.title == "Formatta"
        assert source_result.nodes
        assert all(node.provenance for node in source_result.nodes)
        document_page = restarted.get(f"{project_url}/document")
        assert document_page.status_code == 200
        assert "Formatta" in document_page.text


class _JourneyModel:
    def __init__(self) -> None:
        self.chat_evidence_id: str | None = None
        self.chat_evidence_text: str | None = None

    def generate_json(self, system: str, user: str, schema: type[Any]) -> Any:
        assert system
        payload = json.loads(user)
        if schema is ChatEditPlan:
            assert payload["document"]["template_id"] == "faq"
            source_blocks = payload["source_blocks"]
            assert source_blocks
            evidence = next(
                block
                for block in source_blocks
                if "Загрузите Markdown и выберите смысловой шаблон FAQ"
                in block["text"]
            )
            self.chat_evidence_id = evidence["id"]
            self.chat_evidence_text = evidence["text"]
            assert self.chat_evidence_text == (
                "Загрузите Markdown и выберите смысловой шаблон FAQ."
            )
            return ChatEditPlan(
                summary="Начало работы уточнено по источнику",
                operations=[
                    ChatEditOperation(
                        operation=UpdateData(
                            node_id="faq-start",
                            data={
                                "items": [
                                    "Как начать работу? " + self.chat_evidence_text
                                ]
                            },
                        ),
                        evidence_block_ids=[self.chat_evidence_id],
                    )
                ],
            )
        if schema is CheckReport:
            rule_ids = tuple(rule["id"] for rule in payload["правила"])
            assert payload["документ"]["template_id"] == "faq"
            return CheckReport(template_id="faq", passed_rule_ids=rule_ids)
        raise AssertionError(f"Unexpected schema: {schema}")


def _create_document(client: TestClient) -> tuple[str, str]:
    response = client.post(
        "/projects",
        data={"name": "FAQ Formatta"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    project_url = response.headers["location"]
    return project_url.rsplit("/", 1)[-1], project_url


def _upload_markdown(client: TestClient, project_url: str, project_id: str) -> str:
    response = client.post(
        f"{project_url}/sources/files",
        files={
            "file": (
                "formatta-faq-source.md",
                _FAQ_SOURCE.encode("utf-8"),
                "text/markdown",
            )
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    session = client.app.state.session_factory()
    try:
        sources = SourceRepository(session).list_for_project(project_id)
        assert len(sources) == 1
        return sources[0].id
    finally:
        session.close()


def _persist_generated_faq(client: TestClient, project_id: str) -> WorkingDocument:
    session = client.app.state.session_factory()
    try:
        blocks = _normalized_source_blocks(
            session, client.app.state.settings, project_id
        )
        facts = (
            (
                "faq-general",
                "general_questions",
                "Общие вопросы",
                "Что такое Formatta?",
                "Formatta помогает собрать документ из загруженных материалов.",
            ),
            (
                "faq-start",
                "getting_started",
                "Начало работы",
                "Как начать работу?",
                "Загрузите Markdown и выберите смысловой шаблон FAQ.",
            ),
            (
                "faq-work",
                "working_with_system",
                "Работа с системой",
                "Как собрать документ?",
                "Нажмите «Собрать» и дождитесь завершения задания worker.",
            ),
            (
                "faq-errors",
                "errors_and_limitations",
                "Ошибки и ограничения",
                "Что делать при ошибке?",
                "Если модель недоступна, проверьте настройку локальной text-модели.",
            ),
        )
        document = WorkingDocument(
            title="FAQ Formatta",
            template_id="faq",
            nodes=[
                _grounded_faq_node(
                    blocks=blocks,
                    node_id=node_id,
                    section_id=section_id,
                    title=title,
                    question=question,
                    fact=fact,
                )
                for node_id, section_id, title, question, fact in facts
            ],
        )
        DocumentRepository(session).save_document(project_id, document)
        session.commit()
        return document
    finally:
        session.close()


def _run_check_worker(client: TestClient, model: _JourneyModel) -> None:
    session = client.app.state.session_factory()
    try:
        normalization = NormalizationWorkflow(
            SourceRepository(session),
            LocalStorage(client.app.state.settings.data_dir),
            ExtractorRegistry.default(client.app.state.settings),
            ConfluenceClient.from_settings(client.app.state.settings),
        )
        shared = {
            "projects": ProjectRepository(session),
            "normalization": normalization,
            "templates": TemplateCatalog(),
            "text_model": model,
            "vision_model": _NoVisionModel(),
            "grounding": GroundingValidator(),
            "documents": DocumentRepository(session),
        }
        runner = JobRunner(
            JobRepository(session, worker_id="stage3-journey-worker"),
            {JobKind.CHECK: CheckWorkflow(**shared)},
        )
        assert runner.run_once() is True
    finally:
        session.close()


def _normalized_source_blocks(
    session: Session,
    settings: Settings,
    project_id: str,
) -> list[NormalizedBlock]:
    workflow = NormalizationWorkflow(
        SourceRepository(session),
        LocalStorage(settings.data_dir),
        ExtractorRegistry.default(settings),
        ConfluenceClient.from_settings(settings),
    )
    return workflow.run(project_id).blocks


def _source_block_with_text(
    blocks: list[NormalizedBlock], expected_text: str
) -> NormalizedBlock:
    matches = [block for block in blocks if block.text == expected_text]
    assert len(matches) == 1
    block = matches[0]
    assert block.provenance
    return block


def _grounded_faq_node(
    *,
    blocks: list[NormalizedBlock],
    node_id: str,
    section_id: str,
    title: str,
    question: str,
    fact: str,
) -> DocumentNode:
    block = _source_block_with_text(blocks, fact)
    return DocumentNode(
        id=node_id,
        kind=NodeKind.LIST,
        section_id=section_id,
        text=title,
        data={"items": [f"{question} {fact}"]},
        provenance=[
            Provenance(
                source_id=block.id,
                locator=block.provenance[0].locator,
                quote=block.text,
            )
        ],
        flags=["grounded"],
    )


def _stored_document(client: TestClient, project_id: str) -> WorkingDocument:
    session = client.app.state.session_factory()
    try:
        document = DocumentRepository(session).get_document(project_id)
        assert document is not None
        return document
    finally:
        session.close()


def _stored_report(client: TestClient, project_id: str) -> CheckReport:
    session = client.app.state.session_factory()
    try:
        report = DocumentRepository(session).get_report(project_id)
        assert report is not None
        return report
    finally:
        session.close()


def _job(client: TestClient, job_id: str):
    session = client.app.state.session_factory()
    try:
        job = JobRepository(session).get(job_id)
        assert job is not None
        session.expunge(job)
        return job
    finally:
        session.close()


def _job_id(response_text: str) -> str:
    match = re.search(r"/jobs/([^\" ]+)", response_text)
    assert match is not None
    return match.group(1)


class _NoVisionModel:
    def describe(self, image: bytes, media_type: str) -> object:
        del image, media_type
        raise AssertionError("Text-only journey must not invoke a vision model")


_FAQ_SOURCE = """# Formatta

Formatta помогает собрать документ из загруженных материалов.

## Начало работы

Загрузите Markdown и выберите смысловой шаблон FAQ.

## Сборка

Нажмите «Собрать» и дождитесь завершения задания worker.

## Ошибки

Если модель недоступна, проверьте настройку локальной text-модели.
"""
