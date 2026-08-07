from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RuleDimension = Literal["structure", "completeness", "terminology", "contradiction", "style"]
RuleSeverity = Literal["error", "warning", "info"]


class SemanticSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    required: bool
    description: str = Field(min_length=1)


class SemanticRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    dimension: RuleDimension
    severity: RuleSeverity
    instruction: str = Field(min_length=1)


class SemanticTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sections: list[SemanticSection] = Field(min_length=3)
    rules: list[SemanticRule] = Field(min_length=5)
    style_rules: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_semantics(self) -> SemanticTemplate:
        section_ids = [section.id for section in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("Повторяющийся идентификатор раздела")
        if sum(section.required for section in self.sections) < 3:
            raise ValueError("Необходимо не менее трёх обязательных разделов")

        rule_ids = [rule.id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("Повторяющийся идентификатор правила")

        required_dimensions = {
            "structure",
            "completeness",
            "terminology",
            "contradiction",
            "style",
        }
        actual_dimensions = {rule.dimension for rule in self.rules}
        missing_dimensions = required_dimensions - actual_dimensions
        if missing_dimensions:
            raise ValueError(
                "Не заданы правила для измерений: " + ", ".join(sorted(missing_dimensions))
            )

        return self
