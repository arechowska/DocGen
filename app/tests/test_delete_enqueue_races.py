from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy.orm import Session, sessionmaker

import docgen.jobs.repository as jobs_repository_module
import docgen.projects.service as projects_service_module
from docgen.db import build_session_factory, initialize_database
from docgen.jobs.models import JobKind
from docgen.jobs.repository import (
    ActiveProjectJobExists,
    JobRepository,
    JobTargetUnavailable,
)
from docgen.projects.repository import ProjectRepository
from docgen.projects.service import ProjectService
from docgen.sources.service import SourceService
from docgen.sources.storage import LocalStorage


@pytest.fixture
def race_database(tmp_path: Path) -> tuple[sessionmaker[Session], LocalStorage, str, str]:
    factory = build_session_factory(f"sqlite:///{tmp_path / 'race.db'}")
    engine = factory.kw["bind"]
    initialize_database(engine)
    storage = LocalStorage(tmp_path / "data")
    with factory() as session:
        project = ProjectRepository(session).create("Гонка")
        session.commit()
        source = SourceService(
            session,
            storage,
            confluence_hosts=("wiki.internal",),
        ).add_file(project.id, "case.txt", "text/plain", BytesIO(b"case"))
    yield factory, storage, project.id, source.id
    engine.dispose()


def test_enqueue_winning_source_delete_race_leaves_valid_active_target(
    race_database: tuple[sessionmaker[Session], LocalStorage, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, storage, project_id, source_id = race_database
    lock_acquired = Event()
    release_lock = Event()
    delete_started = Event()
    original_begin = jobs_repository_module.begin_sqlite_writer_transaction

    def gated_begin(session: Session) -> None:
        original_begin(session)
        lock_acquired.set()
        assert release_lock.wait(5)

    monkeypatch.setattr(
        jobs_repository_module,
        "begin_sqlite_writer_transaction",
        gated_begin,
    )

    def enqueue():
        with factory() as session:
            return JobRepository(session).enqueue_if_project_idle(
                project_id,
                JobKind.CHECK,
                "use-case",
                target_source_id=source_id,
            )

    def delete() -> None:
        delete_started.set()
        with factory() as session:
            SourceService(
                session,
                storage,
                confluence_hosts=("wiki.internal",),
            ).delete(project_id, source_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        enqueue_future = pool.submit(enqueue)
        assert lock_acquired.wait(5)
        delete_future = pool.submit(delete)
        assert delete_started.wait(5)
        release_lock.set()
        job = enqueue_future.result(timeout=5)
        with pytest.raises(ActiveProjectJobExists):
            delete_future.result(timeout=5)

    with factory() as session:
        assert JobRepository(session).get(job.id).target_source_id == source_id
    with factory.kw["bind"].connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_project_delete_winning_enqueue_race_returns_safe_missing_target(
    race_database: tuple[sessionmaker[Session], LocalStorage, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, storage, project_id, source_id = race_database
    lock_acquired = Event()
    release_lock = Event()
    enqueue_started = Event()
    original_begin = projects_service_module.begin_sqlite_writer_transaction

    def gated_begin(session: Session) -> None:
        original_begin(session)
        lock_acquired.set()
        assert release_lock.wait(5)

    monkeypatch.setattr(
        projects_service_module,
        "begin_sqlite_writer_transaction",
        gated_begin,
    )

    def delete() -> None:
        with factory() as session:
            ProjectService(session, storage).delete(project_id)

    def enqueue():
        enqueue_started.set()
        with factory() as session:
            return JobRepository(session).enqueue_if_project_idle(
                project_id,
                JobKind.CHECK,
                "use-case",
                target_source_id=source_id,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        delete_future = pool.submit(delete)
        assert lock_acquired.wait(5)
        enqueue_future = pool.submit(enqueue)
        assert enqueue_started.wait(5)
        release_lock.set()
        delete_future.result(timeout=5)
        with pytest.raises(JobTargetUnavailable):
            enqueue_future.result(timeout=5)

    with factory.kw["bind"].connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
