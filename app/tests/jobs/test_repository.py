from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from docgen.db import Base
from docgen.export.service import ExportResult
from docgen.formatting.schemas import OutputFormat
from docgen.jobs.models import CheckTargetKind, Job, JobKind, JobStatus
from docgen.jobs.repository import InvalidJobTransition, JobRepository
from docgen.projects.models import Project


def _install_recovery_race(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    concurrent_change: Callable[[], None],
) -> None:
    original_scalars = session.scalars
    original_execute = session.execute
    triggered = False

    def trigger_once() -> None:
        nonlocal triggered
        if not triggered:
            triggered = True
            concurrent_change()

    def racing_scalars(*args: Any, **kwargs: Any) -> Any:
        rows = list(original_scalars(*args, **kwargs))
        trigger_once()
        return iter(rows)

    def racing_execute(*args: Any, **kwargs: Any) -> Any:
        trigger_once()
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(session, "scalars", racing_scalars)
    monkeypatch.setattr(session, "execute", racing_execute)


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add_all([Project(id="p1", name="Первый"), Project(id="p2", name="Второй")])
        session.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def job_repository(session_factory: sessionmaker[Session]) -> JobRepository:
    session = session_factory()
    repository = JobRepository(session, worker_id="worker-main")
    yield repository
    session.close()


def test_enqueue_persists_initial_state(job_repository: JobRepository) -> None:
    job = job_repository.enqueue("p1", JobKind.ASSEMBLE, "use-case")

    assert job.status is JobStatus.QUEUED
    assert job.progress == 0
    assert job.status_message == "Задание поставлено в очередь"
    assert job.error_message is None
    assert job.cancel_requested is False
    assert job.created_at.tzinfo is not None
    assert job.updated_at.tzinfo is not None


def test_check_enqueue_persists_target_source(job_repository: JobRepository) -> None:
    job = job_repository.enqueue(
        "p1",
        JobKind.CHECK,
        "use-case",
        target_source_id="source-1",
    )

    assert job.target_source_id == "source-1"
    assert job.check_target_kind is CheckTargetKind.SOURCE


def test_assemble_enqueue_rejects_target_source(job_repository: JobRepository) -> None:
    with pytest.raises(ValueError, match="сборки"):
        job_repository.enqueue(
            "p1",
            JobKind.ASSEMBLE,
            "use-case",
            target_source_id="source-1",
        )


def test_export_enqueue_has_no_check_target(job_repository: JobRepository) -> None:
    job = job_repository.enqueue(
        "p1",
        JobKind.EXPORT,
        "docgen-light",
        export_format=OutputFormat.HTML,
        requested_document_revision=1,
    )

    assert job.kind is JobKind.EXPORT
    assert job.target_source_id is None
    assert job.check_target_kind is None
    assert job.export_format is OutputFormat.HTML
    assert job.requested_document_revision == 1
    assert job.status is JobStatus.QUEUED


def test_export_enqueue_rejects_target_source(job_repository: JobRepository) -> None:
    with pytest.raises(ValueError, match="экспорта"):
        job_repository.enqueue(
            "p1",
            JobKind.EXPORT,
            "docgen-light",
            target_source_id="source-1",
            export_format=OutputFormat.HTML,
        )


def test_export_enqueue_requires_format(job_repository: JobRepository) -> None:
    with pytest.raises(ValueError, match="формат"):
        job_repository.enqueue(
            "p1", JobKind.EXPORT, "docgen-light", requested_document_revision=1
        )


def test_export_enqueue_requires_revision(job_repository: JobRepository) -> None:
    with pytest.raises(ValueError, match="ревизию"):
        job_repository.enqueue(
            "p1", JobKind.EXPORT, "docgen-light", export_format=OutputFormat.HTML
        )


def test_assemble_enqueue_rejects_requested_document_revision(
    job_repository: JobRepository,
) -> None:
    with pytest.raises(ValueError, match="экспорта"):
        job_repository.enqueue(
            "p1", JobKind.ASSEMBLE, "use-case", requested_document_revision=1
        )


def test_check_enqueue_rejects_requested_document_revision(
    job_repository: JobRepository,
) -> None:
    with pytest.raises(ValueError, match="экспорта"):
        job_repository.enqueue(
            "p1", JobKind.CHECK, "use-case", requested_document_revision=1
        )


def test_assemble_enqueue_rejects_export_format(job_repository: JobRepository) -> None:
    with pytest.raises(ValueError, match="экспорта"):
        job_repository.enqueue(
            "p1", JobKind.ASSEMBLE, "use-case", export_format=OutputFormat.HTML
        )


def test_check_enqueue_rejects_export_format(job_repository: JobRepository) -> None:
    with pytest.raises(ValueError, match="экспорта"):
        job_repository.enqueue(
            "p1", JobKind.CHECK, "use-case", export_format=OutputFormat.HTML
        )


def test_record_export_result_persists_fields_while_job_stays_running(
    job_repository: JobRepository,
) -> None:
    job_repository.enqueue(
        "p1",
        JobKind.EXPORT,
        "docgen-light",
        export_format=OutputFormat.HTML,
        requested_document_revision=1,
    )
    claimed = job_repository.claim_next()
    assert claimed is not None

    result = ExportResult(
        relative_path="projects/p1/exports/document-docgen-light.html",
        filename="document-docgen-light.html",
        media_type="text/html",
        size_bytes=42,
        document_revision=4,
    )
    updated = job_repository.record_export_result(claimed.id, result)

    assert updated.status is JobStatus.RUNNING
    assert updated.export_relative_path == "projects/p1/exports/document-docgen-light.html"
    assert updated.export_filename == "document-docgen-light.html"
    assert updated.export_media_type == "text/html"
    assert updated.export_size_bytes == 42
    assert updated.export_document_revision == 4

    # the terminal mark_succeeded() JobRunner always calls afterward does not
    # know about export fields and must not clobber them
    succeeded = job_repository.mark_succeeded(claimed.id)
    assert succeeded.status is JobStatus.SUCCEEDED
    assert succeeded.export_relative_path == "projects/p1/exports/document-docgen-light.html"
    assert succeeded.export_filename == "document-docgen-light.html"
    assert succeeded.export_media_type == "text/html"
    assert succeeded.export_size_bytes == 42
    assert succeeded.export_document_revision == 4
    assert job_repository.export_paths_for_project("p1") == {
        "projects/p1/exports/document-docgen-light.html"
    }
    assert job_repository.export_paths_for_project("p2") == set()


def test_claim_next_is_fifo_and_marks_running(job_repository: JobRepository) -> None:
    first = job_repository.enqueue("p1", JobKind.ASSEMBLE, "use-case")
    job_repository.enqueue("p2", JobKind.CHECK, "use-case")

    claimed = job_repository.claim_next()

    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.status is JobStatus.RUNNING
    assert claimed.worker_id == "worker-main"
    assert claimed.worker_instance_token == job_repository.instance_token
    assert claimed.lease_expires_at is not None
    assert claimed.started_at is not None


def test_same_slot_replacement_does_not_recover_live_claim(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 8, 10, 12, tzinfo=UTC)
    with session_factory() as first_session:
        first = JobRepository(
            first_session,
            worker_id="stable-slot",
            instance_token="process-a",
            lease_seconds=30,
            now=lambda: now,
        )
        first.enqueue("p1", JobKind.ASSEMBLE, "faq")
        claimed = first.claim_next()
        assert claimed is not None

    with session_factory() as replacement_session:
        replacement = JobRepository(
            replacement_session,
            worker_id="stable-slot",
            instance_token="process-b",
            lease_seconds=30,
            now=lambda: now + timedelta(seconds=29),
        )
        assert replacement.recover_interrupted("Прервано") == 0

    with session_factory() as observer:
        persisted = observer.get(Job, claimed.id)
        assert persisted is not None
        assert persisted.status is JobStatus.RUNNING
        assert persisted.worker_instance_token == "process-a"


def test_expired_claim_is_recovered_and_heartbeat_extends_lease(
    session_factory: sessionmaker[Session],
) -> None:
    current = [datetime(2026, 8, 10, 12, tzinfo=UTC)]
    with session_factory() as worker_session:
        worker = JobRepository(
            worker_session,
            worker_id="stable-slot",
            instance_token="process-a",
            lease_seconds=30,
            now=lambda: current[0],
        )
        worker.enqueue("p1", JobKind.ASSEMBLE, "faq")
        claimed = worker.claim_next()
        assert claimed is not None
        first_expiry = claimed.lease_expires_at
        current[0] += timedelta(seconds=20)
        assert worker.checkpoint(claimed.id) is False
        renewed = worker.get(claimed.id)
        assert renewed is not None
        assert renewed.lease_expires_at == current[0] + timedelta(seconds=30)
        assert renewed.lease_expires_at > first_expiry

    with session_factory() as replacement_session:
        replacement = JobRepository(
            replacement_session,
            worker_id="stable-slot",
            instance_token="process-b",
            lease_seconds=30,
            now=lambda: current[0] + timedelta(seconds=29),
        )
        assert replacement.recover_interrupted("Прервано") == 0

    with session_factory() as expired_session:
        expired = JobRepository(
            expired_session,
            worker_id="stable-slot",
            instance_token="process-c",
            lease_seconds=30,
            now=lambda: current[0] + timedelta(seconds=31),
        )
        assert expired.recover_interrupted("Прервано") == 1


def test_job_warnings_are_deduplicated_and_persisted(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as worker_session:
        repository = JobRepository(
            worker_session, worker_id="worker", instance_token="process"
        )
        repository.enqueue("p1", JobKind.ASSEMBLE, "faq")
        claimed = repository.claim_next()
        assert claimed is not None
        repository.add_warnings(
            claimed.id,
            [
                "Страница 2 не содержит извлекаемого текста",
                "Обработка может занять более пяти минут",
                "Страница 2 не содержит извлекаемого текста",
            ],
        )

    with session_factory() as observer:
        persisted = observer.get(Job, claimed.id)
        assert persisted is not None
        assert persisted.warning_messages == (
            "Страница 2 не содержит извлекаемого текста",
            "Обработка может занять более пяти минут",
        )


def test_claim_fifo_uses_insertion_order_when_timestamps_are_equal(
    session_factory: sessionmaker[Session],
) -> None:
    same_time = datetime(2026, 8, 8, tzinfo=UTC)
    with session_factory() as session:
        session.add_all(
            [
                Job(
                    id="z-inserted-first",
                    project_id="p1",
                    kind=JobKind.ASSEMBLE,
                    template_id="faq",
                    status=JobStatus.QUEUED,
                    progress=0,
                    status_message="Задание поставлено в очередь",
                    cancel_requested=False,
                    created_at=same_time,
                    updated_at=same_time,
                ),
                Job(
                    id="a-inserted-second",
                    project_id="p2",
                    kind=JobKind.CHECK,
                    template_id="use-case",
                    status=JobStatus.QUEUED,
                    progress=0,
                    status_message="Задание поставлено в очередь",
                    cancel_requested=False,
                    created_at=same_time,
                    updated_at=same_time,
                ),
            ]
        )
        session.commit()
        claimed = JobRepository(session, worker_id="worker").claim_next()

    assert claimed is not None
    assert claimed.id == "z-inserted-first"


def test_cancelled_job_is_not_claimed(job_repository: JobRepository) -> None:
    job = job_repository.enqueue("p1", JobKind.ASSEMBLE, "faq")

    job_repository.request_cancel(job.id)

    cancelled = job_repository.get(job.id)
    assert cancelled is not None
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.cancel_requested is True
    assert cancelled.finished_at is not None
    assert job_repository.claim_next() is None


def test_competing_independent_sessions_claim_each_job_at_most_once(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        repository = JobRepository(session, worker_id="producer")
        first = repository.enqueue("p1", JobKind.ASSEMBLE, "faq")
        second = repository.enqueue("p2", JobKind.CHECK, "use-case")

    barrier = Barrier(3)

    def claim(worker_id: str) -> str | None:
        with session_factory() as session:
            barrier.wait()
            job = JobRepository(session, worker_id=worker_id).claim_next()
            return None if job is None else job.id

    with ThreadPoolExecutor(max_workers=3) as executor:
        claimed_ids = list(executor.map(claim, ("worker-1", "worker-2", "worker-3")))

    non_empty_ids = [job_id for job_id in claimed_ids if job_id is not None]
    assert len(non_empty_ids) == 2
    assert set(non_empty_ids) == {first.id, second.id}

    with session_factory() as session:
        persisted = list(session.scalars(select(Job).order_by(Job.created_at, Job.id)))
    assert [job.status for job in persisted] == [JobStatus.RUNNING, JobStatus.RUNNING]
    assert len({job.worker_id for job in persisted}) == 2


def test_only_legal_state_transitions_are_accepted(job_repository: JobRepository) -> None:
    job = job_repository.enqueue("p1", JobKind.ASSEMBLE, "faq")

    with pytest.raises(InvalidJobTransition):
        job_repository.mark_succeeded(job.id)

    claimed = job_repository.claim_next()
    assert claimed is not None
    succeeded = job_repository.mark_succeeded(claimed.id)
    assert succeeded.status is JobStatus.SUCCEEDED
    assert succeeded.progress == 100
    assert succeeded.finished_at is not None

    with pytest.raises(InvalidJobTransition):
        job_repository.mark_failed(job.id, "Повторный сбой")


def test_progress_is_persisted_only_for_running_job(job_repository: JobRepository) -> None:
    job = job_repository.enqueue("p1", JobKind.ASSEMBLE, "faq")

    with pytest.raises(InvalidJobTransition):
        job_repository.update_progress(job.id, 10, "Подготовка")

    claimed = job_repository.claim_next()
    assert claimed is not None
    updated = job_repository.update_progress(claimed.id, 35, "Источники обработаны")
    assert updated.progress == 35
    assert updated.status_message == "Источники обработаны"

    with pytest.raises(ValueError, match="от 0 до 100"):
        job_repository.update_progress(claimed.id, 101, "Некорректно")

    with pytest.raises(TypeError, match="целым числом"):
        job_repository.update_progress(claimed.id, 35.5, "Некорректно")  # type: ignore[arg-type]


def test_running_cancellation_is_a_request_until_runner_observes_it(
    job_repository: JobRepository,
) -> None:
    job_repository.enqueue("p1", JobKind.CHECK, "technical-spec")
    running = job_repository.claim_next()
    assert running is not None

    job_repository.request_cancel(running.id)

    persisted = job_repository.get(running.id)
    assert persisted is not None
    assert persisted.status is JobStatus.RUNNING
    assert persisted.cancel_requested is True


def test_cancellation_cannot_modify_job_that_concurrently_succeeded(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as worker_session:
        worker = JobRepository(worker_session, worker_id="worker")
        worker.enqueue("p1", JobKind.ASSEMBLE, "faq")
        running = worker.claim_next()
        assert running is not None

    with session_factory() as cancelling_session:
        stale = cancelling_session.get(Job, running.id)
        assert stale is not None

        class StaleCancellationRepository(JobRepository):
            def get(self, job_id: str) -> Job | None:
                return stale

        with session_factory() as completing_session:
            JobRepository(
                completing_session,
                worker_id="worker",
                instance_token=worker.instance_token,
            ).mark_succeeded(running.id)
        StaleCancellationRepository(cancelling_session).request_cancel(running.id)

    with session_factory() as observer_session:
        completed = observer_session.get(Job, running.id)
        assert completed is not None
        assert completed.status is JobStatus.SUCCEEDED
        assert completed.cancel_requested is False


def test_cancellation_follows_job_that_is_concurrently_claimed(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as producer_session:
        queued = JobRepository(producer_session).enqueue("p1", JobKind.CHECK, "use-case")

    with session_factory() as cancelling_session:
        stale = cancelling_session.get(Job, queued.id)
        assert stale is not None

        class StaleCancellationRepository(JobRepository):
            def get(self, job_id: str) -> Job | None:
                return stale

        with session_factory() as claiming_session:
            claimed = JobRepository(claiming_session, worker_id="worker").claim_next()
            assert claimed is not None
        StaleCancellationRepository(cancelling_session).request_cancel(queued.id)

    with session_factory() as observer_session:
        running = observer_session.get(Job, queued.id)
        assert running is not None
        assert running.status is JobStatus.RUNNING
        assert running.cancel_requested is True
        assert running.worker_id == "worker"


def test_deleting_project_deletes_its_jobs(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        repository = JobRepository(session, worker_id="worker")
        job = repository.enqueue("p1", JobKind.ASSEMBLE, "faq")
        project = session.get(Project, "p1")
        assert project is not None
        session.delete(project)
        session.commit()

    with session_factory() as session:
        assert session.get(Job, job.id) is None


def test_recovery_fails_only_jobs_owned_by_same_interrupted_worker(
    session_factory: sessionmaker[Session],
) -> None:
    claimed_at = datetime(2026, 8, 10, 12, tzinfo=UTC)
    with session_factory() as first_session:
        first_repository = JobRepository(
            first_session, worker_id="stable-worker", now=lambda: claimed_at
        )
        first_repository.enqueue("p1", JobKind.ASSEMBLE, "faq")
        first_job = first_repository.claim_next()
        assert first_job is not None
    with session_factory() as second_session:
        second_repository = JobRepository(second_session, worker_id="another-worker")
        second_repository.enqueue("p2", JobKind.CHECK, "use-case")
        second_job = second_repository.claim_next()
        assert second_job is not None

    with session_factory() as restarted_session:
        restarted = JobRepository(
            restarted_session,
            worker_id="stable-worker",
            now=lambda: claimed_at + timedelta(seconds=31),
        )
        recovered = restarted.recover_interrupted(
            "Обработка была прервана; запустите её повторно"
        )

    assert recovered == 1
    with session_factory() as session:
        recovered_job = session.get(Job, first_job.id)
        other_job = session.get(Job, second_job.id)
        assert recovered_job is not None
        assert recovered_job.status is JobStatus.FAILED
        assert recovered_job.status_message == "Задание завершилось с ошибкой"
        assert recovered_job.error_message == "Обработка была прервана; запустите её повторно"
        assert recovered_job.finished_at is not None
        assert other_job is not None
        assert other_job.status is JobStatus.RUNNING


def test_recovery_does_not_fail_job_cancelled_after_selection(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed_at = datetime(2026, 8, 10, 12, tzinfo=UTC)
    with session_factory() as worker_session:
        worker = JobRepository(
            worker_session, worker_id="stable-worker", now=lambda: claimed_at
        )
        worker.enqueue("p1", JobKind.ASSEMBLE, "faq")
        running = worker.claim_next()
        assert running is not None

    with session_factory() as recovery_session:
        recovery = JobRepository(
            recovery_session,
            worker_id="stable-worker",
            now=lambda: claimed_at + timedelta(seconds=31),
        )

        def cancel_concurrently() -> None:
            with session_factory() as cancelling_session:
                JobRepository(cancelling_session).request_cancel(running.id)

        _install_recovery_race(recovery_session, monkeypatch, cancel_concurrently)
        recovered = recovery.recover_interrupted(
            "Обработка была прервана; запустите её повторно"
        )

    assert recovered == 1
    with session_factory() as observer_session:
        cancelled = observer_session.get(Job, running.id)
        assert cancelled is not None
        assert cancelled.status is JobStatus.CANCELLED
        assert cancelled.status_message == "Задание отменено"
        assert cancelled.cancel_requested is True
        assert cancelled.error_message is None
        assert cancelled.finished_at is not None


def test_recovery_tolerates_project_and_job_deleted_after_selection(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed_at = datetime(2026, 8, 10, 12, tzinfo=UTC)
    with session_factory() as worker_session:
        worker = JobRepository(
            worker_session, worker_id="stable-worker", now=lambda: claimed_at
        )
        worker.enqueue("p1", JobKind.CHECK, "use-case")
        running = worker.claim_next()
        assert running is not None

    with session_factory() as recovery_session:
        recovery = JobRepository(
            recovery_session,
            worker_id="stable-worker",
            now=lambda: claimed_at + timedelta(seconds=31),
        )

        def delete_project_concurrently() -> None:
            with session_factory() as deleting_session:
                project = deleting_session.get(Project, running.project_id)
                assert project is not None
                deleting_session.delete(project)
                deleting_session.commit()

        _install_recovery_race(recovery_session, monkeypatch, delete_project_concurrently)
        recovered = recovery.recover_interrupted(
            "Обработка была прервана; запустите её повторно"
        )

    assert recovered == 0
    with session_factory() as observer_session:
        assert observer_session.get(Job, running.id) is None


def test_claim_does_not_commit_unrelated_caller_changes(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as producer_session:
        JobRepository(producer_session).enqueue("p1", JobKind.ASSEMBLE, "faq")

    with session_factory() as claiming_session:
        claiming_session.add(Project(id="uncommitted", name="Не фиксировать"))
        claimed = JobRepository(claiming_session, worker_id="worker").claim_next()
        assert claimed is not None
        claiming_session.rollback()

    with session_factory() as observer_session:
        assert observer_session.get(Project, "uncommitted") is None
