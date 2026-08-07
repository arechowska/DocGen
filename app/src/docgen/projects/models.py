from sqlalchemy.orm import relationship

from docgen.documents.models import ProjectArtifact
from docgen.models import Project, UTCDateTime, utc_now

Project.artifact = relationship(ProjectArtifact, cascade="all, delete-orphan", uselist=False)

__all__ = ["Project", "ProjectArtifact", "UTCDateTime", "utc_now"]
