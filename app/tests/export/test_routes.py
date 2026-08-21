from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import DocumentNode, NodeKind, WorkingDocument
from docgen.export.protocol import RenderedFile
from docgen.export.service import ExportResult
from docgen.export.storage import ExportStorage
from docgen.formatting.schemas import OutputFormat
from docgen.jobs.models import Job, JobKind
from docgen.jobs.repository import JobRepository
from docgen.projects.models import Project
from docgen.projects.repository import ProjectRepository

_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def formatting_catalog_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "formatting-catalog"
    directory.mkdir()
    # Names must be >=50% Cyrillic per FormattingTemplate.validate_name, so
    # "DocGen Light" (the brief's illustrative example) is adapted to a
    # mixed name that still reads as "DocGen" while satisfying that rule.
    (directory / "docgen-light-docx.yaml").write_text(
        "id: docgen-light\nname: Лёгкий DocGen\nformat: docx\nrenderer: docx\n",
        encoding="utf-8",
    )
    (directory / "docgen-light-html.yaml").write_text(
        "id: docgen-light\nname: Облегченный HTML\nformat: html\nrenderer: html\n",
        encoding="utf-8",
    )
    (directory / "docgen-light-markdown.yaml").write_text(
        "id: docgen-light\nname: Облегченный Markdown\nformat: markdown\nrenderer: markdown\n",
        encoding="utf-8",
    )
    return directory


@pytest.fixture(autouse=True)
def configured_formatting_catalog(client: TestClient, formatting_catalog_dir: Path) -> None:
    client.app.state.settings.formatting_template_dir = formatting_catalog_dir


@pytest.fixture
def project_with_document(client: TestClient) -> Project:
    project = _create_project(client, "Проект с документом")
    _save_document(client, project.id, _document())
    return project


@pytest.fixture
def other_project(client: TestClient) -> Project:
    return _create_project(client, "Другой проект")


@pytest.fixture
def completed_export_job(client: TestClient, project_with_document: Project) -> Job:
    return _make_export_job(client, project_with_document.id, state="succeeded")


# --- format/template selection -------------------------------------------------


def test_format_selection_returns_only_matching_templates(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.get(f"/projects/{project_with_document.id}/export/templates?format=docx")

    assert response.status_code == 200
    assert "Лёгкий DocGen" in response.text
    assert 'value="docgen-light"' in response.text
    assert 'hx-include="#export-form"' in response.text
    assert "docgen:document-updated from:body" in response.text
    assert "Облегченный HTML" not in response.text


def test_format_selection_rejects_invalid_format(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.get(f"/projects/{project_with_document.id}/export/templates?format=bogus")

    assert response.status_code == 422


def test_format_selection_unknown_project_returns_404(client: TestClient) -> None:
    response = client.get("/projects/missing/export/templates?format=docx")

    assert response.status_code == 404


# --- starting an export ---------------------------------------------------------


def test_start_export_enqueues_job(client: TestClient, project_with_document: Project) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/export",
        data={"format": "docx", "template_id": "docgen-light", "revision": 1},
    )

    assert response.status_code == 202
    job = _jobs_for_project(client, project_with_document.id)[0]
    assert job.kind is JobKind.EXPORT
    assert job.export_format is OutputFormat.DOCX
    assert job.template_id == "docgen-light"


def test_toolbar_edit_then_export_succeeds_with_updated_revision(
    client: TestClient, project_with_document: Project
) -> None:
    """A direct editor-toolbar edit (not a chat edit) must also refresh the
    export panel's hidden revision field, via the same `docgen:document
    -updated` HX-Trigger chat/routes.py already sets. Before the fix,
    editor/routes.py's toolbar mutation endpoints didn't set this header, so
    the export form kept the pre-edit revision and the very next export
    attempt would incorrectly 409 as "stale" even though the user had just
    made an ordinary edit.
    """
    edit_response = client.patch(
        f"/projects/{project_with_document.id}/editor/nodes/node-1/text",
        data={"text": "Обновлённый текст", "revision": "1"},
    )
    assert edit_response.status_code == 200
    trigger = json.loads(edit_response.headers["hx-trigger"])
    updated_revision = trigger["docgen:document-updated"]["revision"]
    assert updated_revision == 2

    export_response = client.post(
        f"/projects/{project_with_document.id}/export",
        data={"format": "docx", "template_id": "docgen-light", "revision": updated_revision},
    )

    assert export_response.status_code == 202


def test_start_export_for_unknown_project_returns_404(client: TestClient) -> None:
    response = client.post(
        "/projects/missing/export",
        data={"format": "docx", "template_id": "docgen-light", "revision": 1},
    )

    assert response.status_code == 404


def test_start_export_rejects_invalid_format_template_pair(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/export",
        data={"format": "docx", "template_id": "no-such-template", "revision": 1},
    )

    assert response.status_code == 422
    assert _jobs_for_project(client, project_with_document.id) == []


def test_start_export_rejects_unknown_format_value(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/export",
        data={"format": "bogus", "template_id": "docgen-light", "revision": 1},
    )

    assert response.status_code == 422
    assert _jobs_for_project(client, project_with_document.id) == []


def test_start_export_rejects_stale_revision(
    client: TestClient, project_with_document: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_document.id}/export",
        data={"format": "docx", "template_id": "docgen-light", "revision": 0},
    )

    assert response.status_code == 409
    assert _jobs_for_project(client, project_with_document.id) == []


def test_start_export_requires_a_saved_document(
    client: TestClient, other_project: Project
) -> None:
    response = client.post(
        f"/projects/{other_project.id}/export",
        data={"format": "docx", "template_id": "docgen-light", "revision": 1},
    )

    assert response.status_code == 422
    assert _jobs_for_project(client, other_project.id) == []


def test_start_export_rejects_when_project_already_busy(
    client: TestClient, project_with_document: Project
) -> None:
    _make_export_job(client, project_with_document.id, state="queued")

    response = client.post(
        f"/projects/{project_with_document.id}/export",
        data={"format": "docx", "template_id": "docgen-light", "revision": 1},
    )

    assert response.status_code == 409
    assert len(_jobs_for_project(client, project_with_document.id)) == 1


# --- polling status --------------------------------------------------------------


def test_export_status_reports_active_job(
    client: TestClient, project_with_document: Project
) -> None:
    job = _make_export_job(client, project_with_document.id, state="running")

    response = client.get(f"/projects/{project_with_document.id}/exports/{job.id}/status")

    assert response.status_code == 200
    assert "Формируется" in response.text
    assert f"/projects/{project_with_document.id}/exports/{job.id}/status" in response.text


def test_export_status_reports_success_with_download_link(
    client: TestClient, project_with_document: Project, completed_export_job: Job
) -> None:
    response = client.get(
        f"/projects/{project_with_document.id}/exports/{completed_export_job.id}/status"
    )

    assert response.status_code == 200
    assert f"/exports/{completed_export_job.id}/download" in response.text
    assert f"/exports/{completed_export_job.id}/open" not in response.text


def test_export_status_reports_html_open_link(
    client: TestClient, project_with_document: Project
) -> None:
    job = _make_export_job(
        client,
        project_with_document.id,
        state="succeeded",
        export_format=OutputFormat.HTML,
        filename="document.html",
    )

    response = client.get(f"/projects/{project_with_document.id}/exports/{job.id}/status")

    assert response.status_code == 200
    assert f"/exports/{job.id}/open" in response.text
    assert f"/exports/{job.id}/download" not in response.text


def test_export_status_reports_failure(
    client: TestClient, project_with_document: Project
) -> None:
    job = _make_export_job(client, project_with_document.id, state="failed")

    response = client.get(f"/projects/{project_with_document.id}/exports/{job.id}/status")

    assert response.status_code == 200
    assert "Не удалось выполнить экспорт" in response.text


def test_export_status_for_another_projects_job_returns_404(
    client: TestClient, project_with_document: Project, other_project: Project
) -> None:
    job = _make_export_job(client, project_with_document.id, state="running")

    response = client.get(f"/projects/{other_project.id}/exports/{job.id}/status")

    assert response.status_code == 404


def test_export_status_rejects_non_export_job(
    client: TestClient, project_with_document: Project
) -> None:
    with _session(client) as session:
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(project_with_document.id, JobKind.ASSEMBLE, "use-case")

    response = client.get(f"/projects/{project_with_document.id}/exports/{job.id}/status")

    assert response.status_code == 404


# --- downloading -------------------------------------------------------------------


def test_download_has_safe_headers(
    client: TestClient, project_with_document: Project, completed_export_job: Job
) -> None:
    response = client.get(
        f"/projects/{project_with_document.id}/exports/{completed_export_job.id}/download"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content == b"PK\x03\x04fake-docx-bytes"


def test_download_pending_job_returns_409(
    client: TestClient, project_with_document: Project
) -> None:
    job = _make_export_job(client, project_with_document.id, state="running")

    response = client.get(f"/projects/{project_with_document.id}/exports/{job.id}/download")

    assert response.status_code == 409


def test_download_failed_job_returns_409(
    client: TestClient, project_with_document: Project
) -> None:
    job = _make_export_job(client, project_with_document.id, state="failed")

    response = client.get(f"/projects/{project_with_document.id}/exports/{job.id}/download")

    assert response.status_code == 409


def test_download_another_projects_job_returns_404(
    client: TestClient, project_with_document: Project, other_project: Project
) -> None:
    job = _make_export_job(client, project_with_document.id, state="succeeded")

    response = client.get(f"/projects/{other_project.id}/exports/{job.id}/download")

    assert response.status_code == 404


def test_download_missing_file_returns_410(
    client: TestClient, project_with_document: Project
) -> None:
    job = _make_export_job(client, project_with_document.id, state="succeeded", write_file=False)

    response = client.get(f"/projects/{project_with_document.id}/exports/{job.id}/download")

    assert response.status_code == 410


def test_download_encodes_cyrillic_filenames_per_rfc5987(
    client: TestClient, project_with_document: Project
) -> None:
    job = _make_export_job(
        client, project_with_document.id, state="succeeded", filename="Отчёт.docx"
    )

    response = client.get(f"/projects/{project_with_document.id}/exports/{job.id}/download")

    assert response.status_code == 200
    assert "filename*=utf-8''" in response.headers["content-disposition"]


def test_open_html_export_returns_the_saved_html_inline(
    client: TestClient, project_with_document: Project
) -> None:
    job = _make_export_job(
        client,
        project_with_document.id,
        state="succeeded",
        export_format=OutputFormat.HTML,
        filename="Документ.html",
    )

    response = client.get(f"/projects/{project_with_document.id}/exports/{job.id}/open")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "inline" in response.headers["content-disposition"]
    assert response.content == b"<html><body>saved export</body></html>"


def test_open_endpoint_rejects_non_html_export(
    client: TestClient, project_with_document: Project, completed_export_job: Job
) -> None:
    response = client.get(
        f"/projects/{project_with_document.id}/exports/{completed_export_job.id}/open"
    )

    assert response.status_code == 404


# --- fixtures/helpers ----------------------------------------------------------------


def _document() -> WorkingDocument:
    return WorkingDocument(
        title="Документ проекта",
        template_id="use-case",
        nodes=[DocumentNode(id="node-1", kind=NodeKind.PARAGRAPH, text="Текст документа")],
    )


def _create_project(client: TestClient, name: str) -> Project:
    with _session(client) as session:
        project = ProjectRepository(session).create(name)
        session.commit()
        session.refresh(project)
        session.expunge(project)
        return project


def _save_document(client: TestClient, project_id: str, document: WorkingDocument) -> None:
    with _session(client) as session:
        DocumentRepository(session).save_document(project_id, document)
        session.commit()


def _jobs_for_project(client: TestClient, project_id: str) -> list[Job]:
    from sqlalchemy import select

    with _session(client) as session:
        return list(session.scalars(select(Job).where(Job.project_id == project_id)))


def _make_export_job(
    client: TestClient,
    project_id: str,
    *,
    state: str,
    write_file: bool = True,
    filename: str = "document.docx",
    export_format: OutputFormat = OutputFormat.DOCX,
) -> Job:
    with _session(client) as session:
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(
            project_id,
            JobKind.EXPORT,
            "docgen-light",
            export_format=export_format,
            requested_document_revision=1,
        )
        if state == "queued":
            session.expunge(job)
            return job

        claimed = repository.claim_next()
        assert claimed is not None

        if state == "running":
            # claim_next() claims via its own internal Session and already
            # returns a detached instance, so there's nothing to expunge.
            return claimed
        if state == "failed":
            failed = repository.mark_failed(
                claimed.id,
                "Traceback: token=secret raw source content must never be rendered",
                user_message="Не удалось выполнить экспорт",
            )
            session.expunge(failed)
            return failed
        if state == "succeeded":
            storage = ExportStorage(client.app.state.settings.data_dir)
            content = (
                b"<html><body>saved export</body></html>"
                if export_format is OutputFormat.HTML
                else b"PK\x03\x04fake-docx-bytes"
            )
            media_type = (
                "text/html; charset=utf-8"
                if export_format is OutputFormat.HTML
                else _DOCX_MEDIA_TYPE
            )
            rendered = RenderedFile(filename=filename, media_type=media_type, content=content)
            stored = storage.save(project_id, export_format, "docgen-light", rendered)
            if not write_file:
                storage.resolve(stored.relative_path).unlink()
            result = ExportResult(
                relative_path=stored.relative_path,
                filename=stored.filename,
                media_type=rendered.media_type,
                size_bytes=stored.size_bytes,
                document_revision=1,
            )
            repository.record_export_result(claimed.id, result)
            succeeded = repository.mark_succeeded(claimed.id)
            session.expunge(succeeded)
            return succeeded
        raise ValueError(f"Unsupported job state: {state}")


def _session(client: TestClient) -> Iterator:
    return client.app.state.session_factory()
