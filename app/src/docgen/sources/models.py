from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docgen.db import Base
from docgen.projects.models import Project, UTCDateTime, utc_now


class SourceKind(str, Enum):
    FILE = "file"
    CONFLUENCE = "confluence"


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'file' AND storage_path IS NOT NULL AND url IS NULL) "
            "OR (kind = 'confluence' AND url IS NOT NULL AND storage_path IS NULL)",
            name="source_kind_location",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[SourceKind] = mapped_column(
        SqlEnum(
            SourceKind,
            native_enum=False,
            values_callable=lambda kinds: [kind.value for kind in kinds],
        ),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    url: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)

    project: Mapped[Project] = relationship(back_populates="sources")
