from __future__ import annotations

from dataclasses import dataclass

from docgen.documents.operations import (
    DocumentOperation,
    EditValidationError,
    apply_operations,
)
from docgen.documents.repository import DocumentRepository
from docgen.documents.schemas import WorkingDocument


class EditConflict(ValueError):
    pass


@dataclass(frozen=True)
class EditResult:
    document: WorkingDocument
    revision: int


class DocumentEditService:
    def __init__(self, repository: DocumentRepository) -> None:
        self._repository = repository

    def apply(
        self,
        project_id: str,
        expected_revision: int,
        operations: list[DocumentOperation],
    ) -> EditResult:
        stored = self._repository.get_document_with_revision(project_id)
        if stored is None:
            raise EditValidationError("Документ не найден")

        document, current_revision = stored
        if current_revision != expected_revision:
            raise EditConflict("Документ уже изменён")

        candidate = apply_operations(document, operations)
        new_revision = self._repository.replace_document(
            project_id,
            expected_revision,
            candidate,
        )
        if new_revision is None:
            raise EditConflict("Документ уже изменён")
        return EditResult(document=candidate, revision=new_revision)
