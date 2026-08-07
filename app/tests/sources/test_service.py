from io import BytesIO

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from docgen.projects.models import Project
from docgen.projects.service import ProjectService
from docgen.sources.models import Source, SourceKind
from docgen.sources.service import SourceService
from docgen.sources.storage import LocalStorage


def create_project(session: Session, project_id: str = "project-1") -> Project:
    project = Project(id=project_id, name="Проект")
    session.add(project)
    session.commit()
    return project


def test_add_supported_file(source_service: SourceService, session: Session) -> None:
    create_project(session)

    source = source_service.add_file(
        "project-1",
        "case.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        BytesIO(b"docx"),
    )

    assert source.kind is SourceKind.FILE
    assert source.display_name == "case.docx"
    assert source.status == "stored"
    assert source.storage_path is not None
    assert source.size_bytes == 4


def test_add_file_normalizes_extension(source_service: SourceService, session: Session) -> None:
    create_project(session)

    source = source_service.add_file("project-1", "CASE.PDF", "application/pdf", BytesIO(b"pdf"))

    assert source.storage_path is not None
    assert source.storage_path.endswith(".pdf")


def test_reject_unsupported_extension(source_service: SourceService, session: Session) -> None:
    create_project(session)

    with pytest.raises(ValueError, match="Формат файла не поддерживается"):
        source_service.add_file("project-1", "archive.zip", "application/zip", BytesIO(b"zip"))


@pytest.mark.parametrize(
    "url",
    (
        "http://wiki.example.test/page",
        "https://user:password@wiki.example.test/page",
        "https://example.org/page",
        "https://wiki.example.test",
        "https://wiki.example.test:invalid/page",
    ),
)
def test_reject_non_confluence_url(
    source_service: SourceService, session: Session, url: str
) -> None:
    create_project(session)

    with pytest.raises(ValueError, match="Разрешены только ссылки Confluence"):
        source_service.add_confluence("project-1", url)


def test_add_allowed_confluence_url(source_service: SourceService, session: Session) -> None:
    create_project(session)

    source = source_service.add_confluence(
        "project-1", "https://wiki.example.test/display/DOC/Page"
    )

    assert source.kind is SourceKind.CONFLUENCE
    assert source.url == "https://wiki.example.test/display/DOC/Page"
    assert source.storage_path is None
    assert source.status == "linked"


def test_rejects_missing_project_before_storing_file(
    source_service: SourceService, tmp_path
) -> None:
    with pytest.raises(LookupError, match="Проект не найден"):
        source_service.add_file("missing", "case.txt", "text/plain", BytesIO(b"text"))

    assert not (tmp_path / "data" / "projects" / "missing").exists()


def test_delete_rejects_source_owned_by_another_project(
    source_service: SourceService, session: Session
) -> None:
    create_project(session, "project-1")
    create_project(session, "project-2")
    source = source_service.add_file("project-1", "case.txt", "text/plain", BytesIO(b"text"))

    with pytest.raises(LookupError, match="Источник не найден"):
        source_service.delete("project-2", source.id)

    assert source_service.list("project-1")[0].id == source.id


def test_delete_removes_file_only_after_source_record_commits(
    source_service: SourceService, session: Session, tmp_path
) -> None:
    create_project(session)
    source = source_service.add_file("project-1", "case.txt", "text/plain", BytesIO(b"text"))
    assert source.storage_path is not None
    stored_path = tmp_path / "data" / source.storage_path

    source_service.delete("project-1", source.id)

    assert session.get(Source, source.id) is None
    assert not stored_path.exists()


def test_add_file_removes_stored_file_when_database_persistence_fails(
    source_service: SourceService, session: Session, monkeypatch, tmp_path
) -> None:
    create_project(session)

    def fail_flush() -> None:
        raise RuntimeError("database failed")

    monkeypatch.setattr(session, "flush", fail_flush)

    with pytest.raises(RuntimeError, match="database failed"):
        source_service.add_file("project-1", "case.txt", "text/plain", BytesIO(b"text"))

    assert not list((tmp_path / "data" / "projects" / "project-1" / "sources").glob("*"))


def test_project_deletion_cascades_source_records_and_project_files(session: Session, tmp_path) -> None:
    storage = LocalStorage(tmp_path / "data")
    service = SourceService(session, storage, confluence_hosts=("wiki.example.test",))
    create_project(session)
    source = service.add_file("project-1", "case.txt", "text/plain", BytesIO(b"text"))

    ProjectService(session, storage).delete("project-1")

    assert session.get(Project, "project-1") is None
    assert session.scalars(select(Source)).all() == []
    assert source.storage_path is not None
    assert not (tmp_path / "data" / source.storage_path).exists()


def test_project_deletion_rejects_missing_project(session: Session, tmp_path) -> None:
    with pytest.raises(LookupError, match="Проект не найден"):
        ProjectService(session, LocalStorage(tmp_path / "data")).delete("missing")
