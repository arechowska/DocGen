from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docgen.ai.grounding import GroundingValidator
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import (
    CheckFinding,
    CheckReport,
    DocumentNode,
    NodeKind,
    Severity,
    WorkingDocument,
)
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.jobs.models import Job, JobKind
from docgen.jobs.repository import JobRepository
from docgen.jobs.runner import JobRunner
from docgen.models import Project
from docgen.projects.repository import ProjectRepository
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.workflows.check import CheckWorkflow
from docgen.workflows.normalize import NormalizedProject


@pytest.fixture
def configured_models(client: TestClient) -> None:
    settings = client.app.state.settings
    settings.local_text_base_url = "http://text-model.test/v1"
    settings.local_text_model = "text-model"
    settings.local_vision_base_url = "http://vision-model.test/v1"
    settings.local_vision_model = "vision-model"


@pytest.fixture
def empty_project(client: TestClient) -> Project:
    return _create_project(client, "Пустой проект")


@pytest.fixture
def project_with_source(client: TestClient) -> Project:
    project = _create_project(client, "Проект со сценарием")
    response = client.post(
        f"/projects/{project.id}/sources/files",
        files={"file": ("case.md", b"# Case", "text/markdown")},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    return project


@pytest.fixture
def other_project(client: TestClient) -> Project:
    return _create_project(client, "Другой проект")


@pytest.fixture
def running_job(client: TestClient, project_with_source: Project) -> Job:
    with _session(client) as session:
        repository = JobRepository(session, worker_id="route-test-worker")
        queued_job = repository.enqueue(project_with_source.id, JobKind.ASSEMBLE, "use-case")
        claimed_job = repository.claim_next()
        assert claimed_job is not None
        assert claimed_job.id == queued_job.id
        return claimed_job


@pytest.fixture
def failed_job(client: TestClient, project_with_source: Project) -> Job:
    with _session(client) as session:
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(project_with_source.id, JobKind.ASSEMBLE, "use-case")
        claimed_job = repository.claim_next()
        assert claimed_job is not None
        return repository.mark_failed(
            job.id,
            "Traceback: token=secret raw source content must never be rendered",
        )


def test_start_assemble_enqueues_job(
    client: TestClient, configured_models: None, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/jobs/assemble",
        data={"template_id": "use-case"},
    )

    assert response.status_code == 202
    assert "Сборка поставлена в очередь" in response.text
    assert _jobs_for_project(client, project_with_source.id)[0].kind is JobKind.ASSEMBLE


def test_start_check_enqueues_job(
    client: TestClient, configured_models: None, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())

    response = client.post(
        f"/projects/{project_with_source.id}/jobs/check",
        data={"template_id": "use-case"},
    )

    assert response.status_code == 202
    assert "Проверка поставлена в очередь" in response.text
    assert _jobs_for_project(client, project_with_source.id)[0].kind is JobKind.CHECK


def test_missing_model_configuration_returns_503(
    client: TestClient, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/jobs/assemble",
        data={"template_id": "use-case"},
    )

    assert response.status_code == 503
    assert "Локальные модели не настроены" in response.text
    assert _jobs_for_project(client, project_with_source.id) == []


def test_missing_confluence_configuration_returns_503_before_enqueue(
    client: TestClient, configured_models: None
) -> None:
    project = _create_project(client, "Проект Confluence")
    response = client.post(
        f"/projects/{project.id}/sources/confluence",
        data={"url": "https://wiki.example.test/pages/42"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    response = client.post(
        f"/projects/{project.id}/jobs/assemble",
        data={"template_id": "use-case"},
    )

    assert response.status_code == 503
    assert "Интеграция Confluence не настроена" in response.text
    assert _jobs_for_project(client, project.id) == []


def test_empty_project_cannot_start(client: TestClient, empty_project: Project) -> None:
    response = client.post(
        f"/projects/{empty_project.id}/jobs/assemble",
        data={"template_id": "use-case"},
    )

    assert response.status_code == 422
    assert "Добавьте хотя бы один источник" in response.text
    assert 'id="generation-status"' in response.text
    assert 'id="generation-setup"' not in response.text


def test_invalid_template_is_rejected(
    client: TestClient, configured_models: None, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/jobs/assemble",
        data={"template_id": "missing"},
    )

    assert response.status_code == 422
    assert "Шаблон не найден" in response.text


def test_check_without_document_is_rejected_before_enqueue(
    client: TestClient, configured_models: None, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/jobs/check",
        data={"template_id": "use-case"},
    )

    assert response.status_code == 422
    assert "Документ для проверки не найден" in response.text
    assert _jobs_for_project(client, project_with_source.id) == []


def test_second_active_job_for_project_is_rejected_atomically(
    client: TestClient,
    configured_models: None,
    project_with_source: Project,
    running_job: Job,
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/jobs/assemble",
        data={"template_id": "use-case"},
    )

    assert response.status_code == 409
    assert "Проект уже обрабатывается" in response.text
    assert f'hx-get="/projects/{project_with_source.id}/jobs/{running_job.id}"' in response.text
    assert f'/projects/{project_with_source.id}/jobs/{running_job.id}/cancel' in response.text
    assert [job.id for job in _jobs_for_project(client, project_with_source.id)] == [running_job.id]


def test_running_status_polls_every_two_seconds(
    client: TestClient, running_job: Job
) -> None:
    response = client.get(f"/projects/{running_job.project_id}/jobs/{running_job.id}")

    assert response.status_code == 200
    assert f'hx-get="/projects/{running_job.project_id}/jobs/{running_job.id}"' in response.text
    assert 'hx-trigger="every 2s"' in response.text
    assert "Отменить" in response.text


def test_job_status_rejects_job_from_another_project(
    client: TestClient, running_job: Job, other_project: Project
) -> None:
    response = client.get(f"/projects/{other_project.id}/jobs/{running_job.id}")

    assert response.status_code == 404
    assert "Задание не найдено" in response.text


def test_cancel_requests_job_cancellation(client: TestClient, running_job: Job) -> None:
    response = client.post(
        f"/projects/{running_job.project_id}/jobs/{running_job.id}/cancel"
    )

    assert response.status_code == 200
    assert "Отмена запрошена" in response.text
    assert _job(client, running_job.id).cancel_requested is True


def test_cancel_rejects_job_from_another_project_without_changing_it(
    client: TestClient, running_job: Job, other_project: Project
) -> None:
    response = client.post(f"/projects/{other_project.id}/jobs/{running_job.id}/cancel")

    assert response.status_code == 404
    assert "Задание не найдено" in response.text
    assert _job(client, running_job.id).cancel_requested is False


def test_cancel_race_renders_completed_result_instead_of_cancellation_notice(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())
    with _session(client) as session:
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(project_with_source.id, JobKind.ASSEMBLE, "use-case")
        assert repository.claim_next() is not None
        repository.mark_succeeded(job.id)

    response = client.post(f"/projects/{project_with_source.id}/jobs/{job.id}/cancel")

    assert response.status_code == 200
    assert "Собранный документ" in response.text
    assert "Отмена запрошена" not in response.text


def test_failed_job_renders_only_user_safe_message(
    client: TestClient, failed_job: Job
) -> None:
    response = client.get(f"/projects/{failed_job.project_id}/jobs/{failed_job.id}")

    assert response.status_code == 200
    assert "Не удалось обработать источники" in response.text
    assert "Traceback" not in response.text
    assert "secret" not in response.text
    assert "raw source content" not in response.text
    assert "Повторить" in response.text


def test_cancelled_job_renders_retry_action(client: TestClient, running_job: Job) -> None:
    client.post(f"/projects/{running_job.project_id}/jobs/{running_job.id}/cancel")
    with _session(client) as session:
        JobRepository(session, worker_id="route-test-worker").mark_cancelled(running_job.id)

    response = client.get(f"/projects/{running_job.project_id}/jobs/{running_job.id}")

    assert response.status_code == 200
    assert "Повторить" in response.text
    assert f'/projects/{running_job.project_id}/jobs/assemble' in response.text


def test_document_view_renders_all_node_kinds_in_order_and_explicit_gap(
    client: TestClient, project_with_source: Project
) -> None:
    document = WorkingDocument(
        title="Платёжный сценарий",
        template_id="use-case",
        nodes=[
            DocumentNode(id="heading", kind=NodeKind.HEADING, text="Заголовок"),
            DocumentNode(id="paragraph", kind=NodeKind.PARAGRAPH, text="Абзац"),
            DocumentNode(
                id="list",
                kind=NodeKind.LIST,
                text="Список",
                data={"items": ["Первый", "Второй"]},
            ),
            DocumentNode(
                id="table",
                kind=NodeKind.TABLE,
                text="Таблица",
                data={"headers": ["Поле"], "rows": [["Значение"]]},
            ),
            DocumentNode(id="image", kind=NodeKind.IMAGE, text="Схема оплаты"),
            DocumentNode(id="gap", kind=NodeKind.GAP),
        ],
    )
    _save_document(client, project_with_source.id, document)

    response = client.get(f"/projects/{project_with_source.id}/document")

    assert response.status_code == 200
    assert "Нет данных в источниках" in response.text
    positions = [
        response.text.index(f'id="node-{node_id}"')
        for node_id in ("heading", "paragraph", "list", "table", "image", "gap")
    ]
    assert positions == sorted(positions)
    for visible_text in (
        "Заголовок",
        "Абзац",
        "Первый",
        "Второй",
        "Поле",
        "Значение",
        "Схема оплаты",
    ):
        assert visible_text in response.text


def test_document_view_escapes_saved_artifact_content(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(
        client,
        project_with_source.id,
        WorkingDocument(
            title="Безопасный документ",
            template_id="use-case",
            nodes=[
                DocumentNode(
                    id="unsafe",
                    kind=NodeKind.PARAGRAPH,
                    text='<script data-secret="token">alert(1)</script>',
                )
            ],
        ),
    )

    response = client.get(f"/projects/{project_with_source.id}/document")

    assert response.status_code == 200
    assert '<script data-secret="token">' not in response.text
    assert "&lt;script" in response.text


def test_report_view_groups_findings_and_links_to_document_nodes(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())
    report = CheckReport(
        template_id="use-case",
        findings=[
            CheckFinding(
                code="confirmed",
                severity=Severity.ERROR,
                confidence=0.9,
                message="Подтверждённая проблема",
                node_id="node-1",
                rule_id="structure-1",
            ),
            CheckFinding(
                code="uncertain",
                severity=Severity.WARNING,
                confidence=0.4,
                message="Замечание требует проверки",
                node_id="node-1",
                rule_id="style-5",
            ),
            CheckFinding(
                code="global",
                severity=Severity.INFO,
                confidence=0.8,
                message="Общее замечание",
                node_id=None,
                rule_id="completeness-2",
            ),
        ],
        unchecked_rules=["terminology-3"],
    )
    _save_report(client, project_with_source.id, report)

    response = client.get(f"/projects/{project_with_source.id}/report")

    assert response.status_code == 200
    assert "Подтверждённые проблемы" in response.text
    assert "Замечания с низкой уверенностью" in response.text
    assert "Непроверенные правила" in response.text
    assert "Подтверждённая проблема" in response.text
    assert "Замечание требует проверки" in response.text
    assert "terminology-3" in response.text
    assert (
        f'href="/projects/{project_with_source.id}/document#node-node-1"'
        in response.text
    )
    assert (
        f'href="/projects/{project_with_source.id}/document#document-start"'
        in response.text
    )


def test_succeeded_job_swaps_to_saved_document(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())
    with _session(client) as session:
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(project_with_source.id, JobKind.ASSEMBLE, "use-case")
        assert repository.claim_next() is not None
        succeeded_job = repository.mark_succeeded(job.id)

    response = client.get(
        f"/projects/{project_with_source.id}/jobs/{succeeded_job.id}"
    )

    assert response.status_code == 200
    assert "Собранный документ" in response.text
    assert "Оплата заказа" in response.text
    assert 'id="generation-status"' in response.text
    assert 'hx-trigger="every 2s"' not in response.text


def test_check_route_job_runs_once_and_swaps_to_saved_report(
    client: TestClient, configured_models: None, project_with_source: Project
) -> None:
    document = WorkingDocument(
        title="Оплата заказа",
        template_id="use-case",
        nodes=[
            DocumentNode(
                id="node-1",
                kind=NodeKind.PARAGRAPH,
                text="Пользователь оплачивает заказ",
                provenance=[Provenance(source_id="block-1", locator="paragraph:1")],
            )
        ],
    )
    _save_document(client, project_with_source.id, document)
    response = client.post(
        f"/projects/{project_with_source.id}/jobs/check",
        data={"template_id": "use-case"},
    )
    assert response.status_code == 202
    job = _jobs_for_project(client, project_with_source.id)[0]

    catalog = TemplateCatalog()
    unchecked_rules = [rule.id for rule in catalog.get("use-case").rules]
    with _session(client) as session:
        workflow = CheckWorkflow(
            projects=ProjectRepository(session),
            normalization=_StaticNormalization(),
            templates=catalog,
            text_model=_StaticCheckModel(unchecked_rules),
            vision_model=_NoImageVisionModel(),
            grounding=GroundingValidator(),
            documents=DocumentRepository(session),
        )
        runner = JobRunner(
            JobRepository(session, worker_id="route-check-worker"),
            {JobKind.CHECK: workflow},
        )
        assert runner.run_once() is True

    assert _job(client, job.id).status.value == "succeeded"
    response = client.get(f"/projects/{project_with_source.id}/jobs/{job.id}")
    assert response.status_code == 200
    assert "Результат проверки" in response.text
    assert "Непроверенные правила" in response.text
    assert 'id="generation-status"' in response.text
    assert unchecked_rules[0] in response.text


def test_missing_project_is_rejected_on_start(client: TestClient) -> None:
    response = client.post(
        "/projects/missing/jobs/assemble",
        data={"template_id": "use-case"},
    )

    assert response.status_code == 404
    assert "Проект не найден" in response.text


def _create_project(client: TestClient, name: str) -> Project:
    with _session(client) as session:
        project = ProjectRepository(session).create(name)
        session.commit()
        session.refresh(project)
        session.expunge(project)
        return project


def _document() -> WorkingDocument:
    return WorkingDocument(
        title="Оплата заказа",
        template_id="use-case",
        nodes=[
            DocumentNode(
                id="node-1",
                kind=NodeKind.PARAGRAPH,
                text="Пользователь оплачивает заказ",
            )
        ],
    )


def _save_document(client: TestClient, project_id: str, document: WorkingDocument) -> None:
    with _session(client) as session:
        DocumentRepository(session).save_document(project_id, document)
        session.commit()


def _save_report(client: TestClient, project_id: str, report: CheckReport) -> None:
    with _session(client) as session:
        DocumentRepository(session).save_report(project_id, report)
        session.commit()


def _jobs_for_project(client: TestClient, project_id: str) -> list[Job]:
    from sqlalchemy import select

    with _session(client) as session:
        return list(session.scalars(select(Job).where(Job.project_id == project_id)))


def _job(client: TestClient, job_id: str) -> Job:
    with _session(client) as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def _session(client: TestClient) -> Iterator:
    return client.app.state.session_factory()


class _StaticNormalization:
    def run(self, project_id: str, before_extract: Any = None) -> NormalizedProject:
        assert project_id
        assert before_extract is not None
        before_extract()
        return NormalizedProject(
            blocks=[
                NormalizedBlock(
                    id="block-1",
                    kind=BlockKind.TEXT,
                    text="Пользователь оплачивает заказ",
                    provenance=[
                        Provenance(source_id="source-1", locator="paragraph:1")
                    ],
                    confidence=1,
                )
            ],
            total_pages=1,
            warnings=[],
        )


class _StaticCheckModel:
    def __init__(self, unchecked_rules: list[str]) -> None:
        self._unchecked_rules = unchecked_rules

    def generate_json(self, system: str, user: str, schema: type[Any]) -> CheckReport:
        assert system
        assert user
        assert schema is CheckReport
        return CheckReport(template_id="use-case", unchecked_rules=self._unchecked_rules)


class _NoImageVisionModel:
    def describe(self, image: bytes, media_type: str) -> object:
        del image, media_type
        raise AssertionError("text-only check must not call the vision model")
