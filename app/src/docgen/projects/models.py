from sqlalchemy.orm import relationship

from docgen.documents.models import ProjectArtifact
from docgen.jobs.models import Job
from docgen.models import Project, UTCDateTime, utc_now

Project.artifact = relationship(ProjectArtifact, cascade="all, delete-orphan", uselist=False)

__all__ = ["Job", "Project", "ProjectArtifact", "UTCDateTime", "utc_now"]
