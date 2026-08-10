import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from docgen.db import begin_sqlite_writer_transaction
from docgen.jobs.models import Job, JobStatus
from docgen.jobs.repository import ActiveProjectJobExists
from docgen.sources.storage import LocalStorage

from .repository import ProjectRepository

logger = logging.getLogger(__name__)


class ProjectService:
    def __init__(self, session: Session, storage: LocalStorage) -> None:
        self._session = session
        self._repository = ProjectRepository(session)
        self._storage = storage

    def delete(self, project_id: str) -> None:
        try:
            begin_sqlite_writer_transaction(self._session)
            if self._repository.get(project_id) is None:
                raise LookupError("Проект не найден")
            active_job = self._session.scalar(
                select(Job.id)
                .where(
                    Job.project_id == project_id,
                    Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
                )
                .limit(1)
            )
            if active_job is not None:
                raise ActiveProjectJobExists("Проект обрабатывается; дождитесь завершения задания")
            self._repository.delete(project_id)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        try:
            self._storage.delete_project(project_id)
        except Exception:
            logger.exception(
                "project_cleanup_failed project_id=%s",
                project_id,
                extra={"project_id": project_id},
            )
            raise
