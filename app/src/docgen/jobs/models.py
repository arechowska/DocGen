from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, text
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docgen.db import Base
from docgen.models import Project, UTCDateTime, utc_now


class JobKind(str, Enum):
    ASSEMBLE = "assemble"
    CHECK = "check"
    EXPORT = "export"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CheckTargetKind(str, Enum):
    CURRENT = "current"
    SOURCE = "source"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("progress BETWEEN 0 AND 100", name="job_progress_range"),
        CheckConstraint(
            "(kind IN ('assemble', 'export') AND target_source_id IS NULL "
            "AND check_target_kind IS NULL) OR "
            "(kind = 'check' AND check_target_kind IN ('current', 'source'))",
            name="job_target_kind_matches_operation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[JobKind] = mapped_column(
        SqlEnum(
            JobKind,
            native_enum=False,
            values_callable=lambda kinds: [kind.value for kind in kinds],
        ),
        nullable=False,
    )
    template_id: Mapped[str] = mapped_column(String(255), nullable=False)
    target_source_id: Mapped[str | None] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=True
    )
    check_target_kind: Mapped[CheckTargetKind | None] = mapped_column(
        SqlEnum(
            CheckTargetKind,
            native_enum=False,
            values_callable=lambda kinds: [kind.value for kind in kinds],
        )
    )
    status: Mapped[JobStatus] = mapped_column(
        SqlEnum(
            JobStatus,
            native_enum=False,
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
        default=JobStatus.QUEUED,
        index=True,
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_message: Mapped[str] = mapped_column(Text, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    worker_id: Mapped[str | None] = mapped_column(String(255), index=True)
    worker_instance_token: Mapped[str | None] = mapped_column(String(64), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    warnings_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default=text("'[]'")
    )
    result_document_revision: Mapped[int | None] = mapped_column(Integer)
    result_report_revision: Mapped[int | None] = mapped_column(Integer)
    result_report_generation: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    project: Mapped[Project] = relationship(back_populates="jobs")

    @property
    def warning_messages(self) -> tuple[str, ...]:
        try:
            raw = json.loads(self.warnings_json or "[]")
        except (TypeError, json.JSONDecodeError):
            return ()
        if not isinstance(raw, list):
            return ()
        return tuple(item for item in raw if isinstance(item, str) and item.strip())


Project.jobs = relationship(Job, back_populates="project", cascade="all, delete-orphan")


__all__ = ["CheckTargetKind", "Job", "JobKind", "JobStatus"]
