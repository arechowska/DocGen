from __future__ import annotations

from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from docgen.ai.grounding import GroundingValidator
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import (
    CheckFinding,
    CheckReport,
    DocumentNode,
    DocumentOrigin,
    NodeKind,
    Severity,
    WorkingDocument,
)
from docgen.export.protocol import RenderedFile
from docgen.export.service import ExportResult
from docgen.export.storage import ExportStorage
from docgen.extraction.registry import ExtractionResult, ExtractorRegistry
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance
from docgen.formatting.schemas import OutputFormat
from docgen.jobs.models import Job, JobKind
from docgen.jobs.repository import JobRepository
from docgen.jobs.runner import JobRunner
from docgen.models import Project
from docgen.projects.repository import ProjectRepository
from docgen.sources.repository import SourceRepository
from docgen.sources.storage import LocalStorage
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.workflows.check import CheckWorkflow
from docgen.workflows.normalize import NormalizationWorkflow


@pytest.fixture
def configured_models(client: TestClient) -> None:
    settings = client.app.state.settings
    settings.local_text_base_url = "http://text-model.test/v1"
    settings.local_text_model = "text-model"
    settings.local_vision_base_url = "http://vision-model.test/v1"
    settings.local_vision_model = "vision-model"
    settings.trusted_integration_hosts = ("text-model.test", "vision-model.test")


@pytest.fixture
def configured_text_model(client: TestClient) -> None:
    settings = client.app.state.settings
    settings.local_text_base_url = "http://text-model.test/v1"
    settings.local_text_model = "text-model"
    settings.trusted_integration_hosts = ("text-model.test",)


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


def test_start_assemble_without_template_does_not_create_editor_document(
    client: TestClient, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/jobs/assemble",
        data={"template_id": "no-template"},
    )

    assert response.status_code == 422
    assert "конвертаци" in response.text
    assert _jobs_for_project(client, project_with_source.id) == []
    with _session(client) as session:
        assert DocumentRepository(session).get_document(project_with_source.id) is None


def test_template_free_html_conversion_saves_export_and_opens_inline_without_changing_editor(
    client: TestClient, project_with_source: Project
) -> None:
    original = _document()
    _save_document(client, project_with_source.id, original)

    response = client.post(
        f"/projects/{project_with_source.id}/convert",
        data={"output_format": "html", "formatting_template_id": "docgen-light"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-disposition"].startswith("inline;")
    assert b"Case" in response.content
    export_directory = (
        client.app.state.settings.data_dir
        / "projects"
        / project_with_source.id
        / "exports"
    )
    saved_exports = list(export_directory.glob("*.html"))
    assert len(saved_exports) == 1
    assert saved_exports[0].read_bytes() == response.content
    with _session(client) as session:
        assert DocumentRepository(session).get_document_with_revision(
            project_with_source.id
        ) == (original, 1)


def test_template_free_html_conversion_updates_result_and_survives_reload(
    client: TestClient, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/convert",
        data={"output_format": "html", "formatting_template_id": "docgen-light"},
        headers={"HX-Request": "true", "Accept": "text/html"},
    )

    assert response.status_code == 200
    result = BeautifulSoup(response.text, "html.parser")
    assert result.find(id="resultStatus").get_text(strip=True) == "Готово"
    open_link = result.find("a", string="Открыть")
    assert open_link is not None
    assert open_link.get("target") == "_blank"

    opened = client.get(open_link["href"])
    assert opened.status_code == 200
    assert opened.headers["content-type"].startswith("text/html")
    assert opened.headers["content-disposition"].startswith("inline;")
    assert b"Case" in opened.content
    assert b"<style>" in opened.content
    assert b"font-family: Inter, system-ui, sans-serif" in opened.content

    reloaded = client.get(f"/projects/{project_with_source.id}")
    reloaded_page = BeautifulSoup(reloaded.text, "html.parser")
    reloaded_open_link = reloaded_page.find(id="resultPanel").find(
        "a", string="Открыть"
    )
    assert reloaded_open_link is not None
    assert reloaded_open_link["href"] == open_link["href"]
    with _session(client) as session:
        assert DocumentRepository(session).get_document(project_with_source.id) is None
    assert _jobs_for_project(client, project_with_source.id) == []


def test_template_free_html_result_survives_reload_with_legacy_editor_document(
    client: TestClient, project_with_source: Project
) -> None:
    with _session(client) as session:
        DocumentRepository(session).save_document(
            project_with_source.id,
            WorkingDocument(title="Legacy", template_id="no-template", nodes=[]),
        )
        session.commit()

    response = client.post(
        f"/projects/{project_with_source.id}/convert",
        data={"output_format": "html", "formatting_template_id": "docgen-light"},
        headers={"HX-Request": "true", "Accept": "text/html"},
    )
    assert response.status_code == 200

    reloaded = client.get(f"/projects/{project_with_source.id}")
    result = BeautifulSoup(reloaded.text, "html.parser").find(id="resultPanel")

    assert result is not None
    assert result.find("a", string="Открыть") is not None
    assert result.find("a", string="Скачать") is None
    assert _jobs_for_project(client, project_with_source.id) == []


def test_saved_legacy_conversion_is_not_replaced_by_editor_html_export_on_reload(
    client: TestClient, project_with_source: Project
) -> None:
    settings = client.app.state.settings
    storage = ExportStorage(settings.data_dir)
    saved_conversion = storage.save(
        project_with_source.id,
        OutputFormat.HTML,
        "docgen-light",
        RenderedFile(
            filename="source.html",
            media_type="text/html",
            content=b"<html><body>saved source version</body></html>",
        ),
    )
    with _session(client) as session:
        DocumentRepository(session).save_document(
            project_with_source.id,
            WorkingDocument(title="html 2", template_id="no-template", nodes=[]),
        )
        session.commit()

        jobs = JobRepository(session, worker_id="route-test-worker")
        jobs.enqueue(
            project_with_source.id,
            JobKind.EXPORT,
            "docgen-light",
            export_format=OutputFormat.HTML,
            requested_document_revision=1,
        )
        claimed = jobs.claim_next()
        assert claimed is not None
        rebuilt = storage.save(
            project_with_source.id,
            OutputFormat.HTML,
            "docgen-light",
            RenderedFile(
                filename="html-2.html",
                media_type="text/html",
                content=b"<html><body>rebuilt editor placeholder</body></html>",
            ),
        )
        jobs.record_export_result(
            claimed.id,
            ExportResult(
                relative_path=rebuilt.relative_path,
                filename=rebuilt.filename,
                media_type="text/html",
                size_bytes=rebuilt.size_bytes,
                document_revision=1,
            ),
        )
        jobs.mark_succeeded(claimed.id)

    response = client.get(f"/projects/{project_with_source.id}")
    page = BeautifulSoup(response.text, "html.parser")
    result = page.find(id="resultPanel")
    selected_format = page.find(id="formatSelect").find("option", selected=True)

    assert response.status_code == 200
    assert result.find(id="resultFilename").get_text(strip=True) == saved_conversion.filename
    assert result.find(id="conversion-result-actions") is not None
    assert result.find(id="export-result") is None
    assert selected_format["value"] == "html"
    opened = client.get(result.find("a", string="Открыть")["href"])
    assert opened.content == b"<html><body>saved source version</body></html>"


def test_saved_conversion_does_not_replace_semantic_document_result_on_reload(
    client: TestClient, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/convert",
        data={"output_format": "html", "formatting_template_id": "docgen-light"},
        headers={"HX-Request": "true", "Accept": "text/html"},
    )
    assert response.status_code == 200

    with _session(client) as session:
        DocumentRepository(session).save_document(
            project_with_source.id,
            WorkingDocument(
                title="Semantic result",
                template_id="faq",
                origin=DocumentOrigin.ASSEMBLED,
                build_template_id="faq",
                nodes=[],
            ),
        )
        session.commit()

    reloaded = client.get(f"/projects/{project_with_source.id}")
    result = BeautifulSoup(reloaded.text, "html.parser").find(id="resultPanel")

    assert result is not None
    assert result.find(id="resultFilename").get_text(strip=True) == "Semantic result"
    assert result.find("a", string="Открыть") is None


def test_template_free_docx_conversion_downloads_without_creating_editor_document(
    client: TestClient, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/convert",
        data={"output_format": "docx", "formatting_template_id": "colvir"},
    )

    assert response.status_code == 200
    assert response.content.startswith(b"PK\x03\x04")
    assert response.headers["content-disposition"].startswith("attachment;")
    with _session(client) as session:
        assert DocumentRepository(session).get_document(project_with_source.id) is None


def test_template_free_confluence_to_docx_is_direct_export(
    client: TestClient, empty_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = client.post(
        f"/projects/{empty_project.id}/sources/confluence",
        data={"url": "https://wiki.example.test/pages/42"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    settings = client.app.state.settings
    settings.confluence_api_base = "https://wiki.example.test/rest/api"
    settings.confluence_token = "configured-secret"
    settings.trusted_integration_hosts = ("wiki.example.test",)
    confluence = _StaticConfluenceClient()
    monkeypatch.setattr(
        "docgen.generation.routes.ConfluenceClient.from_settings",
        lambda _settings: confluence,
    )

    response = client.post(
        f"/projects/{empty_project.id}/convert",
        data={"output_format": "docx", "formatting_template_id": "colvir"},
    )

    assert response.status_code == 200
    assert response.content.startswith(b"PK\x03\x04")
    assert b"Wiki" not in response.content
    with _session(client) as session:
        assert DocumentRepository(session).get_document(empty_project.id) is None


def test_template_free_conversion_requires_exactly_one_source(
    client: TestClient, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/sources/files",
        files={"file": ("second.md", b"# Second", "text/markdown")},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    response = client.post(
        f"/projects/{project_with_source.id}/convert",
        data={"output_format": "html", "formatting_template_id": "docgen-light"},
    )

    assert response.status_code == 422
    assert "ровно один источник" in response.text
    assert _jobs_for_project(client, project_with_source.id) == []


def test_template_free_conversion_shows_friendly_browser_error_for_multiple_sources(
    client: TestClient, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/sources/files",
        files={"file": ("second.md", b"# Second", "text/markdown")},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    response = client.post(
        f"/projects/{project_with_source.id}/convert",
        data={"output_format": "html", "formatting_template_id": "docgen-light"},
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert "Выбери один источник" in response.text
    assert "Сейчас в проекте их 2" in response.text
    assert f'href="/projects/{project_with_source.id}#sourcesPanel"' in response.text
    assert '"detail"' not in response.text


def test_template_free_conversion_shows_friendly_inline_error_for_multiple_sources(
    client: TestClient, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/sources/files",
        files={"file": ("second.md", b"# Second", "text/markdown")},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    response = client.post(
        f"/projects/{project_with_source.id}/convert",
        data={"output_format": "html", "formatting_template_id": "docgen-light"},
        headers={"HX-Request": "true", "Accept": "text/html"},
    )

    assert response.status_code == 422
    result = BeautifulSoup(response.text, "html.parser")
    assert result.find(id="resultStatus").get_text(strip=True) == "Ошибка"
    assert "Сейчас в проекте: 2" in result.get_text(" ")
    assert '"detail"' not in response.text


def test_start_assemble_accepts_external_semantic_template(
    client: TestClient,
    configured_models: None,
    project_with_source: Project,
    tmp_path: Path,
) -> None:
    bundled_faq = files("docgen.templates_catalog").joinpath("semantic/faq.yaml")
    (tmp_path / "custom-faq.yaml").write_text(
        bundled_faq.read_text(encoding="utf-8").replace("id: faq", "id: custom-faq", 1),
        encoding="utf-8",
    )
    client.app.state.settings.template_dir = tmp_path

    response = client.post(
        f"/projects/{project_with_source.id}/jobs/assemble",
        data={"template_id": "custom-faq"},
    )

    assert response.status_code == 202
    assert _jobs_for_project(client, project_with_source.id)[0].template_id == "custom-faq"


def test_text_only_model_configuration_enqueues_assemble_job(
    client: TestClient, configured_text_model: None, project_with_source: Project
) -> None:
    settings = client.app.state.settings
    settings.local_text_base_url = "http://text-model.test/v1"
    settings.local_text_model = "text-model"
    settings.trusted_integration_hosts = ("text-model.test",)

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


def test_start_check_rejects_without_template(
    client: TestClient, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/jobs/check",
        data={"template_id": "no-template"},
    )

    assert response.status_code == 422
    assert "выберите смысловой шаблон" in response.text.lower()
    assert _jobs_for_project(client, project_with_source.id) == []


def test_start_standalone_check_enqueues_owned_target_source(
    client: TestClient, configured_models: None, project_with_source: Project
) -> None:
    target_source_id = _source_id(client, project_with_source.id, "case.md")

    response = client.post(
        f"/projects/{project_with_source.id}/jobs/check",
        data={"template_id": "use-case", "target_source_id": target_source_id},
    )

    assert response.status_code == 202
    job = _jobs_for_project(client, project_with_source.id)[0]
    assert job.kind is JobKind.CHECK
    assert job.target_source_id == target_source_id


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


def test_failed_job_renders_safe_worker_error(
    client: TestClient, project_with_source: Project
) -> None:
    with _session(client) as session:
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(project_with_source.id, JobKind.ASSEMBLE, "use-case")
        repository.claim_next()
        repository.mark_failed(
            job.id,
            "Локальная модель недоступна",
            user_message="Локальная модель недоступна",
        )

    response = client.get(f"/projects/{project_with_source.id}/jobs/{job.id}")

    assert response.status_code == 200
    assert "Локальная модель недоступна" in response.text


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


def test_untrusted_confluence_api_returns_safe_503_before_enqueue(
    client: TestClient, configured_models: None
) -> None:
    project = _create_project(client, "Проект Confluence")
    response = client.post(
        f"/projects/{project.id}/sources/confluence",
        data={"url": "https://wiki.example.test/pages/42"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    settings = client.app.state.settings
    settings.confluence_api_base = "https://public.example/rest/api"
    settings.confluence_token = "configured-secret"

    response = client.post(
        f"/projects/{project.id}/jobs/assemble",
        data={"template_id": "use-case"},
    )

    assert response.status_code == 503
    assert "Адрес API Confluence не разрешён настройками" in response.text
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


def test_check_uses_the_only_uploaded_document_without_reselecting_it(
    client: TestClient, configured_models: None, project_with_source: Project
) -> None:
    target_source_id = _source_id(client, project_with_source.id, "case.md")

    response = client.post(
        f"/projects/{project_with_source.id}/jobs/check",
        data={"template_id": "use-case"},
    )

    assert response.status_code == 202
    job = _jobs_for_project(client, project_with_source.id)[0]
    assert job.target_source_id == target_source_id
    assert job.template_id == "use-case"


def test_check_rejects_target_source_from_another_project(
    client: TestClient,
    configured_models: None,
    project_with_source: Project,
    other_project: Project,
) -> None:
    response = client.post(
        f"/projects/{other_project.id}/sources/files",
        files={"file": ("other.md", b"# Other", "text/markdown")},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    other_source_id = _source_id(client, other_project.id, "other.md")

    response = client.post(
        f"/projects/{project_with_source.id}/jobs/check",
        data={"template_id": "use-case", "target_source_id": other_source_id},
    )

    assert response.status_code == 422
    assert "Документ для проверки не найден" in response.text
    assert _jobs_for_project(client, project_with_source.id) == []


def test_check_rejects_raster_source_as_target(
    client: TestClient, configured_models: None, project_with_source: Project
) -> None:
    response = client.post(
        f"/projects/{project_with_source.id}/sources/files",
        files={"file": ("diagram.png", b"not-an-image", "image/png")},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    image_id = _source_id(client, project_with_source.id, "diagram.png")

    response = client.post(
        f"/projects/{project_with_source.id}/jobs/check",
        data={"template_id": "use-case", "target_source_id": image_id},
    )

    assert response.status_code == 422
    assert "Документ для проверки не найден" in response.text
    assert _jobs_for_project(client, project_with_source.id) == []


def test_setup_lists_documents_and_confluence_but_excludes_raster(
    client: TestClient, project_with_source: Project
) -> None:
    text_response = client.post(
        f"/projects/{project_with_source.id}/sources/files",
        files={"file": ("notes.txt", b"Notes", "text/plain")},
        headers={"HX-Request": "true"},
    )
    assert text_response.status_code == 200
    image_response = client.post(
        f"/projects/{project_with_source.id}/sources/files",
        files={"file": ("diagram.png", b"not-used", "image/png")},
        headers={"HX-Request": "true"},
    )
    assert image_response.status_code == 200
    confluence_response = client.post(
        f"/projects/{project_with_source.id}/sources/confluence",
        data={"url": "https://wiki.example.test/pages/42"},
        headers={"HX-Request": "true"},
    )
    assert confluence_response.status_code == 200
    markdown_id = _source_id(client, project_with_source.id, "case.md")
    image_id = _source_id(client, project_with_source.id, "diagram.png")
    confluence_id = _source_id(
        client,
        project_with_source.id,
        "https://wiki.example.test/pages/42",
    )

    response = client.get(f"/projects/{project_with_source.id}")

    assert response.status_code == 200
    target_select = response.text.split('name="target_source_id"', maxsplit=1)[1].split(
        "</select>", maxsplit=1
    )[0]
    assert f'value="{markdown_id}"' in target_select
    assert "case.md" in target_select
    assert f'value="{image_id}"' not in target_select
    assert f'value="{confluence_id}"' in target_select
    assert "wiki.example.test" in target_select


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
    assert 'id="docgen2Editor"' in response.text
    assert 'hx-swap-oob="outerHTML"' in response.text
    assert 'id="editor-shell"' not in response.text
    assert "Оплата заказа" in response.text
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


def test_failed_standalone_check_retry_preserves_target_source(
    client: TestClient, project_with_source: Project
) -> None:
    target_source_id = _source_id(client, project_with_source.id, "case.md")
    with _session(client) as session:
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(
            project_with_source.id,
            JobKind.CHECK,
            "use-case",
            target_source_id=target_source_id,
        )
        assert repository.claim_next() is not None
        repository.mark_failed(job.id, "safe failure")

    response = client.get(f"/projects/{project_with_source.id}/jobs/{job.id}")

    assert response.status_code == 200
    assert f'name="target_source_id" value="{target_source_id}"' in response.text


def test_terminal_standalone_check_with_deleted_target_cannot_retry_as_current(
    client: TestClient, project_with_source: Project
) -> None:
    target_source_id = _source_id(client, project_with_source.id, "case.md")
    with _session(client) as session:
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(
            project_with_source.id,
            JobKind.CHECK,
            "use-case",
            target_source_id=target_source_id,
        )
        assert repository.claim_next() is not None
        repository.mark_failed(job.id, "safe failure")

    deleted = client.delete(
        f"/projects/{project_with_source.id}/sources/{target_source_id}",
        headers={"HX-Request": "true"},
    )
    assert deleted.status_code == 200

    response = client.get(f"/projects/{project_with_source.id}/jobs/{job.id}")

    assert "Исходный документ удалён" in response.text
    assert 'data-action="retry-job"' not in response.text


def test_cancelled_job_renders_retry_action(client: TestClient, running_job: Job) -> None:
    client.post(f"/projects/{running_job.project_id}/jobs/{running_job.id}/cancel")
    with _session(client) as session:
        JobRepository(
            session,
            worker_id="route-test-worker",
            instance_token=running_job.worker_instance_token,
        ).mark_cancelled(running_job.id)

    response = client.get(f"/projects/{running_job.project_id}/jobs/{running_job.id}")

    assert response.status_code == 200
    assert "Повторить" in response.text
    assert f'/projects/{running_job.project_id}/jobs/assemble' in response.text


def test_missing_artifact_retry_swaps_error_responses_into_status(
    client: TestClient, project_with_source: Project
) -> None:
    """Catch HTMX leaving a missing-artifact retry failure invisible to the user."""
    with _session(client) as session:
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(project_with_source.id, JobKind.ASSEMBLE, "use-case")
        assert repository.claim_next() is not None
        repository.mark_succeeded(job.id)

    response = client.get(f"/projects/{project_with_source.id}/jobs/{job.id}")

    assert response.status_code == 200
    assert "Результат пока недоступен" in response.text
    assert "hx-on" not in response.text
    project_page = client.get(f"/projects/{project_with_source.id}")
    assert '"code":"[2345].."' in project_page.text


def test_full_page_generation_error_keeps_workspace_context(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())
    saved = client.post(
        f"/projects/{project_with_source.id}/editor/save",
        json={
            "title": "Оплата заказа",
            "html": '<p data-node-id="node-1"><em>Rich workspace</em></p>',
            "revision": 1,
        },
    )
    assert saved.status_code == 200

    response = client.post(
        f"/projects/{project_with_source.id}/jobs/assemble",
        data={"template_id": "missing"},
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 422
    assert 'id="project-workspace"' in response.text
    assert 'id="docgen2Editor"' in response.text
    assert 'id="docgen2DocumentCanvas"' in response.text
    assert 'data-state="ready"' in response.text
    assert "<em>Rich workspace</em>" in response.text
    assert "Шаблон не найден" in response.text


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


def test_document_view_keeps_read_only_preview(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())

    response = client.get(
        f"/projects/{project_with_source.id}/document",
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 200
    assert "Собранный документ" in response.text
    assert 'id="document-start"' in response.text
    assert 'id="editor-shell"' not in response.text


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
    assert "structure-1" in response.text
    assert "style-5" in response.text
    assert "completeness-2" in response.text
    assert "terminology-3" in response.text
    assert (
        f'data-node-target="node-1" href="/projects/{project_with_source.id}#doc-node-node-1"'
        in response.text
    )
    assert (
        f'href="/projects/{project_with_source.id}#docgen2Editor"'
        in response.text
    )


def test_report_renders_evidence_and_suggestion_as_separate_blocks(
    client: TestClient, project_with_source: Project
) -> None:
    """A finding's quote and its proposed fix must not be folded into one
    paragraph with the problem statement -- each is its own labelled block."""
    _save_document(client, project_with_source.id, _document())
    report = CheckReport(
        template_id="use-case",
        findings=[
            CheckFinding(
                code="confirmed",
                severity=Severity.ERROR,
                confidence=0.9,
                message="Шаг не пронумерован",
                evidence="Пользователь открывает форму и заполняет поля",
                suggestion="Добавьте номер шага перед описанием действия",
                node_id="node-1",
                rule_id="structure-1",
            ),
            CheckFinding(
                code="no-detail",
                severity=Severity.WARNING,
                confidence=0.9,
                message="Замечание без цитаты и без предложения",
                node_id="node-1",
                rule_id="style-5",
            ),
        ],
    )
    _save_report(client, project_with_source.id, report)

    response = client.get(f"/projects/{project_with_source.id}/report")

    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    blockquotes = [tag.get_text(strip=True) for tag in page.find_all("blockquote")]
    assert blockquotes == ["Пользователь открывает форму и заполняет поля"]
    assert "Как исправить:" in response.text
    assert "Добавьте номер шага перед описанием действия" in response.text
    assert "Замечание без цитаты и без предложения" in response.text


def test_report_card_reinjects_the_actionable_chat_card(
    client: TestClient, project_with_source: Project
) -> None:
    """The narrow chat only restores a compact link; details and fix
    controls live on the wide report page."""
    _save_document(client, project_with_source.id, _document())
    report = CheckReport(
        template_id="use-case",
        findings=[
            CheckFinding(
                code="confirmed",
                severity=Severity.ERROR,
                confidence=0.9,
                message="Шаг не пронумерован",
                suggestion="Добавьте номер шага перед описанием действия",
                node_id="node-1",
                rule_id="structure-1",
            ),
        ],
    )
    _save_report(client, project_with_source.id, report)

    response = client.get(f"/projects/{project_with_source.id}/report/card")

    assert response.status_code == 200
    assert 'hx-swap-oob="beforeend:#chat-messages"' in response.text
    assert "Шаг не пронумерован" not in response.text
    assert "Открыть отчёт" in response.text
    report_response = client.get(f"/projects/{project_with_source.id}/report")
    assert "Шаг не пронумерован" in report_response.text
    assert (
        f'hx-post="/projects/{project_with_source.id}/report/findings/structure-1/propose-fix"'
        in report_response.text
    )


def test_whole_form_mismatch_offers_scoped_fix_instead_of_rebuild(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())
    _save_report(
        client,
        project_with_source.id,
        CheckReport(
            template_id="use-case",
            findings=[
                CheckFinding(
                    code="template-structure-mismatch",
                    severity=Severity.ERROR,
                    confidence=1,
                    message="Не хватает полной формы",
                    suggestion="Добавить пустые таблицы",
                    node_id="node-1",
                    rule_id="use-case-template-form",
                )
            ],
        ),
    )

    response = client.get(f"/projects/{project_with_source.id}/report/card")
    report_response = client.get(f"/projects/{project_with_source.id}/report")

    assert response.status_code == 200
    assert "Открыть отчёт" in response.text
    assert "Не хватает полной формы" in report_response.text
    assert "Предложить правку" in report_response.text
    assert "Пересобрать по шаблону" not in report_response.text
    assert (
        f'hx-post="/projects/{project_with_source.id}/report/findings/use-case-template-form/propose-fix"'
        in report_response.text
    )


def test_report_card_missing_returns_friendly_banner_not_raw_json(
    client: TestClient, project_with_source: Project
) -> None:
    """A stale/missing report must render as a readable chat banner, not
    the bare {"detail": "..."} JSON FastAPI produces for a raw HTTPException."""
    response = client.get(f"/projects/{project_with_source.id}/report/card")

    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert '"detail"' not in response.text
    assert "документ изменился после последней проверки" in response.text
    assert "Запусти проверку по шаблону ещё раз" in response.text


def test_report_view_missing_returns_friendly_page_not_raw_json(
    client: TestClient, project_with_source: Project
) -> None:
    response = client.get(f"/projects/{project_with_source.id}/report")

    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert '"detail"' not in response.text
    assert "Отчёт недоступен" in response.text
    assert f'href="/projects/{project_with_source.id}#docgen2Editor"' in response.text


def test_stale_report_remains_visible_and_can_start_a_real_recheck(
    client: TestClient, project_with_source: Project
) -> None:
    document = _document()
    _save_document(client, project_with_source.id, document)
    _save_report(
        client,
        project_with_source.id,
        CheckReport(
            template_id="use-case",
            findings=[
                CheckFinding(
                    code="confirmed",
                    severity=Severity.ERROR,
                    confidence=0.9,
                    message="Шаг не пронумерован",
                    suggestion="Добавьте номер шага",
                    node_id="node-1",
                    rule_id="use-case-structure",
                )
            ],
        ),
    )
    _save_document(client, project_with_source.id, document)

    with _session(client) as session:
        documents = DocumentRepository(session)
        current = documents.get_document_with_revision(project_with_source.id)
        latest = documents.get_latest_report_record(project_with_source.id)
        assert current is not None and latest is not None
        assert current[1] != latest.document_revision

    page = client.get(f"/projects/{project_with_source.id}/report")
    card = client.get(f"/projects/{project_with_source.id}/report/card")

    assert page.status_code == 200
    assert "относятся к предыдущей версии" in page.text
    assert 'action="/projects/' in page.text
    assert "Проверить текущую версию снова" in page.text
    assert card.status_code == 200
    assert "Отчёт сохранён для сравнения" in card.text
    assert "Проверить снова" in card.text
    assert "Предложить правку" not in card.text


def test_project_page_shows_persistent_report_link_only_when_report_exists(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())

    before = client.get(f"/projects/{project_with_source.id}")
    assert 'id="reportLink"' not in before.text

    _save_report(
        client,
        project_with_source.id,
        CheckReport(template_id="use-case", passed_rule_ids=["use-case-structure"]),
    )

    after = client.get(f"/projects/{project_with_source.id}")
    assert 'id="reportLink"' in after.text
    assert f'hx-get="/projects/{project_with_source.id}/report/card"' in after.text


def test_succeeded_assemble_job_updates_docgen2_editor_out_of_band(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())
    with _session(client) as session:
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(project_with_source.id, JobKind.ASSEMBLE, "use-case")
        assert repository.claim_next() is not None
        succeeded_job = repository.mark_succeeded(job.id)

    response = client.get(
        f"/projects/{project_with_source.id}/jobs/{succeeded_job.id}",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'id="docgen2Editor"' in response.text
    assert 'hx-swap-oob="outerHTML"' in response.text
    assert "Оплата заказа" in response.text
    assert 'id="editor-shell"' not in response.text
    assert 'id="generation-status"' in response.text
    assert 'hx-trigger="every 2s"' not in response.text


def test_superseded_job_does_not_render_unrelated_current_document(
    client: TestClient, project_with_source: Project
) -> None:
    first_document = _document().model_copy(update={"title": "Результат задания A"})
    replacement = _document().model_copy(update={"title": "Результат задания B"})
    with _session(client) as session:
        documents = DocumentRepository(session)
        documents.save_document(project_with_source.id, first_document)
        session.commit()
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(project_with_source.id, JobKind.ASSEMBLE, "use-case")
        assert repository.claim_next() is not None
        succeeded = repository.mark_succeeded(job.id)
        documents.save_document(project_with_source.id, replacement)
        session.commit()

    response = client.get(f"/projects/{project_with_source.id}/jobs/{succeeded.id}")

    assert response.status_code == 200
    assert "Результат задания заменён более новым" in response.text
    assert "Результат задания B" not in response.text


def test_earlier_check_job_does_not_render_later_report_for_same_document(
    client: TestClient, project_with_source: Project
) -> None:
    with _session(client) as session:
        documents = DocumentRepository(session)
        documents.save_document(project_with_source.id, _document())
        session.commit()
        jobs = JobRepository(session, worker_id="route-test-worker")

        first_job = jobs.enqueue(project_with_source.id, JobKind.CHECK, "use-case")
        assert jobs.claim_next() is not None
        documents.save_report(
            project_with_source.id,
            CheckReport(
                template_id="use-case",
                findings=[
                    CheckFinding(
                        code="marker",
                        severity=Severity.ERROR,
                        confidence=0.9,
                        message="report-A",
                        rule_id="use-case-structure",
                    )
                ],
            ),
        )
        first_succeeded = jobs.mark_succeeded(first_job.id)

        second_job = jobs.enqueue(project_with_source.id, JobKind.CHECK, "use-case")
        assert jobs.claim_next() is not None
        documents.save_report(
            project_with_source.id,
            CheckReport(
                template_id="use-case",
                findings=[
                    CheckFinding(
                        code="marker",
                        severity=Severity.ERROR,
                        confidence=0.9,
                        message="report-B",
                        rule_id="use-case-structure",
                    )
                ],
            ),
        )
        second_succeeded = jobs.mark_succeeded(second_job.id)

    first_response = client.get(
        f"/projects/{project_with_source.id}/jobs/{first_succeeded.id}"
    )
    second_response = client.get(
        f"/projects/{project_with_source.id}/jobs/{second_succeeded.id}"
    )

    assert "Результат задания заменён более новым" in first_response.text
    assert "report-B" not in first_response.text
    assert "Открыть отчёт" in second_response.text
    assert "report-B" in client.get(f"/projects/{project_with_source.id}/report").text


def test_running_and_succeeded_job_pages_render_persisted_warnings(
    client: TestClient, project_with_source: Project
) -> None:
    with _session(client) as session:
        documents = DocumentRepository(session)
        documents.save_document(project_with_source.id, _document())
        session.commit()
        repository = JobRepository(session, worker_id="route-test-worker")
        job = repository.enqueue(project_with_source.id, JobKind.ASSEMBLE, "use-case")
        assert repository.claim_next() is not None
        repository.add_warnings(
            job.id,
            [
                "Обработка может занять более пяти минут",
                "Страница 2 не содержит извлекаемого текста",
            ],
        )

    running = client.get(f"/projects/{project_with_source.id}/jobs/{job.id}")
    assert "Обработка может занять более пяти минут" in running.text
    assert "Страница 2 не содержит извлекаемого текста" in running.text

    with _session(client) as session:
        repository = JobRepository(
            session,
            worker_id="route-test-worker",
            instance_token=repository.instance_token,
        )
        repository.mark_succeeded(job.id)

    succeeded = client.get(f"/projects/{project_with_source.id}/jobs/{job.id}")
    assert "Обработка может занять более пяти минут" in succeeded.text
    assert "Страница 2 не содержит извлекаемого текста" in succeeded.text


def test_empty_report_never_claims_that_all_rules_were_checked(
    client: TestClient, project_with_source: Project
) -> None:
    _save_document(client, project_with_source.id, _document())
    _save_report(client, project_with_source.id, CheckReport(template_id="use-case"))

    response = client.get(f"/projects/{project_with_source.id}/report")

    assert "Все правила проверены" not in response.text
    assert "Нет сведений о покрытии правил" in response.text


def test_report_does_not_render_passed_rules(
    client: TestClient, project_with_source: Project
) -> None:
    """Passed rules are not user-facing output -- the report only surfaces
    problems (confirmed findings, low-confidence findings) and unchecked
    rules, never the rules that already passed."""
    _save_document(client, project_with_source.id, _document())
    _save_report(
        client,
        project_with_source.id,
        CheckReport(
            template_id="use-case",
            passed_rule_ids=["use-case-structure", "use-case-style"],
        ),
    )

    response = client.get(f"/projects/{project_with_source.id}/report")

    assert "Успешно пройденные правила" not in response.text
    assert "нумерованным основным потоком" not in response.text
    assert "один переход состояния" not in response.text
    assert "use-case-structure" not in response.text
    assert "use-case-style" not in response.text


def test_check_route_job_runs_once_and_swaps_to_saved_report(
    client: TestClient, configured_models: None, project_with_source: Project
) -> None:
    target_source_id = _source_id(client, project_with_source.id, "case.md")
    response = client.post(
        f"/projects/{project_with_source.id}/jobs/check",
        data={"template_id": "use-case", "target_source_id": target_source_id},
    )
    assert response.status_code == 202
    job = _jobs_for_project(client, project_with_source.id)[0]

    catalog = TemplateCatalog()
    template = catalog.get("use-case")
    structural_rule_id = (
        template.structure_check.rule_id if template.structure_check is not None else None
    )
    unchecked_rules = [
        rule.id for rule in template.rules if rule.id != structural_rule_id
    ]
    with _session(client) as session:
        workflow = CheckWorkflow(
            projects=ProjectRepository(session),
            normalization=NormalizationWorkflow(
                SourceRepository(session),
                LocalStorage(client.app.state.settings.data_dir),
                ExtractorRegistry.default(),
                _NoConfluenceClient(),
            ),
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
    assert _job(client, job.id).target_source_id == target_source_id
    with _session(client) as session:
        saved_document = DocumentRepository(session).get_document(project_with_source.id)
    assert saved_document is not None
    assert saved_document.template_id == "no-template"
    assert saved_document.build_template_id is None
    assert saved_document.source_id == target_source_id
    assert saved_document.nodes[0].kind is NodeKind.HEADING
    assert saved_document.nodes[0].text == "Case"

    detail_response = client.get(f"/projects/{project_with_source.id}")
    assert detail_response.status_code == 200
    assert 'id="checkTargetSelect"' not in detail_response.text

    browser_response = client.get(
        f"/projects/{project_with_source.id}/jobs/{job.id}",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert browser_response.status_code == 303
    assert browser_response.headers["location"] == (
        f"/projects/{project_with_source.id}#docgen2Editor"
    )

    response = client.get(
        f"/projects/{project_with_source.id}/jobs/{job.id}",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert 'id="generation-status"' in response.text
    assert "Проверка по шаблону" in response.text
    assert "Структура документа не совпадает с полной формой" not in response.text
    assert f'href="/projects/{project_with_source.id}/report"' in response.text
    assert 'id="docgen2Editor"' in response.text
    assert 'hx-swap-oob="outerHTML"' in response.text
    assert "Case" in response.text
    assert 'id="chatPanel"' in response.text

    full_report_response = client.get(f"/projects/{project_with_source.id}/report")
    assert "Результат проверки" in full_report_response.text
    assert "Структура документа не совпадает с полной формой" in full_report_response.text
    assert (
        f'href="/projects/{project_with_source.id}#docgen2Editor"'
        in full_report_response.text
    )
    assert "Открыть рабочую копию" in full_report_response.text
    assert "Непроверенные правила" in full_report_response.text
    assert unchecked_rules[0] not in full_report_response.text

    repeat_response = client.post(
        f"/projects/{project_with_source.id}/jobs/check",
        data={"template_id": "use-case"},
    )
    assert repeat_response.status_code == 202
    repeat_job = next(
        queued_job
        for queued_job in _jobs_for_project(client, project_with_source.id)
        if queued_job.id != job.id
    )
    assert repeat_job.target_source_id is None
    with _session(client) as session:
        repeat_runner = JobRunner(
            JobRepository(session, worker_id="route-repeat-check-worker"),
            {
                JobKind.CHECK: CheckWorkflow(
                    projects=ProjectRepository(session),
                    normalization=NormalizationWorkflow(
                        SourceRepository(session),
                        LocalStorage(client.app.state.settings.data_dir),
                        ExtractorRegistry.default(),
                        _NoConfluenceClient(),
                    ),
                    templates=catalog,
                    text_model=_StaticCheckModel(unchecked_rules),
                    vision_model=_NoImageVisionModel(),
                    grounding=GroundingValidator(),
                    documents=DocumentRepository(session),
                )
            },
        )
        assert repeat_runner.run_once() is True
    assert _job(client, repeat_job.id).status.value == "succeeded"


def test_confluence_document_can_be_checked_by_two_profiles_without_assembly(
    client: TestClient, configured_text_model: None
) -> None:
    project = _create_project(client, "Confluence use case")
    source_response = client.post(
        f"/projects/{project.id}/sources/confluence",
        data={"url": "https://wiki.example.test/pages/42"},
        headers={"HX-Request": "true"},
    )
    assert source_response.status_code == 200
    source_id = _source_id(client, project.id, "https://wiki.example.test/pages/42")
    settings = client.app.state.settings
    settings.confluence_api_base = "https://wiki.example.test/rest/api"
    settings.confluence_token = "configured-secret"
    settings.trusted_integration_hosts = ("text-model.test", "wiki.example.test")

    first_response = client.post(
        f"/projects/{project.id}/jobs/check",
        data={"template_id": "use-case", "target_source_id": source_id},
    )
    assert first_response.status_code == 202
    first_job = _jobs_for_project(client, project.id)[0]
    confluence = _StaticConfluenceClient()
    catalog = TemplateCatalog()
    with _session(client) as session:
        runner = JobRunner(
            JobRepository(session, worker_id="confluence-check-worker"),
            {
                JobKind.CHECK: CheckWorkflow(
                    projects=ProjectRepository(session),
                    normalization=NormalizationWorkflow(
                        SourceRepository(session),
                        LocalStorage(settings.data_dir),
                        ExtractorRegistry.default(),
                        confluence,
                    ),
                    templates=catalog,
                    text_model=_ProfileCheckModel(),
                    vision_model=_NoImageVisionModel(),
                    grounding=GroundingValidator(),
                    documents=DocumentRepository(session),
                )
            },
        )
        assert runner.run_once() is True

    assert _job(client, first_job.id).status.value == "succeeded"
    with _session(client) as session:
        repository = DocumentRepository(session)
        imported, revision = repository.get_document_with_revision(project.id) or (None, 0)
        assert imported is not None
        assert imported.origin is DocumentOrigin.IMPORTED
        assert imported.source_id == source_id
        assert imported.build_template_id is None
        assert imported.nodes[0].text == "Открытие цифрового счёта"
        assert [item.check_profile_id for item in repository.list_check_reports(project.id)] == [
            "use-case"
        ]

    second_response = client.post(
        f"/projects/{project.id}/jobs/check",
        data={"template_id": "faq"},
    )
    assert second_response.status_code == 202
    second_job = next(
        job for job in _jobs_for_project(client, project.id) if job.id != first_job.id
    )
    with _session(client) as session:
        runner = JobRunner(
            JobRepository(session, worker_id="second-profile-worker"),
            {
                JobKind.CHECK: CheckWorkflow(
                    projects=ProjectRepository(session),
                    normalization=NormalizationWorkflow(
                        SourceRepository(session),
                        LocalStorage(settings.data_dir),
                        ExtractorRegistry.default(),
                        confluence,
                    ),
                    templates=catalog,
                    text_model=_ProfileCheckModel(),
                    vision_model=_NoImageVisionModel(),
                    grounding=GroundingValidator(),
                    documents=DocumentRepository(session),
                )
            },
        )
        assert runner.run_once() is True

    assert _job(client, second_job.id).status.value == "succeeded"
    with _session(client) as session:
        repository = DocumentRepository(session)
        unchanged, unchanged_revision = repository.get_document_with_revision(project.id) or (
            None,
            0,
        )
        assert unchanged == imported
        assert unchanged_revision == revision
        assert [
            item.check_profile_id
            for item in repository.list_check_reports(
                project.id, document_revision=revision
            )
        ] == ["use-case", "faq"]
    assert confluence.calls == 2


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


def _source_id(client: TestClient, project_id: str, display_name: str) -> str:
    with _session(client) as session:
        sources = SourceRepository(session).list_for_project(project_id)
        return next(source.id for source in sources if source.display_name == display_name)


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


class _StaticCheckModel:
    def __init__(self, unchecked_rules: list[str]) -> None:
        self._unchecked_rules = unchecked_rules

    def generate_json(self, system: str, user: str, schema: type[Any]) -> CheckReport:
        assert system
        assert user
        assert schema is CheckReport
        return CheckReport(template_id="use-case", unchecked_rules=self._unchecked_rules)


class _ProfileCheckModel:
    def generate_json(self, system: str, user: str, schema: type[Any]) -> CheckReport:
        import json

        assert system
        assert schema is CheckReport
        payload = json.loads(user)
        profile_id = payload["формат_ответа"]["template_id"]
        return CheckReport(
            template_id=profile_id,
            passed_rule_ids=tuple(rule["id"] for rule in payload["правила"]),
        )


class _StaticConfluenceClient:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, url: str, *, before_external_call: Any = None) -> ExtractionResult:
        assert url == "https://wiki.example.test/pages/42"
        if before_external_call is not None:
            before_external_call()
        self.calls += 1
        return ExtractionResult(
            blocks=[
                NormalizedBlock(
                    id="confluence-heading",
                    kind=BlockKind.HEADING,
                    text="Открытие цифрового счёта",
                    provenance=[
                        Provenance(
                            source_id="confluence:42",
                            locator="heading:1",
                            quote="Открытие цифрового счёта",
                        )
                    ],
                    confidence=1.0,
                ),
                NormalizedBlock(
                    id="confluence-body",
                    kind=BlockKind.TEXT,
                    text="Клиент заполняет заявку, после чего система открывает счёт.",
                    provenance=[
                        Provenance(
                            source_id="confluence:42",
                            locator="paragraph:1",
                            quote="Клиент заполняет заявку",
                        )
                    ],
                    confidence=1.0,
                ),
            ],
            page_units=1,
            warnings=[],
        )


class _NoImageVisionModel:
    def describe(self, image: bytes, media_type: str) -> object:
        del image, media_type
        raise AssertionError("text-only check must not call the vision model")


class _NoConfluenceClient:
    def fetch(self, url: str, *, before_external_call: Any = None) -> object:
        del url, before_external_call
        raise AssertionError("file-only check must not call Confluence")
