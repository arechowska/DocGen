import pytest
from fastapi.testclient import TestClient

from docgen.projects.models import Project
from docgen.projects.repository import ProjectRepository
from docgen.sources.repository import SourceRepository


@pytest.fixture
def existing_project(client: TestClient) -> Project:
    session = client.app.state.session_factory()
    try:
        project = ProjectRepository(session).create("Существующий проект")
        session.commit()
        return project
    finally:
        session.close()


def test_upload_file_returns_updated_source_list(
    client: TestClient, existing_project: Project
) -> None:
    response = client.post(
        f"/projects/{existing_project.id}/sources/files",
        files={"file": ("case.md", b"# Case", "text/markdown")},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "case.md" in response.text
    assert "Файл" in response.text


def test_add_confluence_link_returns_updated_source_list(
    client: TestClient, existing_project: Project
) -> None:
    response = client.post(
        f"/projects/{existing_project.id}/sources/confluence",
        data={"url": "https://wiki.example.test/display/DOC/Page"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "wiki.example.test" in response.text
    assert "Confluence" in response.text


def test_project_detail_renders_source_controls_for_its_project(
    client: TestClient, existing_project: Project
) -> None:
    response = client.get(f"/projects/{existing_project.id}")

    assert response.status_code == 200
    assert f'hx-post="/projects/{existing_project.id}/sources/files"' in response.text
    assert f'hx-post="/projects/{existing_project.id}/sources/confluence"' in response.text


def test_unsupported_upload_returns_error_fragment(
    client: TestClient, existing_project: Project
) -> None:
    response = client.post(
        f"/projects/{existing_project.id}/sources/files",
        files={"file": ("case.zip", b"archive", "application/zip")},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert "Формат файла не поддерживается" in response.text


def test_invalid_confluence_url_returns_error_fragment(
    client: TestClient, existing_project: Project
) -> None:
    response = client.post(
        f"/projects/{existing_project.id}/sources/confluence",
        data={"url": "https://example.org/page"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert "Разрешены только ссылки Confluence" in response.text


def test_missing_project_returns_404_for_source_upload(client: TestClient) -> None:
    response = client.post(
        "/projects/missing/sources/files",
        files={"file": ("case.md", b"# Case", "text/markdown")},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 404
    assert "Проект не найден" in response.text


def test_delete_source_returns_updated_source_list(
    client: TestClient, existing_project: Project
) -> None:
    upload = client.post(
        f"/projects/{existing_project.id}/sources/files",
        files={"file": ("case.md", b"# Case", "text/markdown")},
        headers={"HX-Request": "true"},
    )
    assert upload.status_code == 200

    source_id = _source_id(client, existing_project.id)

    response = client.delete(
        f"/projects/{existing_project.id}/sources/{source_id}",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "Источники пока не добавлены." in response.text
    assert "case.md" not in response.text


def test_delete_rejects_source_from_another_project(
    client: TestClient, existing_project: Project
) -> None:
    session = client.app.state.session_factory()
    try:
        other_project = ProjectRepository(session).create("Другой проект")
        session.commit()
    finally:
        session.close()

    upload = client.post(
        f"/projects/{existing_project.id}/sources/files",
        files={"file": ("case.md", b"# Case", "text/markdown")},
        headers={"HX-Request": "true"},
    )
    assert upload.status_code == 200
    source_id = _source_id(client, existing_project.id)

    response = client.delete(
        f"/projects/{other_project.id}/sources/{source_id}",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 404
    assert "Источник не найден" in response.text


def _source_id(client: TestClient, project_id: str) -> str:
    session = client.app.state.session_factory()
    try:
        return SourceRepository(session).list_for_project(project_id)[0].id
    finally:
        session.close()
