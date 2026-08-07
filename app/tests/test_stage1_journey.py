from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docgen.config import Settings
from docgen.main import create_app


@pytest.fixture
def app_factory(tmp_path: Path) -> Callable[[], Iterator[TestClient]]:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'stage1.db'}",
        data_dir=tmp_path / "data",
        confluence_hosts=("wiki.example.test",),
    )

    @contextmanager
    def start_app() -> Iterator[TestClient]:
        with TestClient(create_app(settings)) as client:
            yield client

    return start_app


def test_stage1_journey_survives_app_restart(
    app_factory: Callable[[], Iterator[TestClient]],
) -> None:
    """Catch a restart that loses the configured SQLite database or source metadata."""
    with app_factory() as first_client:
        created = first_client.post(
            "/projects", data={"name": "ТЗ платежей"}, follow_redirects=False
        )
        project_url = created.headers["location"]

        file_response = first_client.post(
            f"{project_url}/sources/files",
            files={"file": ("input.txt", b"data", "text/plain")},
        )
        confluence_response = first_client.post(
            f"{project_url}/sources/confluence",
            data={"url": "https://wiki.example.test/display/DOC/Page"},
        )

        assert created.status_code == 303
        assert file_response.status_code == 200
        assert confluence_response.status_code == 200

    with app_factory() as second_client:
        response = second_client.get(project_url)

    assert response.status_code == 200
    assert "ТЗ платежей" in response.text
    assert "input.txt" in response.text
    assert "wiki.example.test" in response.text
