from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RuleDimension = Literal["structure", "completeness", "terminology", "contradiction", "style"]
RuleSeverity = Literal["error", "warning", "info"]
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_PLACEHOLDER_MARKERS = (
    "tbd",
    "todo",
    "fixme",
    "placeholder",
    "to be determined",
    "заполнить",
    "заполните",
    "заполнитель",
    "заглушка",
    "не определено",
    "lorem ipsum",
)


def _validate_russian_text(value: str, *, concrete: bool) -> str:
    normalized_value = " ".join(value.split())
    normalized_casefold = normalized_value.casefold()
    marker = next(
        (marker for marker in _PLACEHOLDER_MARKERS if marker in normalized_casefold), None
    )
    if marker:
        raise ValueError(f"Текст не должен содержать заполнитель: {marker}")
    if not _CYRILLIC_RE.search(normalized_value):
        raise ValueError("Текст должен содержать кириллицу")
    if concrete and len(normalized_value) < 10:
        raise ValueError("Текст должен быть конкретной русской инструкцией")
    return normalized_value


class SemanticSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    required: bool
    description: str = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _validate_russian_text(value, concrete=False)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _validate_russian_text(value, concrete=True)


class SemanticRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    dimension: RuleDimension
    severity: RuleSeverity
    instruction: str = Field(min_length=1)

    @field_validator("instruction")
    @classmethod
    def validate_instruction(cls, value: str) -> str:
        return _validate_russian_text(value, concrete=True)


class SemanticTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sections: tuple[SemanticSection, ...] = Field(min_length=3)
    rules: tuple[SemanticRule, ...] = Field(min_length=5)
    style_rules: tuple[str, ...] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_russian_text(value, concrete=False)

    @field_validator("style_rules")
    @classmethod
    def validate_style_rules(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_russian_text(style_rule, concrete=True) for style_rule in value)

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
