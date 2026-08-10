from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def build_session_factory(database_url: str) -> sessionmaker[Session]:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    return sessionmaker(engine, expire_on_commit=False)


def ensure_schema_compatibility(engine: Engine) -> None:
    """Apply additive compatibility changes for existing local SQLite databases."""
    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "jobs" in tables:
            columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info('jobs')")
            }
            if "target_source_id" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE jobs ADD COLUMN target_source_id VARCHAR(36) "
                    "REFERENCES sources(id) ON DELETE SET NULL"
                )
        connection.commit()
