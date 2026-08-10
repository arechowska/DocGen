from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ListPayload(BaseModel):
    revision: int
    items: list[str] = Field(min_length=1, max_length=100)


class TablePayload(BaseModel):
    revision: int
    rows: list[list[str]] = Field(min_length=1, max_length=100)

    @field_validator("rows")
    @classmethod
    def _rows_have_allowed_width(cls, rows: list[list[str]]) -> list[list[str]]:
        for row in rows:
            if not 1 <= len(row) <= 20:
                raise ValueError("Таблица должна содержать от 1 до 20 ячеек в строке")
        return rows

    @model_validator(mode="after")
    def _rows_are_rectangular(self) -> TablePayload:
        width = len(self.rows[0])
        if any(len(row) != width for row in self.rows):
            raise ValueError("Все строки таблицы должны иметь одинаковое число ячеек")
        return self


class ImagePayload(BaseModel):
    revision: int
    alignment: Literal["left", "center", "right"]
    width: int = Field(ge=10, le=100)
    alt: str = Field(min_length=1)
