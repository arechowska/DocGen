from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Event, Thread
from time import monotonic as default_monotonic
from typing import Protocol, Self, TypeVar

from pydantic import BaseModel

from docgen.ai.client import (
    ModelConfigurationError,
    ModelError,
    TextModel,
    VisionDescription,
    VisionModel,
    build_text_model,
    build_vision_model,
)
from docgen.ai.grounding import GroundingValidator
from docgen.config import Settings
from docgen.documents.repository import DocumentRepository
from docgen.export.protocol import ExportError
from docgen.export.service import ExportService
from docgen.extraction.registry import ExtractionError
from docgen.projects.repository import ProjectRepository
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.workflows.normalize import NormalizationWorkflow, PageLimitExceeded

from .models import Job, JobKind
from .repository import (
    InvalidJobTransition,
    JobCancellationRequested,
    JobNotFound,
    JobRepository,
)

_GENERIC_ERROR_MESSAGE = "Не удалось выполнить задание"
_INTERRUPTED_ERROR_MESSAGE = "Обработка была прервана; запустите её повторно"


class UserSafeJobError(RuntimeError):
    """A workflow failure whose message is safe to show and persist."""


class ProgressSink(Protocol):
    def __call__(self, progress: int, status_message: str | None = None) -> None: ...

    def checkpoint(self) -> None: ...

    def report_warnings(self, warnings: list[str]) -> None: ...


class JobWorkflow(Protocol):
    def run(self, job: Job, progress: ProgressSink) -> object: ...


WorkflowCallable = Callable[[Job, ProgressSink], object]
Workflow = WorkflowCallable | JobWorkflow
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class WorkflowDependencies:
    projects: ProjectRepository
    normalization: NormalizationWorkflow
    templates: TemplateCatalog
    documents: DocumentRepository
    # Both optional so existing callers that only need ASSEMBLE/CHECK (e.g.
    # tests exercising `build_workflows` directly) keep working unchanged.
    # `jobs` must be the *same* JobRepository instance (matching worker id
    # /instance token) that JobRunner uses to claim jobs -- see
    # `docgen.workflows.export.ExportWorkflow`.
    jobs: JobRepository | None = None
    export_service: ExportService | None = None


class _ProductionTextModel:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model: TextModel | None = None

    def generate_json(self, system: str, user: str, schema: type[T]) -> T:
        if self._model is None:
            self._model = build_text_model(self._settings)
        return self._model.generate_json(system, user, schema)


class _ProductionVisionModel:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model: VisionModel | None = None

    def describe(self, image: bytes, media_type: str) -> VisionDescription:
        if self._model is None:
            self._model = build_vision_model(self._settings)
        return self._model.describe(image, media_type)


class _CancellationObserved(Exception):
    pass


class JobRunner:
    def __init__(
        self,
        repository: JobRepository,
        workflows: Mapping[JobKind, Workflow],
        *,
        max_job_seconds: float = 300,
        monotonic: Callable[[], float] = default_monotonic,
    ) -> None:
        if max_job_seconds <= 0:
            raise ValueError("max_job_seconds must be positive")
        self._repository = repository
        self._workflows = dict(workflows)
        self._max_job_seconds = max_job_seconds
        self._monotonic = monotonic

    def run_once(self) -> bool:
        if not self._workflows:
            return False
        job = self._repository.claim_next()
        if job is None:
            return False
        deadline = self._monotonic() + self._max_job_seconds

        try:
            self._raise_if_cancelled(job.id, deadline=deadline)
            workflow = self._workflows.get(job.kind)
            if workflow is None:
                raise UserSafeJobError("Обработчик задания не настроен")

            heartbeat = _LeaseHeartbeat(self._repository, job.id)
            with heartbeat:
                self._invoke(workflow, job, self._progress_sink(job.id, deadline))
                heartbeat.raise_if_unavailable()
            self._raise_if_cancelled(
                job.id,
                deadline=deadline,
                renew_lease=False,
            )
            self._repository.mark_succeeded(job.id)
        except (_CancellationObserved, JobCancellationRequested):
            self._cancel_if_present(job.id)
        except JobNotFound:
            self._repository.discard_pending_changes()
        except InvalidJobTransition:
            self._repository.discard_pending_changes()
        except Exception as exc:  # noqa: BLE001 - a failed job must not stop the worker
            self._fail_or_cancel(job.id, self._safe_error_message(exc))
        return True

    def recover_interrupted(self) -> int:
        return self._repository.recover_interrupted(_INTERRUPTED_ERROR_MESSAGE)

    def _progress_sink(self, job_id: str, deadline: float) -> ProgressSink:
        return _RunnerProgressSink(self, job_id, deadline)

    def _raise_if_cancelled(
        self,
        job_id: str,
        *,
        deadline: float | None = None,
        renew_lease: bool = True,
    ) -> None:
        if deadline is not None and self._monotonic() > deadline:
            raise UserSafeJobError(
                "Превышено максимальное время обработки задания"
            )
        cancel_requested = (
            self._repository.checkpoint(job_id)
            if renew_lease
            else self._repository.is_cancel_requested(job_id)
        )
        if cancel_requested:
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
            ExportError,
        )
        if isinstance(exc, safe_errors) and str(exc):
            return str(exc)
        return _GENERIC_ERROR_MESSAGE

    def _cancel_if_present(self, job_id: str) -> None:
        self._repository.discard_pending_changes()
        try:
            self._repository.mark_cancelled(job_id)
        except (InvalidJobTransition, JobNotFound):
            pass

    def _fail_or_cancel(self, job_id: str, error_message: str) -> None:
        self._repository.discard_pending_changes()
        try:
            if self._repository.is_cancel_requested(job_id):
                self._repository.mark_cancelled(job_id)
            else:
                self._repository.mark_failed(
                    job_id,
                    error_message,
                    user_message=error_message,
                )
        except JobCancellationRequested:
            self._cancel_if_present(job_id)
        except (InvalidJobTransition, JobNotFound):
            pass


class _RunnerProgressSink:
    def __init__(self, runner: JobRunner, job_id: str, deadline: float) -> None:
        self._runner = runner
        self._job_id = job_id
        self._deadline = deadline

    def __call__(self, progress: int, status_message: str | None = None) -> None:
        self.checkpoint()
        message = status_message or f"Выполнено {progress}%"
        self._runner._repository.update_progress(self._job_id, progress, message)

    def checkpoint(self) -> None:
        self._runner._raise_if_cancelled(self._job_id, deadline=self._deadline)

    def report_warnings(self, warnings: list[str]) -> None:
        if warnings:
            self.checkpoint()
            self._runner._repository.add_warnings(self._job_id, warnings)


class _LeaseHeartbeat:
    def __init__(self, repository: JobRepository, job_id: str) -> None:
        self._repository = repository
        self._job_id = job_id
        self._stop = Event()
        self._lost = Event()
        self._cancelled = Event()
        self._thread = Thread(
            target=self._run,
            name=f"docgen-lease-{job_id}",
            daemon=True,
        )

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._repository.lease_seconds / 2))

    def raise_if_unavailable(self) -> None:
        if self._cancelled.is_set():
            raise _CancellationObserved
        if self._lost.is_set():
            raise InvalidJobTransition("Аренда задания утрачена")

    def _run(self) -> None:
        interval = max(0.1, self._repository.lease_seconds / 3)
        while not self._stop.wait(interval):
            try:
                if self._repository.heartbeat(self._job_id):
                    self._cancelled.set()
                    return
            except (InvalidJobTransition, JobNotFound):
                self._lost.set()
                return


def build_workflows(
    settings: Settings,
    dependencies: WorkflowDependencies,
) -> dict[JobKind, JobWorkflow]:
    from docgen.workflows.assemble import AssembleWorkflow
    from docgen.workflows.check import CheckWorkflow
    from docgen.workflows.export import ExportWorkflow

    text_model = _ProductionTextModel(settings)
    vision_model = _ProductionVisionModel(settings)
    grounding = GroundingValidator()
    shared = {
        "projects": dependencies.projects,
        "normalization": dependencies.normalization,
        "templates": dependencies.templates,
        "text_model": text_model,
        "vision_model": vision_model,
        "grounding": grounding,
        "documents": dependencies.documents,
    }
    workflows: dict[JobKind, JobWorkflow] = {
        JobKind.ASSEMBLE: AssembleWorkflow(**shared),
        JobKind.CHECK: CheckWorkflow(**shared),
    }
    if dependencies.jobs is not None and dependencies.export_service is not None:
        workflows[JobKind.EXPORT] = ExportWorkflow(
            projects=dependencies.projects,
            service=dependencies.export_service,
            jobs=dependencies.jobs,
        )
    return workflows


__all__ = [
    "JobRunner",
    "JobWorkflow",
    "ProgressSink",
    "UserSafeJobError",
    "Workflow",
    "WorkflowCallable",
    "WorkflowDependencies",
    "build_workflows",
]
