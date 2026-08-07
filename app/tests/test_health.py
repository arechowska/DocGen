from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import event

from docgen.config import Settings
from docgen.main import create_app


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_disposes_database_engine_on_shutdown(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        data_dir=tmp_path / "data",
        confluence_hosts=("wiki.example.test",),
    )
    application = create_app(settings)
    disposed_engines = []

    with TestClient(application):
        engine = application.state.session_factory.kw["bind"]
        event.listen(engine, "engine_disposed", disposed_engines.append)

    assert disposed_engines == [engine]
