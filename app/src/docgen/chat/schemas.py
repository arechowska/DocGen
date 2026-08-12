from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from docgen.documents.operations import DocumentOperation
from docgen.documents.schemas import WorkingDocument


class ChatEditRequest(BaseModel):
    message: str
    expected_revision: int


class ChatEditOperation(BaseModel):
    operation: DocumentOperation
    evidence_block_ids: list[str] = Field(default_factory=list)


class ChatEditPlan(BaseModel):
    summary: str
    operations: list[ChatEditOperation] = Field(default_factory=list)


class ChatEditResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    summary: str
    document: WorkingDocument
    revision: int
