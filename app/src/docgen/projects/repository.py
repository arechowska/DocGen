from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Project

UNTITLED_DOCUMENT_NAME = "Новый документ"


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, name: str) -> Project:
        project = Project(name=self._validated_name(name))
        self._session.add(project)
        self._session.flush()
        return project

    def list(self) -> list[Project]:
        statement = select(Project).order_by(Project.created_at, Project.id)
        return list(self._session.scalars(statement))

    def get(self, project_id: str) -> Project | None:
        return self._session.get(Project, project_id)

    def rename(self, project_id: str, name: str) -> Project:
        project = self.get(project_id)
        if project is None:
            raise LookupError("Проект не найден")

        project.name = self._validated_name(name)
        self._session.flush()
        return project

    def name_untitled_document_from_source(self, project_id: str, filename: str) -> Project:
        project = self.get(project_id)
        if project is None:
            raise LookupError("Проект не найден")
        if project.name == UNTITLED_DOCUMENT_NAME:
            source_name = Path(filename).stem.strip()
            if source_name:
                project.name = self._validated_name(source_name)
                self._session.flush()
        return project

    def delete(self, project_id: str) -> bool:
        project = self.get(project_id)
        if project is None:
            return False

        self._session.delete(project)
        self._session.flush()
        return True

    @staticmethod
    def _validated_name(name: str) -> str:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Название проекта обязательно")
        return normalized_name
