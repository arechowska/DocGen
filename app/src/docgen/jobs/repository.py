from __future__ import annotations

from uuid import uuid4

from sqlalchemy import literal_column, select, update
from sqlalchemy.orm import Session

from docgen.models import utc_now

from .models import Job, JobKind, JobStatus

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


class JobRepository:
    def __init__(self, session: Session, *, worker_id: str | None = None) -> None:
        self._session = session
        self._worker_id = worker_id or str(uuid4())

    @property
    def worker_id(self) -> str:
        return self._worker_id

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
        self._session.rollback()
        self._session.connection().exec_driver_sql("BEGIN IMMEDIATE")
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
        if kind is JobKind.ASSEMBLE and target_source_id is not None:
            raise ValueError("Для задания сборки нельзя указывать документ проверки")
        return Job(
            project_id=project_id,
            kind=kind,
            template_id=template_id,
            target_source_id=target_source_id,
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

            now = utc_now()
            job.status = JobStatus.RUNNING
            job.status_message = _RUNNING_MESSAGE
            job.worker_id = self._worker_id
            job.started_at = now
            job.updated_at = now
            claim_session.commit()
            claim_session.expunge(job)
            return job

    def request_cancel(self, job_id: str) -> None:
        now = utc_now()
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
            updated_at=utc_now(),
        )

    def mark_succeeded(self, job_id: str) -> Job:
        now = utc_now()
        return self._atomic_owned_update(
            job_id,
            status=JobStatus.SUCCEEDED,
            progress=100,
            status_message=_SUCCEEDED_MESSAGE,
            error_message=None,
            finished_at=now,
            updated_at=now,
        )

    def mark_failed(self, job_id: str, error_message: str) -> Job:
        now = utc_now()
        return self._atomic_owned_update(
            job_id,
            status=JobStatus.FAILED,
            status_message=_FAILED_MESSAGE,
            error_message=error_message,
            finished_at=now,
            updated_at=now,
        )

    def mark_cancelled(self, job_id: str) -> Job:
        now = utc_now()
        return self._atomic_owned_update(
            job_id,
            allow_cancel_requested=True,
            status=JobStatus.CANCELLED,
            status_message=_CANCELLED_MESSAGE,
            cancel_requested=True,
            finished_at=now,
            updated_at=now,
        )

    def recover_interrupted(self, error_message: str) -> int:
        now = utc_now()
        failed_result = self._session.execute(
            update(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                Job.worker_id == self._worker_id,
                Job.cancel_requested.is_(False),
            )
            .values(
                status=JobStatus.FAILED,
                status_message=_FAILED_MESSAGE,
                error_message=error_message,
                finished_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        cancelled_result = self._session.execute(
            update(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                Job.worker_id == self._worker_id,
                Job.cancel_requested.is_(True),
            )
            .values(
                status=JobStatus.CANCELLED,
                status_message=_CANCELLED_MESSAGE,
                error_message=None,
                finished_at=now,
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
]
