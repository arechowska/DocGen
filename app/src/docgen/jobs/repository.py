from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import literal_column, select, update
from sqlalchemy.orm import Session

from docgen.db import begin_sqlite_writer_transaction
from docgen.documents.models import ProjectArtifact
from docgen.models import Project, Source, utc_now

from .models import CheckTargetKind, Job, JobKind, JobStatus

_QUEUED_MESSAGE = "Задание поставлено в очередь"
_RUNNING_MESSAGE = "Задание выполняется"
_SUCCEEDED_MESSAGE = "Задание завершено"
_FAILED_MESSAGE = "Задание завершилось с ошибкой"
_CANCELLED_MESSAGE = "Задание отменено"


class InvalidJobTransition(RuntimeError):
    """Raised when a job state change violates the persisted state machine."""


class JobCancellationRequested(InvalidJobTransition):
    """Raised when cancellation wins a concurrent state update."""


class JobNotFound(LookupError):
    """Raised when a job disappeared, for example with its deleted project."""


class ActiveProjectJobExists(RuntimeError):
    """Raised when a project already has a queued or running job."""


class JobTargetUnavailable(LookupError):
    """Raised when enqueue revalidation loses its project or explicit target."""


class JobRepository:
    def __init__(
        self,
        session: Session,
        *,
        worker_id: str | None = None,
        instance_token: str | None = None,
        lease_seconds: int = 30,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("Срок аренды worker должен быть положительным")
        self._session = session
        self._worker_id = worker_id or str(uuid4())
        self._instance_token = instance_token or str(uuid4())
        self._lease_seconds = lease_seconds
        self._now = now

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def instance_token(self) -> str:
        return self._instance_token

    @property
    def lease_seconds(self) -> int:
        return self._lease_seconds

    def enqueue(
        self,
        project_id: str,
        kind: JobKind,
        template_id: str,
        *,
        target_source_id: str | None = None,
    ) -> Job:
        job = self._new_job(project_id, kind, template_id, target_source_id)
        self._session.add(job)
        self._commit()
        return job

    def enqueue_if_project_idle(
        self,
        project_id: str,
        kind: JobKind,
        template_id: str,
        *,
        target_source_id: str | None = None,
    ) -> Job:
        # Route validation performs reads first. End that deferred transaction,
        # then reserve the SQLite writer lock before checking and inserting so
        # two concurrent requests cannot both observe an idle project.
        begin_sqlite_writer_transaction(self._session)
        try:
            active_job_id = self._session.scalar(
                select(Job.id)
                .where(
                    Job.project_id == project_id,
                    Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
                )
                .limit(1)
            )
            if active_job_id is not None:
                raise ActiveProjectJobExists
            if self._session.scalar(
                select(Project.id).where(Project.id == project_id).limit(1)
            ) is None:
                raise JobTargetUnavailable("Проект не найден")
            if target_source_id is not None and self._session.scalar(
                select(Source.id)
                .where(
                    Source.id == target_source_id,
                    Source.project_id == project_id,
                )
                .limit(1)
            ) is None:
                raise JobTargetUnavailable("Документ для проверки не найден")
            job = self._new_job(project_id, kind, template_id, target_source_id)
            self._session.add(job)
            self._session.commit()
            return job
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _new_job(
        project_id: str,
        kind: JobKind,
        template_id: str,
        target_source_id: str | None,
    ) -> Job:
        if kind in (JobKind.ASSEMBLE, JobKind.EXPORT) and target_source_id is not None:
            raise ValueError("Для задания сборки или экспорта нельзя указывать документ проверки")
        return Job(
            project_id=project_id,
            kind=kind,
            template_id=template_id,
            target_source_id=target_source_id,
            check_target_kind=(
                None
                if kind in (JobKind.ASSEMBLE, JobKind.EXPORT)
                else (
                    CheckTargetKind.SOURCE
                    if target_source_id is not None
                    else CheckTargetKind.CURRENT
                )
            ),
            status=JobStatus.QUEUED,
            progress=0,
            status_message=_QUEUED_MESSAGE,
            cancel_requested=False,
        )

    def get(self, job_id: str) -> Job | None:
        return self._session.get(Job, job_id, populate_existing=True)

    def get_active_for_project(self, project_id: str) -> Job | None:
        return self._session.scalar(
            select(Job)
            .where(
                Job.project_id == project_id,
                Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
            )
            .order_by(Job.created_at, Job.id)
            .limit(1)
        )

    def claim_next(self) -> Job | None:
        # A RESERVED lock is taken before reading the head of the queue. This
        # serializes competing SQLite workers until the selected row is marked
        # running and committed.
        with Session(bind=self._session.get_bind(), expire_on_commit=False) as claim_session:
            claim_session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = claim_session.scalar(
                select(Job)
                .where(
                    Job.status == JobStatus.QUEUED,
                    Job.cancel_requested.is_(False),
                )
                .order_by(literal_column("rowid"))
                .limit(1)
            )
            if job is None:
                claim_session.commit()
                return None

            now = self._now()
            job.status = JobStatus.RUNNING
            job.status_message = _RUNNING_MESSAGE
            job.worker_id = self._worker_id
            job.worker_instance_token = self._instance_token
            job.lease_expires_at = self._lease_expiry(now)
            job.started_at = now
            job.updated_at = now
            claim_session.commit()
            claim_session.expunge(job)
            return job

    def request_cancel(self, job_id: str) -> None:
        now = self._now()
        queued_result = self._session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
            .values(
                cancel_requested=True,
                status=JobStatus.CANCELLED,
                status_message=_CANCELLED_MESSAGE,
                finished_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if queued_result.rowcount == 1:
            self._commit()
            return

        running_result = self._session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.RUNNING)
            .values(cancel_requested=True, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if running_result.rowcount == 1:
            self._commit()
            return

        self._session.rollback()
        self._required(job_id)
        self._session.rollback()

    def is_cancel_requested(self, job_id: str) -> bool:
        job = self._required(job_id)
        self._session.refresh(job)
        return job.cancel_requested

    def checkpoint(self, job_id: str) -> bool:
        now = self._now()
        job = self._atomic_owned_update(
            job_id,
            allow_cancel_requested=True,
            lease_expires_at=self._lease_expiry(now),
            updated_at=now,
        )
        return job.cancel_requested

    def heartbeat(self, job_id: str) -> bool:
        """Renew a claim in an isolated session without publishing workflow changes."""
        with Session(bind=self._session.get_bind(), expire_on_commit=False) as session:
            repository = JobRepository(
                session,
                worker_id=self._worker_id,
                instance_token=self._instance_token,
                lease_seconds=self._lease_seconds,
                now=self._now,
            )
            return repository.checkpoint(job_id)

    def add_warnings(self, job_id: str, warnings: Iterable[str]) -> Job:
        job = self._required(job_id)
        if (
            job.status is not JobStatus.RUNNING
            or job.worker_id != self._worker_id
            or job.worker_instance_token != self._instance_token
        ):
            raise self._transition_error(job)
        deduplicated = list(job.warning_messages)
        seen = set(deduplicated)
        for warning in warnings:
            normalized = " ".join(warning.split())
            if normalized and normalized not in seen:
                seen.add(normalized)
                deduplicated.append(normalized)
        now = self._now()
        return self._atomic_owned_update(
            job_id,
            allow_cancel_requested=True,
            warnings_json=json.dumps(deduplicated, ensure_ascii=False),
            lease_expires_at=self._lease_expiry(now),
            updated_at=now,
        )

    def update_progress(self, job_id: str, progress: int, status_message: str) -> Job:
        if isinstance(progress, bool) or not isinstance(progress, int):
            raise TypeError("Прогресс должен быть целым числом")
        if not 0 <= progress <= 100:
            raise ValueError("Прогресс должен быть от 0 до 100")
        job = self._required(job_id)
        if job.status is not JobStatus.RUNNING or job.worker_id != self._worker_id:
            raise self._transition_error(job)
        if progress < job.progress:
            raise ValueError("Прогресс не может уменьшаться")
        return self._atomic_owned_update(
            job_id,
            progress=progress,
            status_message=status_message,
            lease_expires_at=self._lease_expiry(self._now()),
            updated_at=self._now(),
        )

    def mark_succeeded(self, job_id: str) -> Job:
        now = self._now()
        job = self._required(job_id)
        artifact = self._session.get(ProjectArtifact, job.project_id)
        document_revision = (
            artifact.document_revision
            if artifact is not None and artifact.document_json is not None
            else None
        )
        report_revision = (
            artifact.report_revision
            if job.kind is JobKind.CHECK
            and artifact is not None
            and artifact.report_json is not None
            else None
        )
        report_generation = (
            artifact.report_generation
            if report_revision is not None and artifact is not None
            else None
        )
        return self._atomic_owned_update(
            job_id,
            status=JobStatus.SUCCEEDED,
            progress=100,
            status_message=_SUCCEEDED_MESSAGE,
            error_message=None,
            lease_expires_at=None,
            result_document_revision=document_revision,
            result_report_revision=report_revision,
            result_report_generation=report_generation,
            finished_at=now,
            updated_at=now,
        )

    def mark_failed(
        self,
        job_id: str,
        error_message: str,
        *,
        user_message: str | None = None,
    ) -> Job:
        now = self._now()
        return self._atomic_owned_update(
            job_id,
            status=JobStatus.FAILED,
            status_message=user_message or _FAILED_MESSAGE,
            error_message=error_message,
            lease_expires_at=None,
            finished_at=now,
            updated_at=now,
        )

    def mark_cancelled(self, job_id: str) -> Job:
        now = self._now()
        return self._atomic_owned_update(
            job_id,
            allow_cancel_requested=True,
            status=JobStatus.CANCELLED,
            status_message=_CANCELLED_MESSAGE,
            cancel_requested=True,
            lease_expires_at=None,
            finished_at=now,
            updated_at=now,
        )

    def recover_interrupted(self, error_message: str) -> int:
        now = self._now()
        failed_result = self._session.execute(
            update(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                Job.worker_id == self._worker_id,
                (Job.lease_expires_at.is_(None) | (Job.lease_expires_at <= now)),
                Job.cancel_requested.is_(False),
            )
            .values(
                status=JobStatus.FAILED,
                status_message=_FAILED_MESSAGE,
                error_message=error_message,
                finished_at=now,
                lease_expires_at=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        cancelled_result = self._session.execute(
            update(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                Job.worker_id == self._worker_id,
                (Job.lease_expires_at.is_(None) | (Job.lease_expires_at <= now)),
                Job.cancel_requested.is_(True),
            )
            .values(
                status=JobStatus.CANCELLED,
                status_message=_CANCELLED_MESSAGE,
                error_message=None,
                finished_at=now,
                lease_expires_at=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        self._commit()
        return failed_result.rowcount + cancelled_result.rowcount

    def discard_pending_changes(self) -> None:
        self._session.rollback()

    def _required(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise JobNotFound("Задание не найдено")
        return job

    def _atomic_owned_update(
        self,
        job_id: str,
        *,
        allow_cancel_requested: bool = False,
        **values: object,
    ) -> Job:
        conditions = [
            Job.id == job_id,
            Job.status == JobStatus.RUNNING,
            Job.worker_id == self._worker_id,
            Job.worker_instance_token == self._instance_token,
        ]
        if not allow_cancel_requested:
            conditions.append(Job.cancel_requested.is_(False))
        result = self._session.execute(
            update(Job)
            .where(*conditions)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            self._commit()
            return self._required(job_id)

        self._session.rollback()
        job = self._required(job_id)
        if job.status is JobStatus.RUNNING and job.cancel_requested:
            raise JobCancellationRequested("Запрошена отмена задания")
        raise self._transition_error(job)

    def _transition_error(self, job: Job) -> InvalidJobTransition:
        if job.status is not JobStatus.RUNNING:
            return InvalidJobTransition(
                f"Переход из состояния {job.status.value} для задания {job.id} запрещён"
            )
        return InvalidJobTransition("Задание выполняется другим worker")

    def _lease_expiry(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self._lease_seconds)

    def _commit(self) -> None:
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise


__all__ = [
    "ActiveProjectJobExists",
    "InvalidJobTransition",
    "JobCancellationRequested",
    "JobNotFound",
    "JobRepository",
    "JobTargetUnavailable",
]
