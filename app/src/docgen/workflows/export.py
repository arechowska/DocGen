from __future__ import annotations

from docgen.export.service import ExportRequest, ExportResult, ExportService
from docgen.jobs.models import Job, JobKind
from docgen.jobs.repository import JobRepository
from docgen.jobs.runner import ProgressSink
from docgen.projects.repository import ProjectRepository

from .assemble import WorkflowError

_EXPORT_KIND_ERROR = "Некорректный тип задания для экспорта"
_PROJECT_NOT_FOUND = "Проект не найден"
_FORMAT_MISSING_ERROR = "Для задания экспорта не указан формат"
_REVISION_MISSING_ERROR = "Для задания экспорта не указана ревизия документа"


class ExportWorkflow:
    """Renders one document revision to the job's chosen format/template.

    Per the Global Constraints, export uses the *exact* document revision
    the user submitted the request against -- never the project's current
    revision, which may have moved on while the job sat queued. `job
    .requested_document_revision` (captured by `export/routes.py` at
    submit time, via `JobRepository.enqueue_if_project_idle`) is what gets
    passed to `ExportService`, which renders it fully in memory (via the
    format's `Exporter`) and writes it atomically before this returns.
    `ExportService.export()` itself is the sole place that re-checks the
    document still exists at that exact revision, raising `ExportError` if
    it has since changed -- this workflow must not duplicate that check
    with a *different* (current-revision) read, or the guard never fires.

    Mirrors `AssembleWorkflow`/`CheckWorkflow`'s shape -- `run()` does the
    work and persists its result mid-run before returning, exactly like
    those workflows call `DocumentRepository.save_document`/`save_report`.
    Here that means calling `JobRepository.record_export_result` once the
    file is written. `jobs` must be the *same* `JobRepository` instance
    (same worker id/instance token) that `JobRunner` uses to claim and run
    this job, since `record_export_result` only succeeds against the job's
    own current RUNNING lease -- see `JobRepository._atomic_owned_update`.
    """

    def __init__(
        self,
        *,
        projects: ProjectRepository,
        service: ExportService,
        jobs: JobRepository,
    ) -> None:
        self._projects = projects
        self._service = service
        self._jobs = jobs

    def run(self, job: Job, progress: ProgressSink) -> ExportResult:
        if job.kind is not JobKind.EXPORT:
            raise WorkflowError(_EXPORT_KIND_ERROR)
        if job.export_format is None:
            raise WorkflowError(_FORMAT_MISSING_ERROR)
        if job.requested_document_revision is None:
            raise WorkflowError(_REVISION_MISSING_ERROR)

        progress(10, "Проверен проект")
        if self._projects.get(job.project_id) is None:
            raise WorkflowError(_PROJECT_NOT_FOUND)

        progress(60, "Формирование файла…")
        result = self._service.export(
            ExportRequest(
                project_id=job.project_id,
                document_revision=job.requested_document_revision,
                format=job.export_format,
                template_id=job.template_id,
            )
        )

        progress(100, "Сохранение файла экспорта")
        self._jobs.record_export_result(job.id, result)
        return result


__all__ = ["ExportWorkflow"]
