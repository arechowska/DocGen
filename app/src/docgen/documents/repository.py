from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import CheckReportRecord, ProjectArtifact
from .schemas import CheckReport, WorkingDocument


@dataclass(frozen=True)
class StoredCheckReport:
    id: int
    project_id: str
    document_revision: int
    check_profile_id: str
    target_source_id: str | None
    report: CheckReport


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_document(self, project_id: str, document: WorkingDocument) -> int:
        artifact = self._get_or_create(project_id)
        artifact.document_revision = (artifact.document_revision or 0) + 1
        artifact.document_json = document.model_dump_json()
        artifact.workspace_html = None
        artifact.report_json = None
        artifact.report_revision = None
        self._session.flush()
        return artifact.document_revision

    def get_document(self, project_id: str) -> WorkingDocument | None:
        artifact = self._session.get(ProjectArtifact, project_id)
        if artifact is None or artifact.document_json is None:
            return None
        return WorkingDocument.model_validate_json(artifact.document_json)

    def get_workspace_html(self, project_id: str) -> str | None:
        artifact = self._session.get(ProjectArtifact, project_id)
        if artifact is None:
            return None
        return artifact.workspace_html

    def get_document_with_revision(
        self, project_id: str
    ) -> tuple[WorkingDocument, int] | None:
        artifact = self._session.get(ProjectArtifact, project_id)
        if artifact is None or artifact.document_json is None:
            return None
        return (
            WorkingDocument.model_validate_json(artifact.document_json),
            artifact.document_revision,
        )

    def replace_document(
        self,
        project_id: str,
        expected_revision: int,
        document: WorkingDocument,
    ) -> int | None:
        result = self._session.execute(
            update(ProjectArtifact)
            .where(
                ProjectArtifact.project_id == project_id,
                ProjectArtifact.document_json.is_not(None),
                ProjectArtifact.document_revision == expected_revision,
            )
            .values(
                document_json=document.model_dump_json(),
                workspace_html=None,
                document_revision=ProjectArtifact.document_revision + 1,
                report_json=None,
                report_revision=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            return None
        self._session.flush()
        return expected_revision + 1

    def save_workspace(
        self,
        project_id: str,
        expected_revision: int,
        document: WorkingDocument,
        html: str,
    ) -> int | None:
        result = self._session.execute(
            update(ProjectArtifact)
            .where(
                ProjectArtifact.project_id == project_id,
                ProjectArtifact.document_json.is_not(None),
                ProjectArtifact.document_revision == expected_revision,
            )
            .values(
                document_json=document.model_dump_json(),
                workspace_html=html,
                document_revision=ProjectArtifact.document_revision + 1,
                report_json=None,
                report_revision=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            return None
        self._session.flush()
        return expected_revision + 1

    def create_workspace(
        self,
        project_id: str,
        document: WorkingDocument,
        html: str,
    ) -> int | None:
        """Create revision one unless the project already has a document."""
        artifact = self._session.get(ProjectArtifact, project_id)
        if artifact is None:
            artifact = ProjectArtifact(project_id=project_id)
            self._session.add(artifact)
        elif artifact.document_json is not None or artifact.document_revision != 0:
            return None
        artifact.document_json = document.model_dump_json()
        artifact.workspace_html = html
        artifact.document_revision = 1
        artifact.report_json = None
        artifact.report_revision = None
        self._session.flush()
        return 1

    def save_report(
        self,
        project_id: str,
        report: CheckReport,
        *,
        expected_document_revision: int | None = None,
        target_source_id: str | None = None,
    ) -> int:
        artifact = self._get_or_create(project_id)
        if artifact.document_json is None or artifact.document_revision <= 0:
            raise ValueError("Нельзя сохранить отчёт без текущего документа")
        if expected_document_revision is None:
            expected_document_revision = artifact.document_revision
        result = self._session.execute(
            update(ProjectArtifact)
            .where(
                ProjectArtifact.project_id == project_id,
                ProjectArtifact.document_json.is_not(None),
                ProjectArtifact.document_revision == expected_document_revision,
            )
            .values(
                report_json=report.model_dump_json(),
                report_revision=expected_document_revision,
                report_generation=ProjectArtifact.report_generation + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            raise ValueError("Документ был изменён во время проверки")
        self._append_check_report(
            project_id,
            expected_document_revision,
            report,
            target_source_id=target_source_id,
        )
        self._session.flush()
        self._session.expire(artifact)
        return expected_document_revision

    def save_document_and_report(
        self,
        project_id: str,
        document: WorkingDocument,
        report: CheckReport,
    ) -> int:
        artifact = self._get_or_create(project_id)
        artifact.document_revision = (artifact.document_revision or 0) + 1
        artifact.document_json = document.model_dump_json()
        artifact.workspace_html = None
        artifact.report_json = report.model_dump_json()
        artifact.report_revision = artifact.document_revision
        artifact.report_generation = (artifact.report_generation or 0) + 1
        self._append_check_report(
            project_id,
            artifact.document_revision,
            report,
            target_source_id=document.source_id,
        )
        self._session.flush()
        return artifact.document_revision

    def get_report(self, project_id: str) -> CheckReport | None:
        artifact = self._session.get(ProjectArtifact, project_id)
        if (
            artifact is None
            or artifact.report_json is None
            or artifact.report_revision is None
            or artifact.report_revision != artifact.document_revision
        ):
            return None
        return CheckReport.model_validate_json(artifact.report_json)

    def get_document_at_revision(
        self, project_id: str, revision: int
    ) -> WorkingDocument | None:
        artifact = self._session.get(ProjectArtifact, project_id)
        if (
            artifact is None
            or artifact.document_json is None
            or artifact.document_revision != revision
        ):
            return None
        return WorkingDocument.model_validate_json(artifact.document_json)

    def get_report_at_revision(
        self,
        project_id: str,
        revision: int,
        *,
        report_generation: int | None = None,
    ) -> CheckReport | None:
        artifact = self._session.get(ProjectArtifact, project_id)
        if (
            artifact is None
            or artifact.report_json is None
            or artifact.document_revision != revision
            or artifact.report_revision != revision
            or (
                report_generation is not None
                and artifact.report_generation != report_generation
            )
        ):
            return None
        return CheckReport.model_validate_json(artifact.report_json)

    def get_revisions(self, project_id: str) -> tuple[int, int | None] | None:
        artifact = self._session.get(ProjectArtifact, project_id)
        if artifact is None:
            return None
        return artifact.document_revision, artifact.report_revision

    def list_check_reports(
        self,
        project_id: str,
        *,
        document_revision: int | None = None,
        check_profile_id: str | None = None,
    ) -> list[StoredCheckReport]:
        statement = select(CheckReportRecord).where(
            CheckReportRecord.project_id == project_id
        )
        if document_revision is not None:
            statement = statement.where(
                CheckReportRecord.document_revision == document_revision
            )
        if check_profile_id is not None:
            statement = statement.where(
                CheckReportRecord.check_profile_id == check_profile_id
            )
        records = self._session.scalars(
            statement.order_by(CheckReportRecord.id)
        ).all()
        return [
            StoredCheckReport(
                id=record.id,
                project_id=record.project_id,
                document_revision=record.document_revision,
                check_profile_id=record.check_profile_id,
                target_source_id=record.target_source_id,
                report=CheckReport.model_validate_json(record.report_json),
            )
            for record in records
        ]

    def _append_check_report(
        self,
        project_id: str,
        document_revision: int,
        report: CheckReport,
        *,
        target_source_id: str | None,
    ) -> None:
        self._session.add(
            CheckReportRecord(
                project_id=project_id,
                document_revision=document_revision,
                check_profile_id=report.check_profile_id or report.template_id,
                target_source_id=target_source_id,
                report_json=report.model_dump_json(),
            )
        )

    def _get_or_create(self, project_id: str) -> ProjectArtifact:
        artifact = self._session.get(ProjectArtifact, project_id)
        if artifact is None:
            artifact = ProjectArtifact(project_id=project_id)
            self._session.add(artifact)
        return artifact
