from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from docgen.db import Base
from docgen.models import UTCDateTime, utc_now


class FindingFixProposalRecord(Base):
    __tablename__ = "finding_fix_proposals"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[int] = mapped_column(
        ForeignKey("check_report_history.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    finding_rule_id: Mapped[str] = mapped_column(String(255), nullable=False)
    finding_code: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    operations_json: Mapped[str] = mapped_column(Text, nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )


__all__ = ["FindingFixProposalRecord"]
