import subprocess
import sys


def test_project_model_registers_source_relationship_in_fresh_interpreter() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from tempfile import TemporaryDirectory; "
                "from sqlalchemy import create_engine; "
                "from sqlalchemy.orm import Session; "
                "from docgen.db import Base; "
                "from docgen.projects.models import Project; "
                "from docgen.projects.repository import ProjectRepository; "
                "from docgen.projects.service import ProjectService; "
                "from docgen.sources.storage import LocalStorage; "
                "engine = create_engine('sqlite://'); "
                "Base.metadata.create_all(engine); "
                "session = Session(engine); "
                "project = ProjectRepository(session).create('Проект'); "
                "session.commit(); "
                "data_dir = TemporaryDirectory(); "
                "ProjectService(session, LocalStorage(Path(data_dir.name))).delete(project.id); "
                "assert session.get(Project, project.id) is None; "
                "data_dir.cleanup()"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
