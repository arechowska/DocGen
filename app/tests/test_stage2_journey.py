from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docgen.ai.grounding import GroundingValidator
from docgen.config import Settings
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import (
    CheckReport,
    DocumentNode,
    NodeKind,
    WorkingDocument,
)
from docgen.extraction.registry import ExtractorRegistry
from docgen.extraction.schemas import Provenance
from docgen.jobs.models import JobKind, JobStatus
from docgen.jobs.repository import JobRepository
from docgen.jobs.runner import JobRunner
from docgen.main import create_app
from docgen.projects.repository import ProjectRepository
from docgen.sources.repository import SourceRepository
from docgen.sources.storage import LocalStorage
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.workflows.assemble import AssembleWorkflow
from docgen.workflows.check import CheckWorkflow
from docgen.workflows.normalize import NormalizationWorkflow


@pytest.fixture
def stage2_app_factory(tmp_path: Path) -> Callable[[], Iterator[TestClient]]:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'stage2.db'}",
        data_dir=tmp_path / "data",
        confluence_hosts=("wiki.example.test",),
        local_text_base_url="http://offline-text-model.test/v1",
        local_text_model="deterministic-text-model",
        local_vision_base_url="http://offline-vision-model.test/v1",
        local_vision_model="deterministic-vision-model",
        trusted_integration_hosts=(
            "offline-text-model.test",
            "offline-vision-model.test",
        ),
    )

    @contextmanager
    def start_app() -> Iterator[TestClient]:
        with TestClient(create_app(settings)) as client:
            yield client

    return start_app


def test_stage2_journey_survives_restart_and_supports_cancelled_retry(
    stage2_app_factory: Callable[[], Iterator[TestClient]],
) -> None:
    """Catch route/worker/artifact drift across standalone, assembly, retry, and restart."""
    model = _DeterministicStage2Model()
    with stage2_app_factory() as first_client:
        created = first_client.post(
            "/projects",
            data={"name": "Синтетический перевод между счетами"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        project_url = created.headers["location"]
        project_id = project_url.rsplit("/", 1)[-1]

        upload = first_client.post(
            f"{project_url}/sources/files",
            files={
                "file": (
                    "synthetic-case.md",
                    (
                        _SYNTHETIC_SOURCE + "\n\n" + ("x" * 181_800)
                    ).encode("utf-8"),
                    "text/markdown",
                )
            },
            headers={"HX-Request": "true"},
        )
        assert upload.status_code == 200
        target_source_id = _source_id(first_client, project_id)

        standalone_start = first_client.post(
            f"{project_url}/jobs/check",
            data={"template_id": "use-case", "target_source_id": target_source_id},
        )
        assert standalone_start.status_code == 202
        standalone_job_id = _job_id(standalone_start.text)
        _run_one_worker_iteration(first_client, model)
        assert _job_status(first_client, standalone_job_id) is JobStatus.SUCCEEDED
        assert "Обработка может занять более пяти минут" in first_client.get(
            f"{project_url}/jobs/{standalone_job_id}"
        ).text
        assert first_client.get(f"{project_url}/report").status_code == 200
        with _session(first_client) as session:
            standalone_document = DocumentRepository(session).get_document(project_id)
            standalone_report = DocumentRepository(session).get_report(project_id)
        assert standalone_document is not None
        assert standalone_document.nodes
        assert standalone_report is not None

        assemble_start = first_client.post(
            f"{project_url}/jobs/assemble", data={"template_id": "use-case"}
        )
        assert assemble_start.status_code == 202
        assemble_job_id = _job_id(assemble_start.text)
        _run_one_worker_iteration(first_client, model)
        assert _job_status(first_client, assemble_job_id) is JobStatus.SUCCEEDED
        document_response = first_client.get(f"{project_url}/document")
        assert document_response.status_code == 200
        assert "Перевод между своими счетами" in document_response.text
        assert first_client.get(f"{project_url}/report").status_code == 404

        check_start = first_client.post(
            f"{project_url}/jobs/check", data={"template_id": "use-case"}
        )
        assert check_start.status_code == 202
        check_job_id = _job_id(check_start.text)
        _run_one_worker_iteration(first_client, model)
        assert _job_status(first_client, check_job_id) is JobStatus.SUCCEEDED
        assert first_client.get(f"{project_url}/report").status_code == 200

        cancelled_start = first_client.post(
            f"{project_url}/jobs/assemble", data={"template_id": "use-case"}
        )
        cancelled_job_id = _job_id(cancelled_start.text)
        cancel_response = first_client.post(
            f"{project_url}/jobs/{cancelled_job_id}/cancel"
        )
        assert cancel_response.status_code == 200
        assert _job_status(first_client, cancelled_job_id) is JobStatus.CANCELLED
        retry_form = first_client.get(f"{project_url}/jobs/{cancelled_job_id}")
        assert "Повторить" in retry_form.text

        retry_start = first_client.post(
            f"{project_url}/jobs/assemble", data={"template_id": "use-case"}
        )
        assert retry_start.status_code == 202
        retry_job_id = _job_id(retry_start.text)
        _run_one_worker_iteration(first_client, model)
        assert _job_status(first_client, retry_job_id) is JobStatus.SUCCEEDED
        assert first_client.get(f"{project_url}/report").status_code == 404

        final_check = first_client.post(
            f"{project_url}/jobs/check", data={"template_id": "use-case"}
        )
        assert final_check.status_code == 202
        final_check_job_id = _job_id(final_check.text)
        _run_one_worker_iteration(first_client, model)
        assert _job_status(first_client, final_check_job_id) is JobStatus.SUCCEEDED

    with stage2_app_factory() as restarted_client:
        project_page = restarted_client.get(project_url)
        saved_document = restarted_client.get(f"{project_url}/document")
        saved_report = restarted_client.get(f"{project_url}/report")
        restarted_job = restarted_client.get(
            f"{project_url}/jobs/{final_check_job_id}"
        )

    assert project_page.status_code == 200
    assert "Перевод между своими счетами" in project_page.text
    assert "synthetic-case.md" in project_page.text
    assert saved_document.status_code == 200
    assert "Перевод между своими счетами" in saved_document.text
    assert saved_report.status_code == 200
    assert "Результат проверки" in saved_report.text
    assert "Обработка может занять более пяти минут" in restarted_job.text


def _run_one_worker_iteration(client: TestClient, model: _DeterministicStage2Model) -> None:
    with _session(client) as session:
        normalization = NormalizationWorkflow(
            SourceRepository(session),
            LocalStorage(client.app.state.settings.data_dir),
            ExtractorRegistry.default(),
            _NoConfluenceClient(),
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
            JobRepository(session, worker_id="stage2-acceptance-worker"),
            {
                JobKind.ASSEMBLE: AssembleWorkflow(**shared),
                JobKind.CHECK: CheckWorkflow(**shared),
            },
        )
        assert runner.run_once() is True


def _source_id(client: TestClient, project_id: str) -> str:
    with _session(client) as session:
        sources = SourceRepository(session).list_for_project(project_id)
        assert len(sources) == 1
        return sources[0].id


def _job_id(response_text: str) -> str:
    match = re.search(r"/jobs/([^\" ]+)", response_text)
    assert match is not None
    return match.group(1)


def _job_status(client: TestClient, job_id: str) -> JobStatus:
    with _session(client) as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        return job.status


def _session(client: TestClient) -> Iterator[Any]:
    return client.app.state.session_factory()


class _DeterministicStage2Model:
    def generate_json(self, system: str, user: str, schema: type[Any]) -> Any:
        assert system
        payload = json.loads(user)
        if schema is CheckReport:
            rule_ids = tuple(rule["id"] for rule in payload["правила"])
            assert set(rule_ids) == {
                "use-case-structure",
                "use-case-completeness",
                "use-case-terminology",
                "use-case-contradiction",
                "use-case-style",
            }
            return CheckReport(
                template_id="use-case",
                passed_rule_ids=rule_ids,
            )
        assert schema is WorkingDocument
        nodes: list[DocumentNode] = []
        blocks_by_text = {
            block["text"].strip().casefold(): block
            for block in payload["исходные_блоки"]
            if block["text"].strip()
        }
        for index, section in enumerate(payload["шаблон"]["sections"], start=1):
            block = blocks_by_text[section["title"].casefold()]
            nodes.append(
                DocumentNode(
                    id=f"assembled-node-{index}",
                    kind=NodeKind.HEADING,
                    section_id=section["id"],
                    text=block["text"],
                    provenance=[
                        Provenance(
                            source_id=block["id"],
                            locator=block["locators"][0],
                            quote=block["text"],
                        )
                    ],
                )
            )
        return WorkingDocument(
            title="Перевод между своими счетами",
            template_id="use-case",
            nodes=nodes,
        )


class _NoVisionModel:
    def describe(self, image: bytes, media_type: str) -> object:
        del image, media_type
        raise AssertionError("Text-only acceptance must not invoke a vision model")


class _NoConfluenceClient:
    def fetch(self, url: str, *, before_external_call: Any = None) -> object:
        del url, before_external_call
        raise AssertionError("File-only acceptance must not invoke Confluence")


_SYNTHETIC_SOURCE = """# Перевод между своими счетами

## Участники

- Клиент мобильного банка
- Система дистанционного банковского обслуживания

## Предусловия

Клиент вошёл в мобильный банк, а оба счёта доступны для операций.

## Основной поток

1. Клиент выбирает счета и вводит сумму перевода.
2. Система проверяет остаток и выполняет операцию.

## Результат

Система показывает статус операции, а перевод появляется в истории клиента.
"""
