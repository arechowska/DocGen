from __future__ import annotations

from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from docgen.extraction.schemas import Provenance


class NodeKind(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    IMAGE = "image"
    GAP = "gap"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class DocumentNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: NodeKind
    section_id: str | None = None
    text: str | None = None
    data: dict = Field(default_factory=dict)
    children: list[DocumentNode] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class WorkingDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    template_id: str
    nodes: list[DocumentNode] = Field(default_factory=list)


class CheckFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    message: str
    node_id: str | None = None
    rule_id: str | None = None


class CheckReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    template_id: str
    findings: list[CheckFinding] = Field(default_factory=list)
    passed_rule_ids: tuple[str, ...] = ()
    unchecked_rules: list[str] = Field(default_factory=list)
