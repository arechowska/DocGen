from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def build_session_factory(database_url: str) -> sessionmaker[Session]:
    is_sqlite = make_url(database_url).get_backend_name() == "sqlite"
    connect_args = (
        {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
    )
    engine = create_engine(database_url, connect_args=connect_args)
    if is_sqlite:
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return sessionmaker(engine, expire_on_commit=False)


def _enable_sqlite_foreign_keys(dbapi_connection: object, connection_record: object) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def initialize_database(engine: Engine) -> None:
    """Create and migrate the complete schema under one SQLite writer lock."""
    _register_models()
    if engine.dialect.name != "sqlite":
        Base.metadata.create_all(engine)
        return

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            Base.metadata.create_all(connection)
            _migrate_project_artifacts(connection)
            _migrate_jobs(connection)
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
            if violations:
                raise RuntimeError("Не удалось восстановить ссылочную целостность базы данных")
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def ensure_schema_compatibility(engine: Engine) -> None:
    """Backward-compatible alias for the serialized database initializer."""
    initialize_database(engine)


def begin_sqlite_writer_transaction(session: Session) -> None:
    """End deferred reads and reserve the SQLite writer before revalidation."""
    session.rollback()
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _register_models() -> None:
    from docgen.documents.models import ProjectArtifact  # noqa: F401
    from docgen.jobs.models import Job  # noqa: F401
    from docgen.projects.models import Project  # noqa: F401
    from docgen.sources.models import Source  # noqa: F401


def _table_names(connection: Connection) -> set[str]:
    return {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in connection.exec_driver_sql(f"PRAGMA table_info('{table}')")
    }


def _migrate_project_artifacts(connection: Connection) -> None:
    if "project_artifacts" not in _table_names(connection):
        return
    columns = _columns(connection, "project_artifacts")
    if "workspace_html" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE project_artifacts ADD COLUMN workspace_html TEXT"
        )
    added_revision = "document_revision" not in columns
    if added_revision:
        connection.exec_driver_sql(
            "ALTER TABLE project_artifacts ADD COLUMN document_revision "
            "INTEGER NOT NULL DEFAULT 0"
        )
        connection.exec_driver_sql(
            "UPDATE project_artifacts SET document_revision = 1 "
            "WHERE document_json IS NOT NULL"
        )
    if "report_revision" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE project_artifacts ADD COLUMN report_revision INTEGER"
        )
        # Legacy reports were not bound to a document and cannot be proven coherent.
        connection.exec_driver_sql(
            "UPDATE project_artifacts SET report_json = NULL, report_revision = NULL"
        )
    if "report_generation" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE project_artifacts ADD COLUMN report_generation "
            "INTEGER NOT NULL DEFAULT 0"
        )
        connection.exec_driver_sql(
            "UPDATE project_artifacts SET report_generation = 1 "
            "WHERE report_json IS NOT NULL"
        )
    connection.exec_driver_sql(
        "DELETE FROM project_artifacts WHERE project_id NOT IN (SELECT id FROM projects)"
    )
    connection.exec_driver_sql(
        "UPDATE project_artifacts SET report_json = NULL, report_revision = NULL "
        "WHERE report_revision IS NOT document_revision"
    )


def _migrate_jobs(connection: Connection) -> None:
    if "jobs" not in _table_names(connection):
        return

    original_columns = _columns(connection, "jobs")
    additions = {
        "target_source_id": (
            "VARCHAR(36) REFERENCES sources(id) ON DELETE RESTRICT"
        ),
        "check_target_kind": "VARCHAR(20)",
        "worker_instance_token": "VARCHAR(64)",
        "lease_expires_at": "DATETIME",
        "warnings_json": "TEXT NOT NULL DEFAULT '[]'",
        "result_document_revision": "INTEGER",
        "result_report_revision": "INTEGER",
        "result_report_generation": "INTEGER",
        "export_format": "VARCHAR(20)",
        "requested_document_revision": "INTEGER",
        "export_relative_path": "VARCHAR(1024)",
        "export_filename": "VARCHAR(255)",
        "export_media_type": "VARCHAR(255)",
        "export_size_bytes": "INTEGER",
        "export_document_revision": "INTEGER",
    }
    for column, definition in additions.items():
        if column not in original_columns:
            connection.exec_driver_sql(
                f"ALTER TABLE jobs ADD COLUMN {column} {definition}"
            )

    connection.exec_driver_sql(
        "UPDATE jobs SET check_target_kind = CASE "
        "WHEN kind = 'check' AND target_source_id IS NOT NULL THEN 'source' "
        "WHEN kind = 'check' THEN 'current' ELSE NULL END "
        "WHERE check_target_kind IS NULL"
    )
    invalid_target = (
        "target_source_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM sources WHERE sources.id = jobs.target_source_id "
        "AND sources.project_id = jobs.project_id)"
    )
    public_error = "Документ для проверки был удалён; запустите проверку повторно"
    connection.exec_driver_sql(
        "UPDATE jobs SET status = 'failed', status_message = 'Задание завершилось с ошибкой', "
        "error_message = ?, finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP), "
        "updated_at = CURRENT_TIMESTAMP, target_source_id = NULL, lease_expires_at = NULL "
        f"WHERE {invalid_target} AND status IN ('queued', 'running')",
        (public_error,),
    )
    connection.exec_driver_sql(
        f"UPDATE jobs SET target_source_id = NULL WHERE {invalid_target}"
    )
    connection.exec_driver_sql(
        "DELETE FROM jobs WHERE project_id NOT IN (SELECT id FROM projects)"
    )
    connection.exec_driver_sql(
        "DELETE FROM sources WHERE project_id NOT IN (SELECT id FROM projects)"
    )

    foreign_keys = connection.exec_driver_sql("PRAGMA foreign_key_list('jobs')").all()
    target_fk = next((row for row in foreign_keys if row[3] == "target_source_id"), None)
    needs_rebuild = (
        set(additions) - original_columns
        or target_fk is None
        or str(target_fk[6]).upper() != "RESTRICT"
    )
    if needs_rebuild:
        _rebuild_jobs(connection)


def _rebuild_jobs(connection: Connection) -> None:
    from docgen.jobs.models import Job

    legacy_table = "jobs_legacy_final_fix"
    connection.exec_driver_sql(f"DROP TABLE IF EXISTS {legacy_table}")
    connection.exec_driver_sql(f"ALTER TABLE jobs RENAME TO {legacy_table}")
    indexes = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
        "AND name NOT LIKE 'sqlite_autoindex%'",
        (legacy_table,),
    ).all()
    for (index_name,) in indexes:
        quoted = str(index_name).replace('"', '""')
        connection.exec_driver_sql(f'DROP INDEX "{quoted}"')

    Job.__table__.create(connection, checkfirst=False)
    legacy_columns = _columns(connection, legacy_table)
    copied_columns = [column.name for column in Job.__table__.columns if column.name in legacy_columns]
    column_sql = ", ".join(f'"{column}"' for column in copied_columns)
    connection.exec_driver_sql(
        f"INSERT INTO jobs ({column_sql}) SELECT {column_sql} FROM {legacy_table}"
    )
    connection.exec_driver_sql(f"DROP TABLE {legacy_table}")
