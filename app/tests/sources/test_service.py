import logging
from io import BytesIO

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from docgen.jobs.models import CheckTargetKind, Job, JobKind
from docgen.jobs.repository import ActiveProjectJobExists, JobRepository
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


def test_file_and_project_storage_limits_accept_boundaries_and_clean_rejection(
    session: Session, tmp_path
) -> None:
    storage = LocalStorage(tmp_path / "data")
    service = SourceService(
        session,
        storage,
        confluence_hosts=("wiki.example.test",),
        max_upload_bytes=4,
        max_project_storage_bytes=7,
    )
    create_project(session)

    first = service.add_file("project-1", "one.txt", "text/plain", BytesIO(b"1234"))
    second = service.add_file("project-1", "two.txt", "text/plain", BytesIO(b"567"))
    with pytest.raises(ValueError, match="Общий объём файлов проекта превышен"):
        service.add_file("project-1", "three.txt", "text/plain", BytesIO(b"8"))

    assert first.size_bytes == 4
    assert second.size_bytes == 3
    assert len(service.list("project-1")) == 2
    source_files = list((tmp_path / "data" / "projects" / "project-1" / "sources").glob("*"))
    assert len(source_files) == 2
    assert all(path.suffix != ".part" for path in source_files)


def test_per_file_limit_rejection_leaves_no_file_or_source(session: Session, tmp_path) -> None:
    storage = LocalStorage(tmp_path / "data")
    service = SourceService(
        session,
        storage,
        confluence_hosts=("wiki.example.test",),
        max_upload_bytes=4,
        max_project_storage_bytes=100,
    )
    create_project(session)

    with pytest.raises(ValueError, match="Файл слишком большой"):
        service.add_file("project-1", "large.txt", "text/plain", BytesIO(b"12345"))

    assert service.list("project-1") == []
    assert not list((tmp_path / "data" / "projects" / "project-1" / "sources").glob("*"))


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


def test_source_delete_is_rejected_while_project_job_is_active(
    source_service: SourceService, session: Session, tmp_path
) -> None:
    create_project(session)
    source = source_service.add_file(
        "project-1", "case.txt", "text/plain", BytesIO(b"text")
    )
    assert source.storage_path is not None
    stored_path = tmp_path / "data" / source.storage_path
    JobRepository(session).enqueue(
        "project-1", JobKind.CHECK, "use-case", target_source_id=source.id
    )

    with pytest.raises(ActiveProjectJobExists, match="обрабатывается"):
        source_service.delete("project-1", source.id)

    assert session.get(Source, source.id) is not None
    assert stored_path.exists()


def test_source_delete_clears_terminal_job_reference_without_changing_target_intent(
    source_service: SourceService, session: Session
) -> None:
    create_project(session)
    source = source_service.add_file(
        "project-1", "case.txt", "text/plain", BytesIO(b"text")
    )
    jobs = JobRepository(session, worker_id="terminal-worker")
    job = jobs.enqueue(
        "project-1", JobKind.CHECK, "use-case", target_source_id=source.id
    )
    assert jobs.claim_next() is not None
    jobs.mark_failed(job.id, "safe failure")

    source_service.delete("project-1", source.id)

    session.expire_all()
    terminal_job = session.get(Job, job.id)
    assert terminal_job is not None
    assert terminal_job.target_source_id is None
    assert terminal_job.check_target_kind is CheckTargetKind.SOURCE


def test_project_delete_is_rejected_while_job_is_active(
    source_service: SourceService, session: Session, tmp_path
) -> None:
    create_project(session)
    source_service.add_file("project-1", "case.txt", "text/plain", BytesIO(b"text"))
    JobRepository(session).enqueue("project-1", JobKind.ASSEMBLE, "use-case")

    with pytest.raises(ActiveProjectJobExists, match="обрабатывается"):
        ProjectService(session, LocalStorage(tmp_path / "data")).delete("project-1")

    assert session.get(Project, "project-1") is not None


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


def test_source_cleanup_failure_is_logged_after_database_commit(
    session: Session, tmp_path, monkeypatch, caplog
) -> None:
    storage = LocalStorage(tmp_path / "data")
    service = SourceService(session, storage, confluence_hosts=("wiki.example.test",))
    create_project(session)
    source = service.add_file("project-1", "case.txt", "text/plain", BytesIO(b"text"))

    def fail_delete(relative_path: str) -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr(storage, "delete", fail_delete)
    with (
        caplog.at_level(logging.ERROR, logger="docgen.sources.service"),
        pytest.raises(OSError, match="cleanup failed"),
    ):
        service.delete("project-1", source.id)

    assert session.get(Source, source.id) is None
    record = next(record for record in caplog.records if record.msg.startswith("source_cleanup_failed"))
    assert record.project_id == "project-1"
    assert record.source_id == source.id
    assert record.storage_path == source.storage_path


def test_project_cleanup_failure_is_logged_after_database_commit(
    session: Session, tmp_path, monkeypatch, caplog
) -> None:
    storage = LocalStorage(tmp_path / "data")
    create_project(session)

    def fail_delete_project(project_id: str) -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr(storage, "delete_project", fail_delete_project)
    with (
        caplog.at_level(logging.ERROR, logger="docgen.projects.service"),
        pytest.raises(OSError, match="cleanup failed"),
    ):
        ProjectService(session, storage).delete("project-1")

    assert session.get(Project, "project-1") is None
    record = next(record for record in caplog.records if record.msg.startswith("project_cleanup_failed"))
    assert record.project_id == "project-1"
