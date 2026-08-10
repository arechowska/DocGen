from __future__ import annotations

from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class BlockKind(str, Enum):
    TEXT = "text"
    HEADING = "heading"
    LIST = "list"
    TABLE = "table"
    IMAGE = "image"


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    locator: str
    quote: str | None = None


class NormalizedBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: BlockKind
    text: str
    data: dict = Field(default_factory=dict)
    provenance: list[Provenance] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
