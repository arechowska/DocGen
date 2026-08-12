from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from docgen.ai.grounding import GroundingValidator
from docgen.chat.routes import _source_blocks_from_project
from docgen.chat.schemas import ChatEditPlan
from docgen.chat.service import ChatService
from docgen.config import Settings
from docgen.documents.operations import UpdateData
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import CheckReport, DocumentNode, NodeKind, WorkingDocument
from docgen.extraction.confluence import ConfluenceClient
from docgen.extraction.registry import ExtractorRegistry
from docgen.extraction.schemas import Provenance
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
        source_blocks=lambda project_id: _source_blocks_from_project(
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
            "Как начать работу? Загрузите Markdown и выберите FAQ."
        ]
        assert saved.nodes[4].flags == ["manual-edit"]
        assert saved.nodes[4].provenance == []
        for expected, actual in zip(original.nodes, saved.nodes[:4], strict=True):
            assert actual.id == expected.id
            assert actual.section_id == expected.section_id
            assert actual.provenance == expected.provenance

        report = _stored_report(client, project_id)
        assert report.template_id == "faq"
        assert set(report.passed_rule_ids) == set(_FAQ_RULE_IDS)

        uploaded_check = client.post(
            f"{project_url}/jobs/check",
            data={"template_id": "faq", "target_source_id": source_id},
        )
        assert uploaded_check.status_code == 202
        uploaded_job = _job(client, _job_id(uploaded_check.text))
        assert uploaded_job.template_id == "faq"
        assert uploaded_job.target_source_id == source_id
        assert uploaded_job.check_target_kind is CheckTargetKind.SOURCE


class _JourneyModel:
    def __init__(self) -> None:
        self.chat_evidence_id: str | None = None

    def generate_json(self, system: str, user: str, schema: type[Any]) -> Any:
        assert system
        payload = json.loads(user)
        if schema is ChatEditPlan:
            assert payload["document"]["template_id"] == "faq"
            source_blocks = payload["source_blocks"]
            assert source_blocks
            self.chat_evidence_id = source_blocks[0]["id"]
            return ChatEditPlan(
                summary="Начало работы уточнено по источнику",
                operations=[
                    UpdateData(
                        node_id="faq-start",
                        data={
                            "items": [
                                "Как начать работу? Загрузите Markdown и выберите FAQ."
                            ]
                        },
                    )
                ],
                evidence_block_ids=[self.chat_evidence_id],
            )
        if schema is CheckReport:
            rule_ids = tuple(rule["id"] for rule in payload["правила"])
            assert rule_ids == _FAQ_RULE_IDS
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
        blocks = _source_blocks_from_project(session, client.app.state.settings, project_id)
        assert len(blocks) >= 4
        document = WorkingDocument(
            title="FAQ Formatta",
            template_id="faq",
            nodes=[
                DocumentNode(
                    id=node_id,
                    kind=NodeKind.LIST,
                    section_id=section_id,
                    text=title,
                    data={"items": [item]},
                    provenance=[
                        Provenance(
                            source_id=block.id,
                            locator=block.provenance[0].locator,
                            quote=block.text,
                        )
                    ],
                    flags=["grounded"],
                )
                for node_id, section_id, title, item, block in zip(
                    ("faq-general", "faq-start", "faq-work", "faq-errors"),
                    (
                        "general_questions",
                        "getting_started",
                        "working_with_system",
                        "errors_and_limitations",
                    ),
                    (
                        "Общие вопросы",
                        "Начало работы",
                        "Работа с системой",
                        "Ошибки и ограничения",
                    ),
                    (
                        "Что такое Formatta?",
                        "Как начать работу?",
                        "Как собрать документ?",
                        "Что делать при ошибке?",
                    ),
                    blocks[:4],
                    strict=True,
                )
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


_FAQ_RULE_IDS = (
    "faq-structure",
    "faq-procedure-structure",
    "faq-completeness",
    "faq-grounding",
    "faq-terminology",
    "faq-contradiction",
    "faq-style",
    "faq-source-visibility",
)

_FAQ_SOURCE = """# Formatta

Formatta помогает собрать документ из загруженных материалов.

## Начало работы

Загрузите Markdown и выберите смысловой шаблон FAQ.

## Сборка

Нажмите «Собрать» и дождитесь завершения задания worker.

## Ошибки

Если модель недоступна, проверьте настройку локальной text-модели.
"""
