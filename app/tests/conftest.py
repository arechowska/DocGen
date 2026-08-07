from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from docgen.config import Settings
from docgen.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        data_dir=tmp_path / "data",
        confluence_hosts=("wiki.example.test",),
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def engine() -> Engine:
    from docgen.db import Base
    from docgen.projects.models import Project
    from docgen.sources.models import Source

    database_engine = create_engine("sqlite://")
    Base.metadata.create_all(database_engine, tables=(Project.__table__, Source.__table__))
    yield database_engine
    Base.metadata.drop_all(database_engine, tables=(Source.__table__, Project.__table__))
    database_engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Session:
    connection = engine.connect()
    transaction = connection.begin()
    database_session = Session(bind=connection)
    yield database_session
    database_session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def project_repository(session: Session):
    from docgen.projects.repository import ProjectRepository

    return ProjectRepository(session)


@pytest.fixture
def source_service(session: Session, tmp_path: Path):
    from docgen.sources.service import SourceService
    from docgen.sources.storage import LocalStorage

    return SourceService(
        session,
        LocalStorage(tmp_path / "data"),
        confluence_hosts=("wiki.example.test",),
    )
