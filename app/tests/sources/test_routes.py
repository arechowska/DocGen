import asyncio
from io import BytesIO
from threading import get_ident

import pytest
from fastapi.testclient import TestClient
from starlette import formparsers
from starlette.requests import Request

from docgen.jobs.models import JobKind
from docgen.jobs.repository import JobRepository
from docgen.projects.models import Project
from docgen.projects.repository import ProjectRepository
from docgen.sources.repository import SourceRepository
from docgen.sources.routes import _process_upload


class _RecordingSpool(BytesIO):
    def __init__(self, max_size: int = 0, *args: object, **kwargs: object) -> None:
        del args, kwargs
        super().__init__()
        self._max_size = max_size
        self._rolled = False
        self.max_written = 0

    def write(self, data: bytes) -> int:
        self.max_written = max(self.max_written, self.tell() + len(data))
        return super().write(data)


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


def test_chunked_upload_is_rejected_before_spool_exceeds_file_limit(
    client: TestClient,
    existing_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_spools: list[_RecordingSpool] = []

    class RecordingSpool(_RecordingSpool):
        def __init__(self, max_size: int = 0, *args: object, **kwargs: object) -> None:
            super().__init__(max_size, *args, **kwargs)
            recorded_spools.append(self)

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", RecordingSpool)
    client.app.state.settings.max_upload_bytes = 4
    boundary = "docgen-stream-boundary"
    chunks = iter(
        [
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="case.md"\r\n'
                "Content-Type: text/markdown\r\n\r\n"
            ).encode(),
            b"123",
            b"45",
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    request = client.build_request(
        "POST",
        f"/projects/{existing_project.id}/sources/files",
        content=chunks,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "HX-Request": "true",
        },
    )
    assert "content-length" not in request.headers

    response = client.send(request)

    assert response.status_code == 422
    assert "Файл слишком большой" in response.text
    assert recorded_spools
    assert max(spool.max_written for spool in recorded_spools) <= 4
    with client.app.state.session_factory() as session:
        assert SourceRepository(session).list_for_project(existing_project.id) == []

    accepted = client.post(
        f"/projects/{existing_project.id}/sources/files",
        files={"file": ("boundary.md", b"1234", "text/markdown")},
        headers={"HX-Request": "true"},
    )
    assert accepted.status_code == 200
    assert "boundary.md" in accepted.text


@pytest.mark.parametrize(
    ("error", "expected_status"),
    (
        (None, 200),
        (ValueError("Формат файла не поддерживается"), 422),
        (LookupError("Проект не найден"), 404),
        (RuntimeError("storage failed"), None),
    ),
)
def test_upload_service_runs_off_event_loop_and_closes_file(
    error: Exception | None, expected_status: int | None
) -> None:
    service = _RecordingSourceService(error)
    upload = _RecordingUpload()

    async def exercise_route() -> int | None:
        event_loop_thread = get_ident()
        request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})

        if isinstance(error, RuntimeError):
            with pytest.raises(RuntimeError, match="storage failed"):
                await _process_upload(request, "project-1", upload, service)  # type: ignore[arg-type]
            response_status = None
        else:
            response = await _process_upload(  # type: ignore[arg-type]
                request, "project-1", upload, service
            )
            response_status = response.status_code

        assert service.add_file_thread is not None
        assert service.add_file_thread != event_loop_thread
        if error is None:
            assert service.list_thread is not None
            assert service.list_thread != event_loop_thread
        return response_status

    assert asyncio.run(exercise_route()) == expected_status
    assert upload.closed


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


def test_non_htmx_upload_error_renders_usable_full_project_page(
    client: TestClient, existing_project: Project
) -> None:
    response = client.post(
        f"/projects/{existing_project.id}/sources/files",
        files={"file": ("case.zip", b"archive", "application/zip")},
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 422
    assert "<!doctype html>" in response.text.lower()
    assert "Существующий проект" in response.text
    assert "Формат файла не поддерживается" in response.text
    assert 'action="/projects/' in response.text
    assert 'name="file"' in response.text


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


def test_delete_source_returns_409_while_project_job_is_active(
    client: TestClient, existing_project: Project
) -> None:
    upload = client.post(
        f"/projects/{existing_project.id}/sources/files",
        files={"file": ("case.md", b"# Case", "text/markdown")},
    )
    assert upload.status_code == 200
    source_id = _source_id(client, existing_project.id)
    with client.app.state.session_factory() as session:
        JobRepository(session).enqueue(
            existing_project.id,
            JobKind.CHECK,
            "use-case",
            target_source_id=source_id,
        )

    response = client.delete(
        f"/projects/{existing_project.id}/sources/{source_id}",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 409
    assert "источник нельзя удалить" in response.text


def _source_id(client: TestClient, project_id: str) -> str:
    session = client.app.state.session_factory()
    try:
        return SourceRepository(session).list_for_project(project_id)[0].id
    finally:
        session.close()


class _RecordingSourceService:
    def __init__(self, error: Exception | None) -> None:
        self._error = error
        self.add_file_thread: int | None = None
        self.list_thread: int | None = None

    def add_file(self, project_id: str, filename: str, media_type: str, stream: BytesIO) -> None:
        self.add_file_thread = get_ident()
        if self._error is not None:
            raise self._error

    def list(self, project_id: str) -> list[object]:
        self.list_thread = get_ident()
        return []


class _RecordingUpload:
    filename = "case.md"
    content_type = "text/markdown"

    def __init__(self) -> None:
        self.file = BytesIO(b"# Case")
        self.closed = False

    async def close(self) -> None:
        self.closed = True
