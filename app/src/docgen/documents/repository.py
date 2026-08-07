from sqlalchemy.orm import Session

from .models import ProjectArtifact
from .schemas import CheckReport, WorkingDocument


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_document(self, project_id: str, document: WorkingDocument) -> None:
        artifact = self._get_or_create(project_id)
        artifact.document_json = document.model_dump_json()
        self._session.flush()

    def get_document(self, project_id: str) -> WorkingDocument | None:
        artifact = self._session.get(ProjectArtifact, project_id)
        if artifact is None or artifact.document_json is None:
            return None
        return WorkingDocument.model_validate_json(artifact.document_json)

    def save_report(self, project_id: str, report: CheckReport) -> None:
        artifact = self._get_or_create(project_id)
        artifact.report_json = report.model_dump_json()
        self._session.flush()

    def get_report(self, project_id: str) -> CheckReport | None:
        artifact = self._session.get(ProjectArtifact, project_id)
        if artifact is None or artifact.report_json is None:
            return None
        return CheckReport.model_validate_json(artifact.report_json)

    def _get_or_create(self, project_id: str) -> ProjectArtifact:
        artifact = self._session.get(ProjectArtifact, project_id)
        if artifact is None:
            artifact = ProjectArtifact(project_id=project_id)
            self._session.add(artifact)
        return artifact
