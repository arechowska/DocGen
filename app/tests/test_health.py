from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect

from docgen.config import Settings
from docgen.db import ensure_schema_compatibility
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


def test_startup_adds_check_target_column_to_existing_sqlite_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "existing.db"
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE jobs (
                id VARCHAR(36) PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL,
                kind VARCHAR(20) NOT NULL,
                template_id VARCHAR(255) NOT NULL,
                status VARCHAR(20) NOT NULL,
                progress INTEGER NOT NULL,
                status_message TEXT NOT NULL,
                error_message TEXT,
                cancel_requested BOOLEAN NOT NULL,
                worker_id VARCHAR(255),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                started_at DATETIME,
                finished_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO jobs (
                id, project_id, kind, template_id, status, progress,
                status_message, cancel_requested, created_at, updated_at
            ) VALUES (
                'old-job', 'p1', 'assemble', 'use-case', 'succeeded', 100,
                'Завершено', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        )
    engine.dispose()
    settings = Settings(
        database_url=f"sqlite:///{database_path}",
        data_dir=tmp_path / "data",
    )

    with TestClient(create_app(settings)):
        pass

    migrated_engine = create_engine(settings.database_url)
    try:
        columns = {column["name"] for column in inspect(migrated_engine).get_columns("jobs")}
        with migrated_engine.connect() as connection:
            migrated_target = connection.exec_driver_sql(
                "SELECT target_source_id FROM jobs WHERE id = 'old-job'"
            ).scalar_one()
    finally:
        migrated_engine.dispose()
    assert "target_source_id" in columns
    assert migrated_target is None


def test_concurrent_schema_compatibility_is_serialized(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'concurrent.db'}"
    setup_engine = create_engine(database_url)
    with setup_engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY)")
    setup_engine.dispose()

    engines = [create_engine(database_url), create_engine(database_url)]
    inspection_barrier = Barrier(2)

    def widen_unserialized_inspection_race(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del cursor, parameters, context, executemany
        normalized = statement.strip().upper()
        if normalized == "BEGIN IMMEDIATE":
            connection.info["schema_lock_acquired"] = True  # type: ignore[attr-defined]
        elif normalized.startswith("PRAGMA TABLE_INFO") and not connection.info.get(  # type: ignore[attr-defined]
            "schema_lock_acquired", False
        ):
            inspection_barrier.wait(timeout=5)

    for engine in engines:
        event.listen(engine, "before_cursor_execute", widen_unserialized_inspection_race)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(ensure_schema_compatibility, engines))
    finally:
        for engine in engines:
            engine.dispose()

    assert results == [None, None]
