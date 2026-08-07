from datetime import UTC

import pytest
from sqlalchemy.orm import Session

from docgen.projects.repository import ProjectRepository


def test_create_list_and_rename_project(project_repository: ProjectRepository) -> None:
    project = project_repository.create("Use Case клиента")
    assert project.name == "Use Case клиента"
    assert project_repository.list()[0].id == project.id

    renamed = project_repository.rename(project.id, "Use Case банка")
    assert renamed.name == "Use Case банка"


def test_project_name_cannot_be_blank(project_repository: ProjectRepository) -> None:
    with pytest.raises(ValueError, match="Название проекта обязательно"):
        project_repository.create("   ")


def test_project_can_be_fetched_and_deleted(project_repository: ProjectRepository) -> None:
    project = project_repository.create("  Проект клиента  ")

    fetched = project_repository.get(project.id)

    assert fetched is not None
    assert fetched.id == project.id
    assert fetched.name == "Проект клиента"
    assert project_repository.delete(project.id) is True
    assert project_repository.get(project.id) is None
    assert project_repository.delete(project.id) is False


def test_rename_trims_name_and_rejects_missing_project(project_repository: ProjectRepository) -> None:
    project = project_repository.create("Проект")

    renamed = project_repository.rename(project.id, "  Новый проект  ")

    assert renamed.name == "Новый проект"
    with pytest.raises(LookupError, match="Проект не найден"):
        project_repository.rename("missing", "Другое имя")


def test_project_timestamps_remain_utc_after_database_read(
    project_repository: ProjectRepository, session: Session
) -> None:
    project = project_repository.create("Проект с UTC")
    project_id = project.id

    session.expunge_all()
    fetched = project_repository.get(project_id)

    assert fetched is not None
    assert fetched.created_at.tzinfo is UTC
    assert fetched.updated_at.tzinfo is UTC
