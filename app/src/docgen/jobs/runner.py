from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from docgen.ai.client import ModelConfigurationError, ModelError
from docgen.extraction.registry import ExtractionError
from docgen.workflows.normalize import PageLimitExceeded

from .models import Job, JobKind
from .repository import JobCancellationRequested, JobNotFound, JobRepository

_GENERIC_ERROR_MESSAGE = "Не удалось выполнить задание"
_INTERRUPTED_ERROR_MESSAGE = "Обработка была прервана; запустите её повторно"


class UserSafeJobError(RuntimeError):
    """A workflow failure whose message is safe to show and persist."""


class ProgressSink(Protocol):
    def __call__(self, progress: int, status_message: str | None = None) -> None: ...


class JobWorkflow(Protocol):
    def run(self, job: Job, progress: ProgressSink) -> object: ...


WorkflowCallable = Callable[[Job, ProgressSink], object]
Workflow = WorkflowCallable | JobWorkflow


class _CancellationObserved(Exception):
    pass


class JobRunner:
    def __init__(
        self,
        repository: JobRepository,
        workflows: Mapping[JobKind, Workflow],
    ) -> None:
        self._repository = repository
        self._workflows = dict(workflows)

    def run_once(self) -> bool:
        if not self._workflows:
            return False
        job = self._repository.claim_next()
        if job is None:
            return False

        try:
            self._raise_if_cancelled(job.id)
            workflow = self._workflows.get(job.kind)
            if workflow is None:
                raise UserSafeJobError("Обработчик задания не настроен")

            self._invoke(workflow, job, self._progress_sink(job.id))
            self._raise_if_cancelled(job.id)
            self._repository.mark_succeeded(job.id)
        except (_CancellationObserved, JobCancellationRequested):
            self._cancel_if_present(job.id)
        except JobNotFound:
            pass
        except Exception as exc:  # noqa: BLE001 - a failed job must not stop the worker
            self._fail_or_cancel(job.id, self._safe_error_message(exc))
        return True

    def recover_interrupted(self) -> int:
        return self._repository.recover_interrupted(_INTERRUPTED_ERROR_MESSAGE)

    def _progress_sink(self, job_id: str) -> ProgressSink:
        def report(progress: int, status_message: str | None = None) -> None:
            # Workflows report a stage immediately before doing its potentially
            # external work. Checking here makes each report a cancellation gate.
            self._raise_if_cancelled(job_id)
            message = status_message or f"Выполнено {progress}%"
            self._repository.update_progress(job_id, progress, message)

        return report

    def _raise_if_cancelled(self, job_id: str) -> None:
        if self._repository.is_cancel_requested(job_id):
            raise _CancellationObserved

    @staticmethod
    def _invoke(workflow: Workflow, job: Job, progress: ProgressSink) -> object:
        if callable(workflow):
            return workflow(job, progress)
        return workflow.run(job, progress)

    @staticmethod
    def _safe_error_message(exc: Exception) -> str:
        safe_errors = (
            UserSafeJobError,
            ModelError,
            ModelConfigurationError,
            ExtractionError,
            PageLimitExceeded,
        )
        if isinstance(exc, safe_errors) and str(exc):
            return str(exc)
        return _GENERIC_ERROR_MESSAGE

    def _cancel_if_present(self, job_id: str) -> None:
        try:
            self._repository.mark_cancelled(job_id)
        except JobNotFound:
            pass

    def _fail_or_cancel(self, job_id: str, error_message: str) -> None:
        try:
            if self._repository.is_cancel_requested(job_id):
                self._repository.mark_cancelled(job_id)
            else:
                self._repository.mark_failed(job_id, error_message)
        except JobCancellationRequested:
            self._cancel_if_present(job_id)
        except JobNotFound:
            pass


__all__ = [
    "JobRunner",
    "JobWorkflow",
    "ProgressSink",
    "UserSafeJobError",
    "Workflow",
    "WorkflowCallable",
]
