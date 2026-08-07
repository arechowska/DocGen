import logging

from sqlalchemy.orm import Session

from docgen.sources.storage import LocalStorage

from .repository import ProjectRepository

logger = logging.getLogger(__name__)


class ProjectService:
    def __init__(self, session: Session, storage: LocalStorage) -> None:
        self._session = session
        self._repository = ProjectRepository(session)
        self._storage = storage

    def delete(self, project_id: str) -> None:
        if self._repository.get(project_id) is None:
            raise LookupError("Проект не найден")
        try:
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
