from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from docgen.documents.operations import DocumentOperation
from docgen.documents.schemas import WorkingDocument


class ChatEditRequest(BaseModel):
    message: str
    expected_revision: int


class ChatEditOperation(BaseModel):
    operation: DocumentOperation
    evidence_block_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_bare_and_dotted_operations(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        if "operation" not in value and isinstance(value.get("kind"), str):
            value = {"operation": value, "evidence_block_ids": []}
        raw_operation = value.get("operation")
        if not isinstance(raw_operation, dict) or raw_operation.get("kind") != "update_data":
            return value

        data = raw_operation.get("data")
        normalized_data = dict(data) if isinstance(data, dict) else {}
        for key in list(raw_operation):
            if not isinstance(key, str) or not key.startswith("data."):
                continue
            _set_dotted_data(normalized_data, key.removeprefix("data."), raw_operation[key])
            del raw_operation[key]
        if normalized_data:
            raw_operation["data"] = normalized_data
        return value


class ChatEditPlan(BaseModel):
    summary: str
    operations: list[ChatEditOperation] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_bare_operation_list(cls, value: object) -> object:
        if isinstance(value, list):
            summary = "Нет правок" if not value else "Применены правки"
            return {"summary": summary, "operations": value}
        return value


class FaqPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: str | None = None
    index: int = Field(ge=0)


class FaqEntryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    placement: FaqPlacement
    evidence_block_ids: list[str] = Field(min_length=1)

    @field_validator("question", "answer")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("FAQ text must not be blank")
        return normalized

    @field_validator("evidence_block_ids")
    @classmethod
    def reject_blank_evidence(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("FAQ evidence IDs must not be blank")
        return value


def _set_dotted_data(data: dict, path: str, value: object) -> None:
    target = data
    parts = [part for part in path.split(".") if part]
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    if parts:
        target[parts[-1]] = value


class ChatEditResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    summary: str
    document: WorkingDocument
    revision: int


class FindingFixProposal(BaseModel):
    summary: str
    operations: list[DocumentOperation]
    document: WorkingDocument
