from __future__ import annotations

from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from docgen.extraction.schemas import Provenance


class NodeKind(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    IMAGE = "image"
    GAP = "gap"


class DocumentOrigin(str, Enum):
    IMPORTED = "imported"
    ASSEMBLED = "assembled"
    MANUAL = "manual"


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
    # A default keeps the model-facing JSON schema backward compatible. The
    # pre-validator still derives the canonical value from legacy template_id.
    origin: DocumentOrigin = DocumentOrigin.MANUAL
    source_id: str | None = None
    build_template_id: str | None = None
    nodes: list[DocumentNode] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_identity(cls, value):
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        template_id = migrated.get("template_id")
        origin = migrated.get("origin")
        if origin is None:
            if template_id and template_id != "no-template":
                migrated["origin"] = DocumentOrigin.ASSEMBLED
                migrated.setdefault("build_template_id", template_id)
            else:
                migrated["origin"] = DocumentOrigin.MANUAL
        elif (
            origin == DocumentOrigin.ASSEMBLED
            or origin == DocumentOrigin.ASSEMBLED.value
        ) and migrated.get("build_template_id") is None and template_id != "no-template":
            migrated["build_template_id"] = template_id
        return migrated

    @model_validator(mode="after")
    def validate_identity(self) -> WorkingDocument:
        if self.origin is DocumentOrigin.IMPORTED:
            if not self.source_id:
                raise ValueError("Импортированный документ должен ссылаться на источник")
            if self.build_template_id is not None or self.template_id != "no-template":
                raise ValueError(
                    "Импортированный документ не может иметь шаблон сборки"
                )
        elif self.origin is DocumentOrigin.ASSEMBLED:
            if not self.build_template_id:
                raise ValueError("Собранный документ должен иметь шаблон сборки")
            if self.template_id != self.build_template_id:
                raise ValueError(
                    "Совместимое template_id не совпадает с шаблоном сборки"
                )
            if self.source_id is not None:
                raise ValueError("Собранный документ не может быть импортированным")
        elif self.source_id is not None or self.build_template_id is not None:
            raise ValueError("Ручной документ не должен иметь источник или шаблон сборки")
        return self


class CheckFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    message: str
    evidence: str | None = None
    suggestion: str | None = None
    node_id: str | None = None
    rule_id: str | None = None


class CheckReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    template_id: str
    check_profile_id: str | None = None
    findings: list[CheckFinding] = Field(default_factory=list)
    passed_rule_ids: tuple[str, ...] = ()
    unchecked_rules: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def populate_check_profile(self) -> CheckReport:
        profile_id = self.check_profile_id or self.template_id
        if profile_id != self.template_id:
            raise ValueError("Профиль отчёта не совпадает с template_id ответа модели")
        object.__setattr__(self, "check_profile_id", profile_id)
        return self
