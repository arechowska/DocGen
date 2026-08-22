from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, text
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


class CheckReportRecord(Base):
    __tablename__ = "check_report_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_revision: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    check_profile_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    target_source_id: Mapped[str | None] = mapped_column(String(36))
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
