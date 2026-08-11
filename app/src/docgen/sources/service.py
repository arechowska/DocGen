from __future__ import annotations

import logging
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from docgen.db import begin_sqlite_writer_transaction
from docgen.jobs.models import Job, JobStatus
from docgen.jobs.repository import ActiveProjectJobExists
from docgen.projects.repository import ProjectRepository

from .models import Source
from .repository import SourceRepository
from .storage import LocalStorage

_DEFAULT_MAX_UPLOAD_BYTES = 52_428_800
_DEFAULT_MAX_PROJECT_STORAGE_BYTES = 524_288_000
_PROJECT_STORAGE_ERROR = "Общий объём файлов проекта превышен"

ALLOWED_EXTENSIONS = frozenset({".docx", ".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp"})
_CONFLUENCE_URL_ERROR = "Разрешены только ссылки Confluence"
logger = logging.getLogger(__name__)


class SourceService:
    def __init__(
        self,
        session: Session,
        storage: LocalStorage,
        confluence_hosts: tuple[str, ...],
        *,
        max_upload_bytes: int = _DEFAULT_MAX_UPLOAD_BYTES,
        max_project_storage_bytes: int = _DEFAULT_MAX_PROJECT_STORAGE_BYTES,
    ) -> None:
        self._session = session
        self._storage = storage
        self._project_repository = ProjectRepository(session)
        self._repository = SourceRepository(session)
        self._confluence_hosts = frozenset(host.lower() for host in confluence_hosts)
        self._max_upload_bytes = max_upload_bytes
        self._max_project_storage_bytes = max_project_storage_bytes

    def add_file(
        self, project_id: str, filename: str, media_type: str, stream: BinaryIO
    ) -> Source:
        self._validate_extension(filename)
        source_id = str(uuid4())
        stored_file = None
        try:
            begin_sqlite_writer_transaction(self._session)
            self._require_project(project_id)
            current_size = self._session.scalar(
                select(func.coalesce(func.sum(Source.size_bytes), 0)).where(
                    Source.project_id == project_id
                )
            )
            remaining_project_bytes = self._max_project_storage_bytes - int(
                current_size or 0
            )
            if remaining_project_bytes <= 0:
                raise ValueError(_PROJECT_STORAGE_ERROR)
            project_limit_is_tighter = remaining_project_bytes < self._max_upload_bytes
            stored_file = self._storage.save(
                project_id,
                source_id,
                filename,
                stream,
                max_bytes=min(self._max_upload_bytes, remaining_project_bytes),
                limit_message=(
                    _PROJECT_STORAGE_ERROR
                    if project_limit_is_tighter
                    else "Файл слишком большой"
                ),
            )
            source = self._repository.create_file(
                project_id,
                source_id,
                filename,
                media_type,
                stored_file.size_bytes,
                stored_file.relative_path,
            )
            self._project_repository.name_untitled_document_from_source(project_id, filename)
            self._session.commit()
        except Exception:
            self._session.rollback()
            if stored_file is not None:
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
        try:
            begin_sqlite_writer_transaction(self._session)
            self._require_project(project_id)
            source = self._repository.get(source_id)
            if source is None or source.project_id != project_id:
                raise LookupError("Источник не найден")
            active_job = self._session.scalar(
                select(Job.id)
                .where(
                    Job.project_id == project_id,
                    Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
                )
                .limit(1)
            )
            if active_job is not None:
                raise ActiveProjectJobExists("Проект обрабатывается; источник нельзя удалить")
            storage_path = source.storage_path
            self._session.execute(
                update(Job)
                .where(Job.target_source_id == source_id)
                .values(target_source_id=None)
                .execution_options(synchronize_session=False)
            )
            self._repository.delete(source_id)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        if storage_path is not None:
            try:
                self._storage.delete(storage_path)
            except Exception:
                logger.exception(
                    "source_cleanup_failed project_id=%s source_id=%s storage_path=%s",
                    project_id,
                    source_id,
                    storage_path,
                    extra={
                        "project_id": project_id,
                        "source_id": source_id,
                        "storage_path": storage_path,
                    },
                )
                raise

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
