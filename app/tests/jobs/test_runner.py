from __future__ import annotations

import signal
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from docgen.config import Settings
from docgen.db import Base
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import CheckReport, WorkingDocument
from docgen.export.protocol import ExportError
from docgen.formatting.schemas import OutputFormat
from docgen.jobs import worker as worker_module
from docgen.jobs.models import Job, JobKind, JobStatus
from docgen.jobs.repository import JobRepository
from docgen.jobs.runner import JobRunner, UserSafeJobError
from docgen.jobs.worker import install_signal_handlers, resolve_worker_id, run_worker
from docgen.projects.models import Project
from docgen.templates_catalog.loader import TemplateConfigurationError


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'runner.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(Project(id="p1", name="Проект"))
        session.commit()
    yield factory
    engine.dispose()


def enqueue(
    session_factory: sessionmaker[Session], kind: JobKind = JobKind.ASSEMBLE
) -> Job:
    export_format = OutputFormat.HTML if kind is JobKind.EXPORT else None
    with session_factory() as session:
        return JobRepository(session, worker_id="producer").enqueue(
            "p1", kind, "use-case", export_format=export_format
        )


def persisted(session_factory: sessionmaker[Session], job_id: str) -> Job:
    with session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def test_run_once_returns_false_when_queue_is_empty(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        runner = JobRunner(JobRepository(session, worker_id="worker"), {})
        assert runner.run_once() is False


def test_empty_workflow_mapping_does_not_claim_queued_job(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory)
    with session_factory() as session:
        assert JobRunner(JobRepository(session, worker_id="worker"), {}).run_once() is False

    assert persisted(session_factory, job.id).status is JobStatus.QUEUED


def test_run_once_commits_progress_between_workflow_stages(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory)
    observed_progress: list[int] = []

    def workflow(claimed: Job, progress: Any) -> None:
        progress(10, "Шаблон загружен")
        observed_progress.append(persisted(session_factory, claimed.id).progress)
        progress(70, "Модель обработала источники")
        observed_progress.append(persisted(session_factory, claimed.id).progress)

    with session_factory() as session:
        runner = JobRunner(
            JobRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: workflow},
        )
        assert runner.run_once() is True

    assert observed_progress == [10, 70]
    completed = persisted(session_factory, job.id)
    assert completed.status is JobStatus.SUCCEEDED
    assert completed.progress == 100


def test_same_runner_processes_multiple_jobs_sequentially(
    session_factory: sessionmaker[Session],
) -> None:
    first = enqueue(session_factory)
    second = enqueue(session_factory)
    with session_factory() as session:
        runner = JobRunner(
            JobRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: lambda claimed, progress: None},
        )
        assert runner.run_once() is True
        assert runner.run_once() is True

    assert persisted(session_factory, first.id).status is JobStatus.SUCCEEDED
    assert persisted(session_factory, second.id).status is JobStatus.SUCCEEDED


def test_lost_terminal_transition_does_not_stop_worker_loop(
    session_factory: sessionmaker[Session],
) -> None:
    first = enqueue(session_factory)
    second = enqueue(session_factory)

    class LostTransitionRepository(JobRepository):
        def mark_succeeded(self, job_id: str) -> Job:
            if job_id == first.id:
                with session_factory() as competing_session:
                    competing = competing_session.get(Job, job_id)
                    assert competing is not None
                    competing.status = JobStatus.FAILED
                    competing.error_message = "Аренда утрачена"
                    competing_session.commit()
            return super().mark_succeeded(job_id)

    with session_factory() as session:
        runner = JobRunner(
            LostTransitionRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: lambda claimed, progress: None},
        )
        assert runner.run_once() is True
        assert runner.run_once() is True

    assert persisted(session_factory, first.id).status is JobStatus.FAILED
    assert persisted(session_factory, second.id).status is JobStatus.SUCCEEDED


def test_workflow_warnings_are_persisted_before_terminal_state(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory)

    def workflow(claimed: Job, progress: Any) -> None:
        progress.report_warnings(
            [
                "Страница 3 не содержит извлекаемого текста",
                "Страница 3 не содержит извлекаемого текста",
            ]
        )

    with session_factory() as session:
        JobRunner(
            JobRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: workflow},
        ).run_once()

    assert persisted(session_factory, job.id).warning_messages == (
        "Страница 3 не содержит извлекаемого текста",
    )


def test_background_heartbeat_keeps_claim_live_during_long_model_call(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory)
    recovered: list[int] = []

    def workflow(claimed: Job, progress: Any) -> None:
        del progress
        time.sleep(1.2)
        with session_factory() as replacement_session:
            replacement = JobRepository(
                replacement_session,
                worker_id="stable-slot",
                instance_token="replacement-process",
                lease_seconds=1,
            )
            recovered.append(replacement.recover_interrupted("Прервано"))

    with session_factory() as session:
        runner = JobRunner(
            JobRepository(
                session,
                worker_id="stable-slot",
                instance_token="original-process",
                lease_seconds=1,
            ),
            {JobKind.ASSEMBLE: workflow},
        )
        assert runner.run_once() is True

    assert recovered == [0]
    assert persisted(session_factory, job.id).status is JobStatus.SUCCEEDED


def test_cancellation_is_checked_before_next_external_stage(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory)
    external_calls: list[str] = []

    def workflow(claimed: Job, progress: Any) -> None:
        progress(10, "Извлечение")
        external_calls.append("extractor")
        with session_factory() as cancelling_session:
            JobRepository(cancelling_session).request_cancel(claimed.id)
        progress(70, "Вызов модели")
        external_calls.append("model")

    with session_factory() as session:
        runner = JobRunner(
            JobRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: workflow},
        )
        assert runner.run_once() is True

    assert external_calls == ["extractor"]
    cancelled = persisted(session_factory, job.id)
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.cancel_requested is True


def test_cancellation_checkpoint_does_not_publish_duplicate_progress(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory)
    external_calls: list[str] = []

    def workflow(claimed: Job, progress: Any) -> None:
        progress(35, "Нормализация")
        with session_factory() as cancelling_session:
            JobRepository(cancelling_session).request_cancel(claimed.id)
        progress.checkpoint()
        external_calls.append("model")

    with session_factory() as session:
        JobRunner(
            JobRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: workflow},
        ).run_once()

    assert external_calls == []
    cancelled = persisted(session_factory, job.id)
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.progress == 35


def test_job_time_budget_accepts_exact_deadline_and_rejects_next_stage(
    session_factory: sessionmaker[Session],
) -> None:
    accepted_job = enqueue(session_factory)
    rejected_job = enqueue(session_factory)

    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    external_calls: list[str] = []

    def workflow(claimed: Job, progress: Any) -> None:
        if claimed.id == accepted_job.id:
            clock.value = 5.0
            progress.checkpoint()
            external_calls.append("accepted")
            clock.value = 0.0
            return
        clock.value = 5.001
        progress.checkpoint()
        external_calls.append("must-not-run")

    with session_factory() as session:
        runner = JobRunner(
            JobRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: workflow},
            max_job_seconds=5,
            monotonic=clock,
        )
        assert runner.run_once() is True
        assert runner.run_once() is True

    assert external_calls == ["accepted"]
    assert persisted(session_factory, accepted_job.id).status is JobStatus.SUCCEEDED
    rejected = persisted(session_factory, rejected_job.id)
    assert rejected.status is JobStatus.FAILED
    assert rejected.error_message == "Превышено максимальное время обработки задания"


def test_runner_preserves_explicitly_user_safe_error(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory)

    def workflow(claimed: Job, progress: Any) -> None:
        raise UserSafeJobError("Не удалось проверить документ")

    with session_factory() as session:
        JobRunner(
            JobRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: workflow},
        ).run_once()

    failed = persisted(session_factory, job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.error_message == "Не удалось проверить документ"


def test_runner_preserves_export_engine_error(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory, JobKind.EXPORT)

    def workflow(claimed: Job, progress: Any) -> None:
        raise ExportError("Документ изменён; запустите экспорт повторно")

    with session_factory() as session:
        JobRunner(
            JobRepository(session, worker_id="worker"),
            {JobKind.EXPORT: workflow},
        ).run_once()

    failed = persisted(session_factory, job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.error_message == "Документ изменён; запустите экспорт повторно"


def test_runner_does_not_persist_internal_exception_details(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory)

    def workflow(claimed: Job, progress: Any) -> None:
        raise RuntimeError("token=very-secret internal path C:/private")

    with session_factory() as session:
        JobRunner(
            JobRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: workflow},
        ).run_once()

    failed = persisted(session_factory, job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.error_message == "Не удалось выполнить задание"
    assert "secret" not in failed.error_message


def test_runner_sanitizes_template_configuration_details(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory)

    def workflow(claimed: Job, progress: Any) -> None:
        raise TemplateConfigurationError("Каталог C:/private/templates: raw yaml secret")

    with session_factory() as session:
        JobRunner(
            JobRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: workflow},
        ).run_once()

    assert persisted(session_factory, job.id).error_message == "Не удалось выполнить задание"


def test_cancel_racing_final_success_wins_atomically(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory)

    class RaceRepository(JobRepository):
        def mark_succeeded(self, job_id: str) -> Job:
            with session_factory() as cancelling_session:
                JobRepository(cancelling_session).request_cancel(job_id)
            return super().mark_succeeded(job_id)

    with session_factory() as session:
        JobRunner(
            RaceRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: lambda claimed, progress: None},
        ).run_once()

    raced = persisted(session_factory, job.id)
    assert raced.status is JobStatus.CANCELLED
    assert raced.progress == 0


def test_cancel_after_final_progress_discards_uncommitted_artifacts(
    session_factory: sessionmaker[Session],
) -> None:
    previous_document = WorkingDocument(title="Предыдущий документ", template_id="use-case")
    previous_report = CheckReport(template_id="use-case", unchecked_rules=["previous"])
    replacement_document = WorkingDocument(title="Новый документ", template_id="use-case")
    replacement_report = CheckReport(template_id="use-case", unchecked_rules=["replacement"])
    with session_factory() as artifact_session:
        documents = DocumentRepository(artifact_session)
        documents.save_document("p1", previous_document)
        documents.save_report("p1", previous_report)
        artifact_session.commit()
    job = enqueue(session_factory, JobKind.CHECK)

    with session_factory() as worker_session:
        worker_documents = DocumentRepository(worker_session)

        def workflow(claimed: Job, progress: Any) -> None:
            progress(100, "Сохранение отчёта")
            with session_factory() as cancelling_session:
                JobRepository(cancelling_session).request_cancel(claimed.id)
            worker_documents.save_document(claimed.project_id, replacement_document)
            worker_documents.save_report(claimed.project_id, replacement_report)

        JobRunner(
            JobRepository(worker_session, worker_id="worker"),
            {JobKind.CHECK: workflow},
        ).run_once()

    cancelled = persisted(session_factory, job.id)
    assert cancelled.status is JobStatus.CANCELLED
    with session_factory() as artifact_session:
        documents = DocumentRepository(artifact_session)
        assert documents.get_document("p1") == previous_document
        assert documents.get_report("p1") == previous_report


def test_cancel_racing_progress_update_wins_atomically(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory)

    class RaceRepository(JobRepository):
        def update_progress(self, job_id: str, progress: int, status_message: str) -> Job:
            with session_factory() as cancelling_session:
                JobRepository(cancelling_session).request_cancel(job_id)
            return super().update_progress(job_id, progress, status_message)

    def workflow(claimed: Job, progress: Any) -> None:
        progress(70, "Вызов модели")

    with session_factory() as session:
        JobRunner(
            RaceRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: workflow},
        ).run_once()

    raced = persisted(session_factory, job.id)
    assert raced.status is JobStatus.CANCELLED
    assert raced.progress == 0


def test_project_deletion_while_running_does_not_crash_worker(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory)

    def workflow(claimed: Job, progress: Any) -> None:
        with session_factory() as deleting_session:
            project = deleting_session.get(Project, claimed.project_id)
            assert project is not None
            deleting_session.delete(project)
            deleting_session.commit()

    with session_factory() as session:
        processed = JobRunner(
            JobRepository(session, worker_id="worker"),
            {JobKind.ASSEMBLE: workflow},
        ).run_once()

    assert processed is True
    with session_factory() as session:
        assert session.get(Job, job.id) is None


def test_runner_supports_workflow_objects_with_run_method(
    session_factory: sessionmaker[Session],
) -> None:
    job = enqueue(session_factory, JobKind.CHECK)

    class Workflow:
        def run(self, claimed: Job, progress: Any) -> None:
            progress(90)

    with session_factory() as session:
        JobRunner(
            JobRepository(session, worker_id="worker"),
            {JobKind.CHECK: Workflow()},
        ).run_once()

    assert persisted(session_factory, job.id).status is JobStatus.SUCCEEDED


def test_worker_recovers_then_polls_at_half_second_interval() -> None:
    calls: list[str] = []

    class Runner:
        def recover_interrupted(self) -> int:
            calls.append("recover")
            return 0

        def run_once(self) -> bool:
            calls.append("run")
            return False

    class StopEvent:
        def is_set(self) -> bool:
            return False

        def wait(self, timeout: float) -> bool:
            calls.append(f"wait:{timeout}")
            return True

    run_worker(Runner(), StopEvent())  # type: ignore[arg-type]

    assert calls == ["recover", "run", "wait:0.5"]


def test_replacement_worker_recovers_claim_after_lease_expires_while_polling(
    session_factory: sessionmaker[Session],
) -> None:
    current = [datetime(2026, 8, 10, 12, tzinfo=UTC)]
    with session_factory() as first_session:
        first = JobRepository(
            first_session,
            worker_id="stable-slot",
            instance_token="process-a",
            lease_seconds=30,
            now=lambda: current[0],
        )
        job = first.enqueue("p1", JobKind.ASSEMBLE, "faq")
        assert first.claim_next() is not None

    current[0] += timedelta(seconds=29)

    class StopEvent:
        waits = 0

        def is_set(self) -> bool:
            return False

        def wait(self, timeout: float) -> bool:
            assert timeout == 0
            self.waits += 1
            current[0] += timedelta(seconds=2)
            return self.waits == 2

    with session_factory() as replacement_session:
        runner = JobRunner(
            JobRepository(
                replacement_session,
                worker_id="stable-slot",
                instance_token="process-b",
                lease_seconds=30,
                now=lambda: current[0],
            ),
            {},
        )
        run_worker(runner, StopEvent(), poll_interval=0)

    assert persisted(session_factory, job.id).status is JobStatus.FAILED


def test_sigterm_handler_requests_worker_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: dict[signal.Signals, Any] = {}
    monkeypatch.setattr(signal, "signal", handlers.__setitem__)
    stop_event = Event()

    install_signal_handlers(stop_event)
    handlers[signal.SIGTERM](signal.SIGTERM, None)

    assert stop_event.is_set()


def test_worker_requires_explicit_stable_slot_id() -> None:
    with pytest.raises(RuntimeError, match="DOCGEN_WORKER_ID"):
        resolve_worker_id({})

    assert resolve_worker_id({"DOCGEN_WORKER_ID": " worker-slot-1 "}) == "worker-slot-1"


def test_production_worker_processes_registered_workflow_instead_of_leaving_job_queued(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'production-worker.db'}",
        data_dir=tmp_path / "data",
        worker_lease_seconds=17,
        local_text_base_url=None,
        local_text_model=None,
        local_vision_base_url=None,
        local_vision_model=None,
    )
    factory = sessionmaker(
        create_engine(settings.database_url, connect_args={"check_same_thread": False}),
        expire_on_commit=False,
    )
    Base.metadata.create_all(factory.kw["bind"])
    with factory() as session:
        session.add(Project(id="p1", name="Проект"))
        session.commit()
        job = JobRepository(session, worker_id="producer").enqueue(
            "p1", JobKind.ASSEMBLE, "use-case"
        )

    def run_one_job(runner: JobRunner, stop_event: Any) -> None:
        del stop_event
        assert runner._repository.worker_id == "worker-slot-test"
        assert UUID(runner._repository.instance_token)
        assert runner._repository.instance_token != runner._repository.worker_id
        assert runner._repository.lease_seconds == 17
        assert runner._max_job_seconds == settings.max_job_seconds
        assert runner.run_once() is True

    monkeypatch.setattr(worker_module, "Settings", lambda: settings)
    monkeypatch.setattr(worker_module, "run_worker", run_one_job)
    monkeypatch.setenv("DOCGEN_WORKER_ID", "worker-slot-test")

    worker_module.main()

    with factory() as session:
        persisted_job = session.get(Job, job.id)
        assert persisted_job is not None
        assert persisted_job.status is JobStatus.FAILED
        assert persisted_job.error_message == "Локальные модели не настроены"
    factory.kw["bind"].dispose()
