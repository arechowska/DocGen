from __future__ import annotations

import json

from pydantic import TypeAdapter
from sqlalchemy.orm import Session

from docgen.documents.operations import DocumentOperation

from .models import FindingFixProposalRecord

_OPERATIONS = TypeAdapter(list[DocumentOperation])


class FindingFixProposalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        project_id: str,
        report_id: int,
        document_revision: int,
        finding_rule_id: str,
        finding_code: str,
        summary: str,
        operations: list[DocumentOperation],
    ) -> FindingFixProposalRecord:
        record = FindingFixProposalRecord(
            project_id=project_id,
            report_id=report_id,
            document_revision=document_revision,
            finding_rule_id=finding_rule_id,
            finding_code=finding_code,
            summary=summary,
            operations_json=json.dumps(
                _OPERATIONS.dump_python(operations, mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        self._session.add(record)
        self._session.flush()
        return record

    def get(self, proposal_id: str) -> FindingFixProposalRecord | None:
        return self._session.get(FindingFixProposalRecord, proposal_id)

    @staticmethod
    def operations(record: FindingFixProposalRecord) -> list[DocumentOperation]:
        return _OPERATIONS.validate_json(record.operations_json)


__all__ = ["FindingFixProposalRepository"]
