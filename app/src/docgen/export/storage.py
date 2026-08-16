"""Atomic on-disk storage for rendered export files.

Mirrors the write-to-``.part``-then-``Path.replace()`` convention already
established by ``docgen.sources.storage.LocalStorage.save``: content is
always written to a hidden ``.part`` file first, fsynced, and only
atomically renamed onto the final target once fully and successfully
written. A failure at any point deletes the ``.part`` file and leaves any
previously-written, successfully-exported file at the target path
completely untouched -- an export failure never overwrites a working file
with a partial one.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from docgen.export.protocol import RenderedFile
from docgen.formatting.schemas import OutputFormat

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


@dataclass(frozen=True)
class StoredExport:
    """Metadata describing an export file written to disk."""

    relative_path: str
    filename: str
    size_bytes: int


class ExportStorage:
    """Writes rendered export files atomically under a project's exports directory."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir.resolve()
        self._projects_dir = self._data_dir / "projects"

    def save(
        self,
        project_id: str,
        format: OutputFormat,
        template_id: str,
        rendered: RenderedFile,
    ) -> StoredExport:
        """Atomically write `rendered` as the export for `format`/`template_id`.

        Writes to a shared `.{format}-{template_id}.part` file inside the
        project's `exports/` directory, fsyncs it, then `Path.replace()`s it
        onto `<safe-title>-<template_id>.<ext>` (derived from `rendered
        .filename`). If anything fails before the replace, the `.part` file
        is deleted and the previous successful export at the destination
        path (if any) is left completely untouched.
        """
        validated_template_id = self._validated_identifier(template_id)
        exports_dir = self._exports_dir(project_id)

        part_path = exports_dir / f".{format.value}-{validated_template_id}.part"
        self._require_within(part_path.resolve(), exports_dir)

        destination = exports_dir / self._destination_name(
            rendered.filename, validated_template_id
        )
        self._require_within(destination.resolve(), exports_dir)

        part_path.unlink(missing_ok=True)
        try:
            with part_path.open("xb") as part_file:
                part_file.write(rendered.content)
                part_file.flush()
                os.fsync(part_file.fileno())
            part_path.replace(destination)
        except Exception:
            part_path.unlink(missing_ok=True)
            raise

        return StoredExport(
            relative_path=destination.relative_to(self._data_dir).as_posix(),
            filename=destination.name,
            size_bytes=len(rendered.content),
        )

    def resolve(self, relative_path: str) -> Path:
        path = Path(relative_path)
        if not relative_path or path.is_absolute() or ".." in path.parts or "\\" in relative_path:
            raise ValueError("Недопустимый путь")
        resolved_path = (self._data_dir / path).resolve()
        self._require_within(resolved_path, self._data_dir)
        return resolved_path

    def _exports_dir(self, project_id: str) -> Path:
        project_dir = self._resolved_project_dir(project_id)
        exports_dir = project_dir / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        exports_dir = exports_dir.resolve()
        self._require_within(exports_dir, project_dir)
        return exports_dir

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
    def _destination_name(rendered_filename: str, template_id: str) -> str:
        stem = Path(rendered_filename).stem
        suffix = Path(rendered_filename).suffix
        return f"{stem}-{template_id}{suffix}"

    @staticmethod
    def _validated_identifier(identifier: str) -> str:
        if not _IDENTIFIER_PATTERN.fullmatch(identifier):
            raise ValueError("Недопустимый идентификатор")
        return identifier

    @staticmethod
    def _require_within(path: Path, root: Path) -> None:
        if not path.is_relative_to(root):
            raise ValueError("Недопустимый путь")


__all__ = ["ExportStorage", "StoredExport"]
