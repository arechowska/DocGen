from __future__ import annotations

from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy.orm import Session

from docgen.projects.repository import ProjectRepository

from .models import Source
from .repository import SourceRepository
from .storage import LocalStorage

ALLOWED_EXTENSIONS = frozenset({".docx", ".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp"})
_CONFLUENCE_URL_ERROR = "Разрешены только ссылки Confluence"


class SourceService:
    def __init__(
        self, session: Session, storage: LocalStorage, confluence_hosts: tuple[str, ...]
    ) -> None:
        self._session = session
        self._storage = storage
        self._project_repository = ProjectRepository(session)
        self._repository = SourceRepository(session)
        self._confluence_hosts = frozenset(host.lower() for host in confluence_hosts)

    def add_file(
        self, project_id: str, filename: str, media_type: str, stream: BinaryIO
    ) -> Source:
        self._require_project(project_id)
        self._validate_extension(filename)
        source_id = str(uuid4())
        stored_file = self._storage.save(project_id, source_id, filename, stream)

        try:
            source = self._repository.create_file(
                project_id,
                source_id,
                filename,
                media_type,
                stored_file.size_bytes,
                stored_file.relative_path,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            self._storage.delete(stored_file.relative_path)
            raise
        return source

    def add_confluence(self, project_id: str, url: str) -> Source:
        self._require_project(project_id)
        self._validate_confluence_url(url)

        try:
            source = self._repository.create_confluence(project_id, str(uuid4()), url)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return source

    def list(self, project_id: str) -> list[Source]:
        self._require_project(project_id)
        return self._repository.list_for_project(project_id)

    def delete(self, project_id: str, source_id: str) -> None:
        self._require_project(project_id)
        source = self._repository.get(source_id)
        if source is None or source.project_id != project_id:
            raise LookupError("Источник не найден")

        storage_path = source.storage_path
        try:
            self._repository.delete(source_id)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        if storage_path is not None:
            self._storage.delete(storage_path)

    def _require_project(self, project_id: str) -> None:
        if self._project_repository.get(project_id) is None:
            raise LookupError("Проект не найден")

    @staticmethod
    def _validate_extension(filename: str) -> None:
        if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise ValueError("Формат файла не поддерживается")

    def _validate_confluence_url(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            _ = parsed.port
        except ValueError:
            raise ValueError(_CONFLUENCE_URL_ERROR) from None

        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or host is None
            or host.lower() not in self._confluence_hosts
            or not parsed.path
        ):
            raise ValueError(_CONFLUENCE_URL_ERROR)
