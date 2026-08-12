from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from docgen.db import Base
from docgen.models import UTCDateTime, utc_now


class ProjectArtifact(Base):
    __tablename__ = "project_artifacts"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    document_json: Mapped[str | None] = mapped_column(Text)
    workspace_html: Mapped[str | None] = mapped_column(Text)
    report_json: Mapped[str | None] = mapped_column(Text)
    document_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    report_revision: Mapped[int | None] = mapped_column(Integer)
    report_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )
