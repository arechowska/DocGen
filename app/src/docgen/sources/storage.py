from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_EXTENSION_PATTERN = re.compile(r"\.[a-z0-9]{1,16}")
_COPY_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class StoredFile:
    relative_path: str
    size_bytes: int


class LocalStorage:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir.resolve()
        self._projects_dir = self._data_dir / "projects"

    def save(
        self, project_id: str, source_id: str, filename: str, stream: BinaryIO
    ) -> StoredFile:
        project_dir = self._resolved_project_dir(project_id)
        validated_source_id = self._validated_identifier(source_id)
        sources_dir = project_dir / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        sources_dir = sources_dir.resolve()
        self._require_within(sources_dir, project_dir)

        destination = sources_dir / f"{validated_source_id}{self._extension(filename)}"
        self._require_within(destination.resolve(), sources_dir)
        part_path = destination.with_suffix(f"{destination.suffix}.part")
        part_path.unlink(missing_ok=True)

        size_bytes = 0
        try:
            with part_path.open("xb") as part_file:
                while chunk := stream.read(_COPY_CHUNK_SIZE):
                    part_file.write(chunk)
                    size_bytes += len(chunk)
            part_path.replace(destination)
        except Exception:
            part_path.unlink(missing_ok=True)
            raise

        return StoredFile(
            relative_path=destination.relative_to(self._data_dir).as_posix(),
            size_bytes=size_bytes,
        )

    def delete(self, relative_path: str) -> None:
        path = self.resolve(relative_path)
        if path.is_dir():
            raise ValueError("Недопустимый путь")
        path.unlink(missing_ok=True)

    def delete_project(self, project_id: str) -> None:
        project_path = self._project_path(project_id)
        if project_path.is_symlink():
            project_path.unlink()
            return
        project_dir = self._resolved_project_dir(project_id)
        if not project_dir.exists():
            return
        shutil.rmtree(project_dir)

    def resolve(self, relative_path: str) -> Path:
        path = Path(relative_path)
        if not relative_path or path.is_absolute() or ".." in path.parts or "\\" in relative_path:
            raise ValueError("Недопустимый путь")
        resolved_path = (self._data_dir / path).resolve()
        self._require_within(resolved_path, self._data_dir)
        return resolved_path

    def _project_path(self, project_id: str) -> Path:
        validated_project_id = self._validated_identifier(project_id)
        projects_dir = self._projects_dir.resolve()
        self._require_within(projects_dir, self._data_dir)
        if self._projects_dir.is_symlink():
            raise ValueError("Недопустимый путь")
        return self._projects_dir / validated_project_id

    def _resolved_project_dir(self, project_id: str) -> Path:
        project_path = self._project_path(project_id)
        if project_path.is_symlink():
            raise ValueError("Недопустимый путь")
        project_dir = project_path.resolve()
        self._require_within(project_dir, self._projects_dir.resolve())
        return project_dir

    @staticmethod
    def _validated_identifier(identifier: str) -> str:
        if not _IDENTIFIER_PATTERN.fullmatch(identifier):
            raise ValueError("Недопустимый идентификатор")
        return identifier

    @staticmethod
    def _extension(filename: str) -> str:
        extension = Path(filename).suffix.lower()
        return extension if _EXTENSION_PATTERN.fullmatch(extension) else ""

    @staticmethod
    def _require_within(path: Path, root: Path) -> None:
        if not path.is_relative_to(root):
            raise ValueError("Недопустимый путь")
