import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect

from docgen.config import Settings
from docgen.db import build_session_factory, initialize_database
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
            "CREATE TABLE projects (id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO projects VALUES ('p1', 'Проект', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE project_artifacts (project_id VARCHAR(36) PRIMARY KEY, "
            "document_json TEXT, report_json TEXT, updated_at DATETIME NOT NULL, "
            "FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE)"
        )
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
        artifact_columns = {
            column["name"]
            for column in inspect(migrated_engine).get_columns("project_artifacts")
        }
        with migrated_engine.connect() as connection:
            migrated_target = connection.exec_driver_sql(
                "SELECT target_source_id FROM jobs WHERE id = 'old-job'"
            ).scalar_one()
    finally:
        migrated_engine.dispose()
    assert "target_source_id" in columns
    assert "result_report_generation" in columns
    assert {
        "document_revision",
        "report_revision",
        "report_generation",
        "workspace_html",
    }.issubset(artifact_columns)
    assert "check_report_history" in inspect(migrated_engine).get_table_names()
    assert migrated_target is None


def test_concurrent_first_start_serializes_all_schema_ddl(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'concurrent-fresh.db'}"
    engines = [
        build_session_factory(database_url).kw["bind"],
        build_session_factory(database_url).kw["bind"],
    ]
    start_barrier = Barrier(2)
    ddl_without_lock: list[str] = []

    def record_unserialized_ddl(
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
        elif normalized.startswith(
            ("CREATE TABLE", "ALTER TABLE", "DROP TABLE")
        ) and not connection.info.get("schema_lock_acquired", False):  # type: ignore[attr-defined]
            ddl_without_lock.append(statement)

    for engine in engines:
        event.listen(engine, "before_cursor_execute", record_unserialized_ddl)

    def initialize(engine: object) -> None:
        start_barrier.wait(timeout=5)
        initialize_database(engine)  # type: ignore[arg-type]

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(initialize, engines))
    finally:
        for engine in engines:
            engine.dispose()

    assert results == [None, None]
    assert ddl_without_lock == []
    inspected = create_engine(database_url)
    try:
        assert {
            "projects",
            "sources",
            "project_artifacts",
            "check_report_history",
            "jobs",
        }.issubset(
            inspect(inspected).get_table_names()
        )
    finally:
        inspected.dispose()


def test_startup_moves_coherent_legacy_report_into_check_history(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'legacy-report.db'}"
    legacy = create_engine(database_url)
    report = {
        "template_id": "use-case",
        "findings": [],
        "passed_rule_ids": ["use-case-structure"],
        "unchecked_rules": [],
    }
    with legacy.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE projects (id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO projects VALUES ('p1', 'Проект', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE project_artifacts (project_id VARCHAR(36) PRIMARY KEY, "
            "document_json TEXT, workspace_html TEXT, report_json TEXT, "
            "document_revision INTEGER NOT NULL, report_revision INTEGER, "
            "report_generation INTEGER NOT NULL, updated_at DATETIME NOT NULL, "
            "FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE)"
        )
        connection.exec_driver_sql(
            "INSERT INTO project_artifacts VALUES "
            "('p1', '{}', NULL, ?, 3, 3, 1, CURRENT_TIMESTAMP)",
            (json.dumps(report),),
        )
    legacy.dispose()

    factory = build_session_factory(database_url)
    engine = factory.kw["bind"]
    try:
        initialize_database(engine)
        initialize_database(engine)
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT project_id, document_revision, check_profile_id, report_json "
                "FROM check_report_history"
            ).all()
    finally:
        engine.dispose()

    assert len(rows) == 1
    assert rows[0].project_id == "p1"
    assert rows[0].document_revision == 3
    assert rows[0].check_profile_id == "use-case"
    assert json.loads(rows[0].report_json) == report


def test_every_application_sqlite_connection_enforces_foreign_keys(tmp_path: Path) -> None:
    factory = build_session_factory(f"sqlite:///{tmp_path / 'foreign-keys.db'}")
    engine = factory.kw["bind"]
    try:
        initialize_database(engine)
        with engine.connect() as first, engine.connect() as second:
            assert first.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert second.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    finally:
        engine.dispose()


def test_migration_repairs_dangling_targets_and_installs_restrictive_fk(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'legacy-targets.db'}"
    legacy = create_engine(database_url)
    with legacy.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE projects (id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE sources (id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, "
            "kind VARCHAR(20) NOT NULL, display_name VARCHAR(1024) NOT NULL, media_type VARCHAR(255), "
            "size_bytes INTEGER, storage_path VARCHAR(1024), url VARCHAR(2048), status VARCHAR(32) NOT NULL, "
            "created_at DATETIME NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, "
            "kind VARCHAR(20) NOT NULL, template_id VARCHAR(255) NOT NULL, "
            "target_source_id VARCHAR(36) REFERENCES sources(id) ON DELETE SET NULL, "
            "status VARCHAR(20) NOT NULL, progress INTEGER NOT NULL, status_message TEXT NOT NULL, "
            "error_message TEXT, cancel_requested BOOLEAN NOT NULL, worker_id VARCHAR(255), "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, started_at DATETIME, "
            "finished_at DATETIME, FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE)"
        )
        connection.exec_driver_sql(
            "INSERT INTO projects VALUES ('p1', 'Проект', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        for job_id, status in (("queued", "queued"), ("running", "running"), ("done", "succeeded")):
            connection.exec_driver_sql(
                "INSERT INTO jobs (id, project_id, kind, template_id, target_source_id, status, "
                "progress, status_message, cancel_requested, created_at, updated_at) "
                "VALUES (?, 'p1', 'check', 'use-case', 'missing-source', ?, 0, 'Старое', 0, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (job_id, status),
            )
    legacy.dispose()

    factory = build_session_factory(database_url)
    engine = factory.kw["bind"]
    try:
        initialize_database(engine)
        with engine.connect() as connection:
            rows = {
                row.id: row
                for row in connection.exec_driver_sql(
                    "SELECT id, status, error_message, target_source_id, check_target_kind "
                    "FROM jobs ORDER BY id"
                )
            }
            foreign_keys = connection.exec_driver_sql("PRAGMA foreign_key_list('jobs')").all()
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()

    assert rows["queued"].status == "failed"
    assert rows["running"].status == "failed"
    assert rows["queued"].error_message == "Документ для проверки был удалён; запустите проверку повторно"
    assert rows["done"].status == "succeeded"
    assert all(row.target_source_id is None for row in rows.values())
    assert all(row.check_target_kind == "source" for row in rows.values())
    target_fk = next(row for row in foreign_keys if row[3] == "target_source_id")
    assert target_fk[6].upper() == "RESTRICT"
    assert violations == []
